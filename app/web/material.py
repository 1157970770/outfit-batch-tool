#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材检索：按款号找素材文件夹地址。

数据来自定妆自动化区检索管道产出的 material-index.json
（契约见 C:\\Users\\EDY\\Desktop\\迁移指南.md，字段按下标取值、不按键）：

    folders[i] = [path, codes(逗号分隔), desc, 文件数, 视频数, MB, src, star, note]
    files[i]   = [folderIndex, name, MB, catIndex]
    src：0=服装图片库（Z:\\产品原图文件夹），1=男装视频库（Z:\\星伙公共空间二）

检索语义与原插件保持一致（10 条里的关键 6 条）：
  · 大小写无关；比对款号时剥离 `-` / `_` / 空格（3100 → 命中 LL-SCS3100）；
  · 款号形查询（≤6 位字母前缀 + ≥3 位数字，如 ll1132 / ax111 / aw26143）
    优先只走款号匹配，避免目录名误伤；查不到再回退路径/描述模糊匹配；
  · scope：-1 全部 / 0 服装图片 / 1 男装视频；-1 时两来源各取一半
    （balance：视频库 4TB，不配平会把图片结果全部挤下去）；
  · 空查询 → 体积最大的前 N 个；
  · 类别用预计算的位掩码（catmask）过滤；
  · 打开文件夹只允许 rootWhitelist（Z:\\）下的路径，其余一律拒绝。

索引文件由外部管道重建：这里只读、按 mtime 热重载，读不到只降级
「素材检索不可用」，不影响网页其它功能。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

# 款号形查询：0–6 位字母 + ≥3 位数字 + 可选字母数字尾巴（归一化后判定）。
# 例：ll1132 / llscs3100 / ax111 / aw26143 / 3100。
_CODE_QUERY_RE = re.compile(r"^[a-z]{0,6}\d{3,}[a-z0-9]{0,4}$")
# 比对款号时剥离的字符：空格、连字符、下划线。
_STRIP_RE = re.compile(r"[\s\-_]+")
# 组合款分隔：半角/全角逗号。
_CODE_SPLIT_RE = re.compile(r"[,，]")


def normalize_code(text: str) -> str:
    """款号归一化：小写 + 剥离空格/连字符/下划线。"""
    return _STRIP_RE.sub("", str(text or "")).lower()


def is_code_query(text: str) -> bool:
    """查询词是否「长得像款号」：是则优先走款号匹配。"""
    return bool(_CODE_QUERY_RE.fullmatch(normalize_code(text)))


def map_unc(path: str, mapping: dict) -> str:
    """把本机盘符路径映射成 UNC 网络路径（给没映射 Z: 盘的同事复制用）。

    mapping 形如 {"Z:\\": "\\\\192.168.1.21\\admin 共享给我\\"}；
    键按「更长者优先」匹配，未命中返回空串。
    """
    raw = str(path or "")
    if not raw or not isinstance(mapping, dict) or not mapping:
        return ""
    lowered = raw.lower()
    best = ""
    for prefix, unc in sorted(mapping.items(), key=lambda kv: -len(str(kv[0]))):
        prefix_raw = str(prefix or "")
        if not prefix_raw or not unc:
            continue
        if lowered.startswith(prefix_raw.lower()):
            best = str(unc) + raw[len(prefix_raw):]
            break
    return best.lstrip("\\") if best.startswith("\\\\") is False else best


def _int_field(entry: list, index: int, default: int = 0) -> int:
    """数组下标取整数，越界/非数字回退默认值（契约要求字段兜底）。"""
    try:
        return int(entry[index])
    except (IndexError, TypeError, ValueError):
        return default


def _float_field(entry: list, index: int, default: float = 0.0) -> float:
    try:
        return float(entry[index])
    except (IndexError, TypeError, ValueError):
        return default


def _str_field(entry: list, index: int, default: str = "") -> str:
    try:
        return str(entry[index] or default)
    except (IndexError, TypeError):
        return default


