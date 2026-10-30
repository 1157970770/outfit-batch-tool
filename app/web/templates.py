#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板选择逻辑；浏览器只拿模板编号/名称，不拿 COS 地址。

数据来源约定：
- 标签工作簿（本机「标签清单1版.xlsx」）是**生产清单**，也是候选集的唯一来源。
- COS manifest 只是"哪些视频已经上传云端"的索引，仅用于给同一条视频补 url/key，
  补全时必须通过稳定键（相对路径/文件名/文件指纹）+ 大小一致性校验，
  **绝不允许按模板 ID 数字硬配**——两套编号体系互不相干，按 ID 匹配会拿错视频。
"""
from __future__ import annotations

import math
import random
import urllib.parse
from pathlib import Path


TAG_CATEGORIES = ["景别", "适合季节", "场景", "姿势"]


def _normalize_id(value) -> str:
    text = str(value or "").strip()
    return text.zfill(3) if text.isdigit() else text


def _infer_category(tag: str) -> str:
    if tag in {"近景", "中景", "中远景", "远景", "特写"}:
        return "景别"
    if tag in {"适合春夏", "适合秋冬", "适合春季", "适合夏季", "适合秋季", "适合冬季"}:
        return "适合季节"
    if tag in {"手串展示", "只有上半身", "坐在椅子上", "裤子展示", "站立", "坐姿"}:
        return "姿势"
    return "场景"


def _as_float(value) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _basename(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = urllib.parse.urlsplit(text).path
    return text.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _source_keys(item: dict) -> set[str]:
    """一条已上传模板可供 join 的稳定键（文件名级别，不含数字 ID）。"""
    keys: set[str] = set()
    for field in ("relative_path", "filename"):
        name = _basename(item.get(field))
        if name:
            keys.add(name)
    for field in ("key", "url"):
        name = _basename(item.get(field))
        if name:
            keys.add(name)
    return keys


def _sizes_agree(row_size: float | None, source_size: float | None) -> bool:
    """两处记录都有大小时必须一致，否则视为不同视频。缺一侧则不足以证明同一视频。"""
    if row_size is None or source_size is None:
        return False
    return abs(row_size - source_size) <= max(0.05, row_size * 0.01)


class TemplateError(ValueError):
    pass


class TemplateCatalog:
    def __init__(self, templates: list[dict]):
        self.items = [dict(item) for item in templates]
        self._reindex()
        self.tag_categories = list(TAG_CATEGORIES)
        self.tag_values: dict[str, list[str]] = {category: [] for category in TAG_CATEGORIES}
        self.tag_workbook_info: dict = {}
        self._collect_tag_values()

    def _collect_tag_values(self):
        self.tag_values = {category: [] for category in TAG_CATEGORIES}
        for item in self.items:
            categories = item.get("tag_categories", {})
            for tag in item.get("tags", []):
                category = categories.get(tag, _infer_category(tag))
                if category in self.tag_values and tag not in self.tag_values[category]:
                    self.tag_values[category].append(tag)

    def _reindex(self):
        ids = [_normalize_id(item.get("id")) for item in self.items]
        if len(ids) != len(set(ids)):
            raise TemplateError("视频场景清单中存在重复模板ID，无法安全匹配标签表格")
        for item, template_id in zip(self.items, ids):
            item["id"] = template_id
        self.by_id = {str(item.get("id")): item for item in self.items}

    # ------------------------------------------------------------------ 工作簿
    def attach_tag_workbook(self, workbook_path: str | Path) -> dict:
        """只读加载标签工作簿，并以其为生产清单重建候选集。"""
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise TemplateError("主机缺少 openpyxl，无法读取视频标签清单（请先安装依赖）") from None
        path = Path(workbook_path)
        if not path.is_file():
            raise TemplateError(f"标签清单不存在：{path}")
        try:
            workbook = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            raise TemplateError(f"无法读取标签清单：{exc}") from None
        required = {"视频标签清单", "标签字典"}
        if not required.issubset(set(workbook.sheetnames)):
            workbook.close()
            raise TemplateError("标签清单缺少“视频标签清单”或“标签字典”工作表")

        try:
            categories = self._read_tag_dictionary(workbook["标签字典"])
            rows = self._read_tag_rows(workbook["视频标签清单"], categories)
            video_dir = self._read_project_video_dir(workbook)
        finally:
            workbook.close()

        source_index, fp_index = self._build_source_index()
        matched_source = 0
        for row in rows:
            source = self._pick_source(row, source_index, fp_index)
            row["url"] = str((source or {}).get("url") or "")
            row["key"] = str((source or {}).get("key") or "")
            if source is not None:
                matched_source += 1
            else:
                row["url"] = row["key"] = ""
            row.pop("_keys", None)
            row.pop("_size", None)
            row.pop("_fingerprint", None)

        manifest_count = len(self.items)
        self.items = rows
        self._reindex()
        self._collect_tag_values()
        self.tag_workbook_info = {
            "path": str(path),
            "workbook_rows": len(rows),
            "matched_templates": len(rows),
            "source_ready_templates": matched_source,
            "manifest_entries": manifest_count,
            "manifest_unmatched": manifest_count - matched_source,
            "unlisted_files": self._count_unlisted(video_dir, rows),
        }
        return dict(self.tag_workbook_info)

    @staticmethod
    def _read_tag_dictionary(sheet) -> dict[str, str]:
        header = [str(value or "").strip() for value in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
        tag_col = {name: i for i, name in enumerate(header)}
        if "标签值" not in tag_col:
            raise TemplateError("标签字典缺少“标签值”列")
        categories: dict[str, str] = {}
        for values in sheet.iter_rows(min_row=2, values_only=True):
            if not values or tag_col["标签值"] >= len(values) or not values[tag_col["标签值"]]:
                continue
            tag = str(values[tag_col["标签值"]]).strip()
            raw_category = values[tag_col["分类"]] if "分类" in tag_col and tag_col["分类"] < len(values) else None
            category = str(raw_category or "").strip()
            categories[tag] = category if category in TAG_CATEGORIES else _infer_category(tag)
        if not categories:
            raise TemplateError("标签字典是空的，无法按标签筛选")
        return categories

    @staticmethod
    def _read_project_video_dir(workbook) -> Path | None:
        if "项目" not in workbook.sheetnames:
            return None
        for values in workbook["项目"].iter_rows(values_only=True):
            if values and str(values[0] or "").strip() == "本机视频文件夹" and len(values) > 1:
                text = str(values[1] or "").strip()
                return Path(text) if text else None
        return None

    @staticmethod
    def _read_tag_rows(sheet, categories: dict[str, str]) -> list[dict]:
        header = [str(value or "").strip() for value in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
        columns = {name: i for i, name in enumerate(header)}
        needed = ("模板ID", "相对路径", "文件名", "标签")
        if any(name not in columns for name in needed):
            raise TemplateError("视频标签清单缺少模板ID、相对路径、文件名或标签列")

        def cell(values, name):
            index = columns.get(name)
            if index is None or index >= len(values):
                return None
            return values[index]

        rows: list[dict] = []
        seen: dict[str, int] = {}
        for values in sheet.iter_rows(min_row=2, values_only=True):
            if not values or cell(values, "模板ID") is None or not str(cell(values, "模板ID")).strip():
                continue
            template_id = _normalize_id(cell(values, "模板ID"))
            if template_id in seen:
                raise TemplateError(f"标签清单中模板ID重复：{template_id}")
            relative = str(cell(values, "相对路径") or "").replace("\\", "/").strip()
            filename = str(cell(values, "文件名") or "").strip()
            if not relative or not filename:
                raise TemplateError(f"标签清单中模板ID {template_id} 的视频路径或文件名为空")
            raw_tags = str(cell(values, "标签") or "")
            tags = list(dict.fromkeys(part.strip() for part in raw_tags.replace("；", ";").split(";") if part.strip()))
            unknown = [tag for tag in tags if tag not in categories]
            if unknown:
                raise TemplateError(f"视频 {relative} 使用了标签字典外的值：{unknown[0]}")
            if not tags:
                raise TemplateError(f"视频 {relative} 没有任何标签，无法参与筛选")
            size = _as_float(cell(values, "大小(MB)"))
            rows.append({
                "id": template_id,
                "name": filename,
                "filename": filename,
                "relative_path": relative,
                "tags": tags,
                "tag_categories": {tag: categories[tag] for tag in tags},
                "upload_state": str(cell(values, "状态") or "").strip(),
                "duration": _as_float(cell(values, "时长(秒)")),
                "size_mb": size,
                "shot": "",
                "_keys": {_basename(relative), _basename(filename)} - {""},
                "_size": size,
                "_fingerprint": str(cell(values, "文件指纹") or "").strip().lower(),
            })
            seen[template_id] = len(rows) - 1
        if not rows:
            raise TemplateError("视频标签清单没有任何有效行")
        return rows

    def _build_source_index(self):
        """用旧 manifest 建索引，键只取文件名/相对路径/指纹，不取 ID。"""
        by_key: dict[str, list[dict]] = {}
        by_fp: dict[str, list[dict]] = {}
        for item in self.items:
            if not (item.get("url") or item.get("key")):
                continue
            for key in _source_keys(item):
                bucket = by_key.setdefault(key, [])
                if len(bucket) < 8:
                    bucket.append(item)
            fingerprint = str(item.get("fingerprint") or "").strip().lower()
            if fingerprint:
                by_fp.setdefault(fingerprint, []).append(item)
        return by_key, by_fp

    @staticmethod
    def _pick_source(row: dict, by_key, by_fp) -> dict | None:
        candidates: list[dict] = []
        fingerprint = row.get("_fingerprint")
        if fingerprint and fingerprint in by_fp:
            candidates = list(by_fp[fingerprint])
        else:
            for key in row.get("_keys") or ():
                candidates.extend(by_key.get(key, []))
        # 唯一且大小一致才算同一条视频；模糊时宁可不补地址，也不冒错配风险。
        confirmed = [item for item in candidates if _sizes_agree(row.get("_size"), _as_float(item.get("size_mb")))]
        unique = {id(item): item for item in confirmed}
        return next(iter(unique.values())) if len(unique) == 1 else None

    @staticmethod
    def _count_unlisted(video_dir: Path | None, rows: list[dict]) -> int | None:
        if video_dir is None:
            return None
        try:
            if not video_dir.is_dir():
                return None
            listed = {_basename(row["relative_path"]) for row in rows}
            files = {path.name.lower() for path in video_dir.iterdir() if path.is_file()}
        except OSError:
            return None
        return len(files - listed)

    # ------------------------------------------------------------------ 查询
    def public(self) -> list[dict]:
        return [{"id": str(t.get("id", "")), "name": str(t.get("name") or f"场景 {t.get('id', '')}"),
                 "shot": str(t.get("shot") or ""), "duration": t.get("duration"),
                 "size_mb": t.get("size_mb"), "tags": list(t.get("tags", [])),
                 "tag_categories": dict(t.get("tag_categories", {})),
                 "ready": bool(t.get("url") or t.get("key")),
                 "upload_state": str(t.get("upload_state") or "")} for t in self.items]

    def filter_items(self, tag_filters: dict | None = None) -> list[dict]:
        if not tag_filters:
            return list(self.items)
        if not isinstance(tag_filters, dict):
            raise TemplateError("标签筛选条件格式无效")
        selected = {}
        for category, values in tag_filters.items():
            if category not in TAG_CATEGORIES:
                raise TemplateError(f"未知标签分类：{category}")
            if not isinstance(values, (list, tuple)):
                raise TemplateError(f"{category} 筛选必须为标签列表")
            values = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
            if not values:
                continue
            allowed = set(self.tag_values.get(category, []))
            unknown = [value for value in values if value not in allowed]
            if unknown:
                raise TemplateError(f"{category} 中不存在标签：{unknown[0]}")
            selected[category] = set(values)
        if not selected:
            return list(self.items)
        result = []
        for item in self.items:
            item_cats = item.get("tag_categories", {})
            keep = True
            for category, values in selected.items():
                if not any(tag in values and item_cats.get(tag) == category for tag in item.get("tags", [])):
                    keep = False
                    break
            if keep:
                result.append(item)
        return result

    def choose(self, mode: str, count: int = 1, template_id: str = "", max_items: int = 20,
               tag_filters: dict | None = None) -> list[dict]:
        candidates = self.filter_items(tag_filters)
        if not candidates:
            raise TemplateError("当前标签筛选没有匹配的视频场景")
        mode = (mode or "random").lower()
        if mode == "selected":
            item = next((item for item in candidates if str(item.get("id")) == _normalize_id(template_id)), None)
            if not item:
                raise TemplateError("请选择符合当前标签筛选的视频场景")
            selected = [item]
        elif mode == "random":
            try:
                count = int(count)
            except (TypeError, ValueError):
                raise TemplateError("生成数量不正确") from None
            if not 1 <= count <= min(max_items, len(candidates)):
                raise TemplateError(f"当前筛选条件可生成 1–{min(max_items, len(candidates))} 条")
            selected = random.SystemRandom().sample(candidates, count)
        else:
            raise TemplateError("请选择自动推荐或手动挑选场景")
        return [dict(item) for item in selected]

    def preview(self, template_id: str) -> dict:
        item = self.by_id.get(_normalize_id(template_id))
        if not item:
            raise TemplateError("找不到该视频场景")
        return item