def _open_path(path: str) -> None:
    """实际打开目录的钩子；单拆出来是为了测试能替换，不真开资源管理器。"""
    os.startfile(path)  # noqa: S606 - 只对白名单内的本机目录调用


class MaterialIndex:
    """material-index.json 的只读检索器；按 mtime 热重载，线程安全。"""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.error = ""
        self.loaded_at = 0.0
        self._lock = threading.Lock()
        self._doc: dict | None = None
        self._stamp: tuple[float, int] = (0.0, 0)   # (mtime, size)：双指标防时间精度
        # ---- 派生数据（加载时一次性预计算，检索零重复解析）----
        self.folders: list[list] = []
        self.files: list[list] = []
        self.cats: list[str] = []
        self.scopes: list[list] = []
        self.root_whitelist: list[str] = []
        self.generated_at = ""
        self.counts: dict = {}
        self._codes: list[list[str]] = []       # 每目录原始款号（展示用）
        self._norm_codes: list[list[str]] = []  # 每目录归一化款号（匹配用）
        self._code_hits: dict[str, list[int]] = {}  # 归一化款号 → 目录下标（整码命中直达）
        self._cat_counts: list[dict[int, int]] = []  # 每目录 catIndex → 文件数
        self._catmask: list[int] = []           # 每目录类别位掩码
        self._path_lower: list[str] = []
        self._desc_lower: list[str] = []

    # ------------------------------------------------------------ 加载

    @property
    def available(self) -> bool:
        return self._ensure()

    def _stat(self) -> tuple[float, int] | None:
        if self.path is None:
            self.error = "素材索引未配置（server/web.json → material_index）"
            return None
        try:
            info = self.path.stat()
        except OSError as exc:
            self._doc = None
            self.error = f"素材索引读不到：{self.path}（{exc}）"
            return None
        return (info.st_mtime, info.st_size)

    def _ensure(self) -> bool:
        stamp = self._stat()
        if stamp is None:
            return False
        if self._doc is not None and stamp == self._stamp:
            return True
        with self._lock:
            if self._doc is not None and stamp == self._stamp:
                return True
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                self._doc = None
                self.error = f"素材索引解析失败：{exc}"
                return False
            if not isinstance(doc, dict) or not isinstance(doc.get("folders"), list):
                self._doc = None
                self.error = "素材索引格式不对：顶层缺少 folders 数组"
                return False
            self._build(doc)
            self._doc = doc
            self._stamp = stamp
            self.error = ""
            self.loaded_at = time.time()
            return True

    def _build(self, doc: dict) -> None:
        self.folders = doc.get("folders") or []
        files = doc.get("files") or []
        self.files = files if isinstance(files, list) else []
        cats = doc.get("cats") or []
        self.cats = [str(c) for c in cats] if isinstance(cats, list) else []
        scopes = doc.get("scopes") or []
        self.scopes = [list(s) for s in scopes if isinstance(s, list)] if isinstance(scopes, list) else []
        roots = doc.get("rootWhitelist") or []
        self.root_whitelist = [str(r) for r in roots if str(r)] if isinstance(roots, list) else []
        self.generated_at = str(doc.get("generatedAt") or "")
        self.counts = doc.get("counts") if isinstance(doc.get("counts"), dict) else {}

        count = len(self.folders)
        self._codes = [[] for _ in range(count)]
        self._norm_codes = [[] for _ in range(count)]
        self._cat_counts = [dict() for _ in range(count)]
        self._catmask = [0] * count
        self._path_lower = [""] * count
        self._desc_lower = [""] * count
        self._code_hits = {}

        for i, entry in enumerate(self.folders):
            if not isinstance(entry, list):
                entry = []
            self._path_lower[i] = _str_field(entry, 0).lower()
            self._desc_lower[i] = _str_field(entry, 2).lower()
            codes = [c.strip() for c in _CODE_SPLIT_RE.split(_str_field(entry, 1)) if c.strip()]
            self._codes[i] = codes
            norms = [normalize_code(c) for c in codes]
            self._norm_codes[i] = norms
            for norm in norms:
                self._code_hits.setdefault(norm, []).append(i)

        for entry in self.files:
            if not isinstance(entry, list):
                continue
            try:
                folder_index = int(entry[0])
            except (TypeError, ValueError):
                continue
            cat_index = _int_field(entry, 3, -1)
            if not 0 <= folder_index < count:
                continue
            if 0 <= cat_index < len(self.cats):
                bucket = self._cat_counts[folder_index]
                bucket[cat_index] = bucket.get(cat_index, 0) + 1
                self._catmask[folder_index] |= 1 << cat_index

    # ------------------------------------------------------------ 元信息

    def meta(self) -> dict:
        if not self._ensure():
            return {"available": False, "error": self.error,
                    "path": str(self.path or ""), "unc_roots": []}
        all_counts = self.counts.get("all") if isinstance(self.counts.get("all"), dict) else {}
        return {
            "available": True,
            "error": "",
            "path": str(self.path or ""),
            "generatedAt": self.generated_at,
            "cats": self.cats,
            "scopes": self.scopes,
            "counts": {"folders": len(self.folders), "files": len(self.files),
                       **{k: v for k, v in all_counts.items() if isinstance(v, int)}},
            "unc_roots": sorted({str(k) for k in self._unc_roots}),
        }

    # search() 里逐条算 map_unc 太散：这里先记下配置的 UNC 前缀（由 server 注入），
    # meta 用来告诉前端「有网络路径可复制」。
    _unc_roots: set[str] = set()

    def set_unc_mapping(self, mapping: dict) -> None:
        self._unc_roots = {str(k) for k in (mapping or {}) if str(k)}

    # ------------------------------------------------------------ 检索

    def search(self, q: str, *, scope: int = -1, cat: int = -1,
               limit: int = 30, offset: int = 0) -> dict:
        """按款号/关键词找目录。返回 {total, items, truncated, mode}。

        排序：整码命中 > 前缀命中 > 包含命中；同级 ★ 优先、体积大的优先。
        scope=-1 时两来源配平（各占一半交替出列），避免 4TB 视频淹没图片。
        """
        limit = max(1, min(100, int(limit or 30)))
        offset = max(0, int(offset or 0))
        query = str(q or "").strip()
        norm_q = normalize_code(query)
        if not self._ensure():
            return {"total": 0, "items": [], "truncated": False, "mode": "unavailable",
                    "error": self.error}

        mode = "code" if (query and is_code_query(query)) else "fuzzy"
        matched = self._match(query, norm_q, mode, scope, cat)
        if not matched and query and mode == "code":
            # 款号没查到：回退路径/描述模糊匹配，别让用户白跑一趟
            mode = "fallback"
            matched = self._match(query, norm_q, "fuzzy", scope, cat)

        if not query:
            # 空查询：按 星标 > 体积 给个「库概览」
            matched = self._base_list(scope, cat)
            mode = "empty"

        ordered = self._balanced(matched) if scope == -1 else self._sorted(matched)
        total = len(ordered)
        window = ordered[offset:offset + limit]
        return {"total": total, "truncated": offset + len(window) < total,
                "mode": mode, "items": [self._item(i, norm_q) for i in window]}

    def _match(self, query: str, norm_q: str, mode: str,
               scope: int, cat: int) -> list[tuple[int, int]]:
        """返回 [(rank, folder_index)]；rank 0=整码 1=前缀 2=包含/模糊。"""
        matched: dict[int, int] = {}
        lower_q = query.lower()

        def accept(index: int, rank: int) -> None:
            if rank < matched.get(index, 9):
                matched[index] = rank

        if mode == "code":
            # 整码命中走预计算字典直达（~3ms 的关键）；前缀/包含再扫一遍。
            for index in self._code_hits.get(norm_q, []):
                accept(index, 0)
            if norm_q:
                for index, norms in enumerate(self._norm_codes):
                    if index in matched:
                        continue
                    for norm in norms:
                        if norm == norm_q:
                            accept(index, 0)
                        elif norm.startswith(norm_q):
                            accept(index, 1)
                        elif norm_q in norm:
                            accept(index, 2)
        else:
            for index in range(len(self.folders)):
                hit = -1
                if lower_q and (lower_q in self._path_lower[index]
                                or lower_q in self._desc_lower[index]):
                    hit = 2
                else:
                    for norm in self._norm_codes[index]:
                        if norm_q and norm_q in norm:
                            hit = 2
                            break
                if hit >= 0:
                    accept(index, hit)

        if scope in (0, 1):
            matched = {i: r for i, r in matched.items() if self._src_of(i) == scope}
        if 0 <= cat < len(self.cats):
            mask = 1 << cat
            matched = {i: r for i, r in matched.items() if self._catmask[i] & mask}
        return [(rank, index) for index, rank in matched.items()]

    def _base_list(self, scope: int, cat: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for index in range(len(self.folders)):
            if scope in (0, 1) and self._src_of(index) != scope:
                continue
            if 0 <= cat < len(self.cats) and not (self._catmask[index] & (1 << cat)):
                continue
            out.append((0, index))
        return out

    def _src_of(self, index: int) -> int:
        entry = self.folders[index]
        return _int_field(entry, 6, 0) if isinstance(entry, list) else 0

    def _sorted(self, matched: list[tuple[int, int]]) -> list[int]:
        def key(pair: tuple[int, int]) -> tuple:
            rank, index = pair
            entry = self.folders[index]
            star = _int_field(entry, 7, 0) if isinstance(entry, list) else 0
            mb = _float_field(entry, 5, 0.0) if isinstance(entry, list) else 0.0
            return (rank, -star, -mb)
        return [index for _, index in sorted(matched, key=key)]

    def _balanced(self, matched: list[tuple[int, int]]) -> list[int]:
        """scope=-1 时两来源各取一半：交替出列（图片库先出），谁先用完谁退场。"""
        pools = {0: [], 1: []}
        for index in self._sorted(matched):
            pools.setdefault(self._src_of(index), pools[0]).append(index)
        out: list[int] = []
        left, right = pools.get(0, []), pools.get(1, [])
        i = j = 0
        while i < len(left) or j < len(right):
            if i < len(left):
                out.append(left[i]); i += 1
            if j < len(right):
                out.append(right[j]); j += 1
        return out

    def _item(self, index: int, norm_q: str = "") -> dict:
        entry = self.folders[index] if isinstance(self.folders[index], list) else []
        codes = self._codes[index]
        cat_counts = self._cat_counts[index]
        cats = [{"name": self.cats[cat], "count": count}
                for cat, count in sorted(cat_counts.items(), key=lambda kv: -kv[1])
                if 0 <= cat < len(self.cats)]
        matched_codes = [c for c, n in zip(codes, self._norm_codes[index])
                         if norm_q and (n == norm_q or norm_q in n)]
        return {
            "path": _str_field(entry, 0),
            "codes": codes,
            "desc": _str_field(entry, 2),
            "note": _str_field(entry, 8),
            "files": _int_field(entry, 3, 0),
            "videos": _int_field(entry, 4, 0),
            "mb": round(_float_field(entry, 5, 0.0), 2),
            "src": self._src_of(index),
            "star": _int_field(entry, 7, 0),
            "cats": cats[:6],
            "matched": matched_codes[:4],
        }

    # ------------------------------------------------------------ 打开目录

    def open_folder(self, path: str) -> str:
        """在主机上用资源管理器打开目录；返回错误文案，空串 = 成功。

        只放行索引 rootWhitelist 下的路径（Z:\\），且拒绝 `..` 注入；
        打开动作发生在服务所在的电脑上，不是访问者的电脑。
        """
        if not self._ensure():
            return self.error or "素材索引不可用"
        target = str(path or "").strip().rstrip("\\")
        if not target or ".." in target:
            return "路径不合法"
        lowered = target.lower()
        if not any(lowered.startswith(str(root).lower()) for root in self.root_whitelist):
            return "该路径不在素材库白名单内，只允许打开素材目录"
        try:
            _open_path(target)
        except OSError as exc:
            return f"打开失败：{exc}"
        return ""
