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
import re
import urllib.parse
from pathlib import Path


TAG_CATEGORIES = ["景别", "适合季节", "场景", "姿势"]
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


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


def _store_upload_state(video: dict, tags: list[str]) -> str:
    """与 template_catalog.save_xlsx 的状态列口径一致，便于导出后回读。"""
    if video.get("missing"):
        return "文件不存在"
    if video.get("changed"):
        return "文件已变化"
    if video.get("deprecated"):
        return "已弃用"
    if video.get("uploaded"):
        return "已上传"
    if not tags:
        return "废弃"
    return "待上传"


class TemplateCatalog:
    def __init__(self, templates: list[dict]):
        self.items = [dict(item) for item in templates]
        self.retired_by_id: dict[str, dict] = {}
        self.untagged_by_id: dict[str, dict] = {}
        self._reindex()
        self.tag_categories = list(TAG_CATEGORIES)
        self.tag_values: dict[str, list[str]] = {category: [] for category in TAG_CATEGORIES}
        self.tag_workbook_info: dict = {}
        self._collect_tag_values()

    def _collect_tag_values(self):
        self.tag_values = {category: [] for category in self.tag_categories}
        for item in self.items:
            categories = item.get("tag_categories", {})
            for tag in item.get("tags", []):
                category = categories.get(tag, _infer_category(tag))
                if category not in self.tag_values:
                    # 标签引用了清单里没登记的大类：补进来，别让标签在界面上消失
                    self.tag_categories.append(category)
                    self.tag_values[category] = []
                if tag not in self.tag_values[category]:
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
            rows, untagged, listed = self._read_tag_rows(workbook["视频标签清单"], categories)
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
            "untagged_skipped": untagged,
            "unlisted_files": self._count_unlisted(video_dir, listed),
        }
        return dict(self.tag_workbook_info)

    @staticmethod
    def _read_tag_dictionary(sheet) -> dict[str, str]:
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            raise TemplateError("标签字典是空表：请用 --export-tags 导出的模板填写")
        header = [str(value or "").strip() for value in header_row]
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
    def _read_tag_rows(sheet, categories: dict[str, str]) -> tuple[list[dict], int, set[str]]:
        """返回 (可参与筛选的行, 被排除的废弃行数, 清单里出现过的全部文件名)。

        约定：**没打标签的行就是废弃素材**，直接排除，不进候选集、不提示、不当作待办。
        仅当整张表一行可用的都没有时才报错（说明清单本身没打标）。
        第三个返回值用于统计"目录里有、清单里没有"的文件，不能被排除项污染。
        """
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            raise TemplateError("视频标签清单是空表：请用 --export-tags 导出的模板填写")
        header = [str(value or "").strip() for value in header_row]
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
        untagged = 0
        listed: set[str] = set()
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
            listed |= {_basename(relative), _basename(filename)} - {""}
            raw_tags = str(cell(values, "标签") or "")
            tags = list(dict.fromkeys(part.strip() for part in raw_tags.replace("；", ";").split(";") if part.strip()))
            unknown = [tag for tag in tags if tag not in categories]
            if unknown:
                raise TemplateError(f"视频 {relative} 使用了标签字典外的值：{unknown[0]}")
            if not tags:
                untagged += 1
                seen[template_id] = -1
                continue
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
            raise TemplateError(
                f"视频标签清单里没有可用行（{untagged} 行都没有标签），请先在打标工具里完成标注")
        return rows, untagged, listed

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
    def _count_unlisted(video_dir: Path | None, listed: set[str]) -> int | None:
        """统计目录里有、清单里没有的视频文件数（只看视频，忽略 csv/记录文件等）。"""
        if video_dir is None:
            return None
        try:
            if not video_dir.is_dir():
                return None
            files = {path.name.lower() for path in video_dir.iterdir()
                     if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES}
        except OSError:
            return None
        return len(files - listed)

    # ------------------------------------------------------------------ 标签真源
    def attach_tag_store(self, store) -> dict:
        """以服务端 JSON 真源重建候选集（口径与 attach_tag_workbook 完全一致）。

        约定不变：**没打标签的行不进候选集**；`deprecated`（显式弃用）也不进，但单独存进
        `retired_by_id` 供标签页预览/恢复。云端地址仍按「文件名/相对路径/指纹 + 大小一致」补，
        **绝不按模板 ID 匹配**。
        """
        categories = store.categories()
        self.tag_categories = list(store.category_list()) if hasattr(store, "category_list") else list(TAG_CATEGORIES)
        source_index, fp_index = self._build_source_index()
        manifest_count = len(self.items)
        rows: list[dict] = []
        retired: dict[str, dict] = {}
        untagged_rows: dict[str, dict] = {}
        untagged = 0
        deprecated = 0
        for video in store.rows(include_deprecated=True):
            template_id = _normalize_id(video.get("id"))
            tags = [tag for tag in (video.get("tags") or []) if tag in categories]
            size = _as_float(video.get("size_mb"))
            row = {
                "id": template_id,
                "name": str(video.get("name") or f"场景 {template_id}"),
                "filename": str(video.get("name") or ""),
                "shot": "",
                "relative_path": str(video.get("relative_path") or "").replace("\\", "/"),
                "tags": tags,
                "tag_categories": {tag: categories[tag] for tag in tags},
                "upload_state": _store_upload_state(video, tags),
                "duration": _as_float(video.get("duration")),
                "size_mb": size,
                "deprecated": bool(video.get("deprecated")),
                "deprecated_at": video.get("deprecated_at"),
                "deprecated_by": str(video.get("deprecated_by") or ""),
                "deprecated_reason": str(video.get("deprecated_reason") or ""),
                "missing": bool(video.get("missing")),
                "changed": bool(video.get("changed")),
                "touched_at": video.get("touched_at"),
                "touched_by": str(video.get("touched_by") or ""),
                "uploaded_at": float(video.get("uploaded_at") or 0),
                "rating": int(video.get("rating") or 0),
                "_keys": {_basename(row_path) for row_path in
                          (str(video.get("relative_path") or ""), str(video.get("name") or ""))} - {""},
                "_size": size,
                "_fingerprint": str(video.get("fingerprint") or "").strip().lower(),
            }
            source = self._pick_source(row, source_index, fp_index)
            row["url"] = str((source or {}).get("url") or "")
            row["key"] = str((source or {}).get("key") or "")
            for field in ("_keys", "_size", "_fingerprint"):
                row.pop(field, None)
            if row["deprecated"]:
                retired[template_id] = row
                deprecated += 1
                continue
            if not tags:
                # 未打标素材：不进候选集，但标签页要能列出来补标签
                untagged_rows[template_id] = row
                untagged += 1
                continue
            rows.append(row)

        ready = sum(1 for row in rows if row.get("url") or row.get("key"))
        self.items = rows
        self.retired_by_id = retired
        self.untagged_by_id = untagged_rows
        self._reindex()
        self._collect_tag_values()
        self.tag_workbook_info = {
            "path": str(getattr(store, "path", "") or ""),
            "source": "store",
            "workbook_rows": len(rows),
            "matched_templates": len(rows),
            "source_ready_templates": ready,
            "manifest_entries": manifest_count,
            "manifest_unmatched": manifest_count - ready,
            "untagged_skipped": untagged,
            "deprecated_skipped": deprecated,
            "unlisted_files": None,
            "store_revision": int(getattr(store, "revision", 0) or 0),
            "all_untagged": not rows,
        }
        return dict(self.tag_workbook_info)

    def resolve(self, template_id: str, *, include_retired: bool = False) -> dict:
        """取模板；include_retired=True 时已弃用/未打标的也能取到（标签页预览用）。"""
        key = _normalize_id(template_id)
        item = self.by_id.get(key)
        if item:
            return item
        if include_retired:
            for bucket in (self.retired_by_id, self.untagged_by_id):
                row = bucket.get(key)
                if row:
                    return row
        raise TemplateError("找不到该视频场景")

    def public_tags(self) -> list[dict]:
        """标签页专用：在用 + 已弃用全量，带弃用信息与 ready。"""
        def payload(item: dict) -> dict:
            return {
                "id": str(item.get("id", "")),
                "name": str(item.get("name") or ""),
                "filename": str(item.get("filename") or ""),
                "relative_path": str(item.get("relative_path") or ""),
                "tags": list(item.get("tags", [])),
                "tag_categories": dict(item.get("tag_categories", {})),
                "deprecated": bool(item.get("deprecated")),
                "deprecated_at": item.get("deprecated_at"),
                "deprecated_by": str(item.get("deprecated_by") or ""),
                "ready": bool(item.get("url") or item.get("key")),
                "upload_state": str(item.get("upload_state") or ""),
                "duration": item.get("duration"),
                "size_mb": item.get("size_mb"),
                "missing": bool(item.get("missing")),
                "changed": bool(item.get("changed")),
                "touched_at": item.get("touched_at"),
                "touched_by": str(item.get("touched_by") or ""),
                "uploaded_at": float(item.get("uploaded_at") or 0),
                "rating": int(item.get("rating") or 0),
            }

        rows = [payload(item) for item in self.items]
        retired = sorted((payload(item) for item in self.retired_by_id.values()),
                         key=lambda row: int(row["id"]) if row["id"].isdigit() else 0)
        untagged = sorted((payload(item) for item in self.untagged_by_id.values()),
                          key=lambda row: int(row["id"]) if row["id"].isdigit() else 0)
        return rows + untagged + retired

    # ------------------------------------------------------------------ 查询
    def public(self) -> list[dict]:
        return [{"id": str(t.get("id", "")), "name": str(t.get("name") or f"场景 {t.get('id', '')}"),
                 "shot": str(t.get("shot") or ""), "duration": t.get("duration"),
                 "size_mb": t.get("size_mb"), "tags": list(t.get("tags", [])),
                 "tag_categories": dict(t.get("tag_categories", {})),
                 "ready": bool(t.get("url") or t.get("key")),
                 "upload_state": str(t.get("upload_state") or "")} for t in self.items]

    def _normalize_tag_selection(self, raw: dict | None, *, what: str) -> dict[str, set[str]]:
        """把 {分类: [标签]} 校验成 {分类: set(标签)}，空分类忽略。

        what 只用于错误文案：包含条件报「筛选」，排除条件报「排除」。
        """
        if not raw:
            return {}
        if not isinstance(raw, dict):
            raise TemplateError(f"标签{what}条件格式无效")
        selected: dict[str, set[str]] = {}
        for category, values in raw.items():
            if category not in self.tag_categories:
                raise TemplateError(f"未知标签分类：{category}")
            if not isinstance(values, (list, tuple)):
                raise TemplateError(f"{category} {what}必须为标签列表")
            values = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
            if not values:
                continue
            allowed = set(self.tag_values.get(category, []))
            unknown = [value for value in values if value not in allowed]
            if unknown:
                raise TemplateError(f"{category} 中不存在标签：{unknown[0]}")
            selected[category] = set(values)
        return selected

    @staticmethod
    def _matches_include(item: dict, selected: dict[str, set[str]]) -> bool:
        """包含条件：同一大类内命中任一标签即可（OR），跨大类必须全部命中（AND）。"""
        item_cats = item.get("tag_categories", {})
        for category, values in selected.items():
            if not any(tag in values and item_cats.get(tag) == category for tag in item.get("tags", [])):
                return False
        return True

    def filter_items(self, tag_filters: dict | None = None,
                     tag_excludes: dict | None = None) -> list[dict]:
        """按标签筛选候选。

        - tag_filters：同类 OR、跨类 AND（原有语义）；
        - tag_excludes：黑名单。模板只要命中**任一**排除标签就剔除，不再分大类；
          同一标签同时出现在包含与排除里时，排除优先（黑名单永远赢）。
        """
        include = self._normalize_tag_selection(tag_filters, what="筛选")
        exclude = self._normalize_tag_selection(tag_excludes, what="排除")
        if not include and not exclude:
            return list(self.items)
        excluded_tags = set().union(*exclude.values()) if exclude else set()
        result = []
        for item in self.items:
            if excluded_tags and set(item.get("tags") or []) & excluded_tags:
                continue
            if include and not self._matches_include(item, include):
                continue
            result.append(item)
        return result

    def choose(self, mode: str, count: int = 1, template_id: str = "", max_items: int = 20,
               tag_filters: dict | None = None) -> list[dict]:
        candidates = self.filter_items(tag_filters)
        if not candidates:
            raise TemplateError("当前标签筛选没有匹配的视频场景")
        mode = (mode or "random").lower()
        if mode == "selected":
            # template_id 支持逗号/分号/空白分隔的多个编号：小批量「我来挑选」
            # 现在允许一次点选最多 3 个场景（前端限制 3，这里按 max_items 兜底）。
            raw_ids = re.split(r"[,;，；\s]+", str(template_id or ""))
            wanted_ids: list[str] = []
            for raw in raw_ids:
                tid = _normalize_id(raw)
                if tid and tid not in wanted_ids:
                    wanted_ids.append(tid)
            if not wanted_ids:
                raise TemplateError("请选择符合当前标签筛选的视频场景")
            cap = max(1, min(int(max_items), len(candidates)))
            if len(wanted_ids) > cap:
                raise TemplateError(f"一次最多挑选 {cap} 个视频场景")
            by_id = {str(item.get("id")): item for item in candidates}
            missing = next((tid for tid in wanted_ids if tid not in by_id), None)
            if missing:
                raise TemplateError(f"场景 {missing} 不在当前标签筛选结果里，请调整筛选后重选")
            selected = [dict(by_id[tid]) for tid in wanted_ids]
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

    def pick_by_ids(self, ids: list[str], *, require_ready: bool = True,
                    max_items: int | None = None) -> list[dict]:
        """按 ID 列表取模板（保持入参顺序、自动去重），供批次锁定清单使用。

        校验口径与 `choose` 一致：ID 必须存在、默认必须已上传云端（有 url 或 key）。
        `require_ready=False` 时只保证 ID 存在，让调用方自己处理「模板已下架」。
        """
        if not isinstance(ids, (list, tuple)) or not ids:
            raise TemplateError("清单不能为空")
        ordered: list[str] = []
        for raw in ids:
            template_id = _normalize_id(raw)
            if template_id and template_id not in ordered:
                ordered.append(template_id)
        if max_items is not None and len(ordered) > int(max_items):
            raise TemplateError(f"清单最多 {int(max_items)} 条")
        chosen: list[dict] = []
        pending: list[str] = []
        for template_id in ordered:
            item = self.by_id.get(template_id)
            if not item:
                raise TemplateError(f"模板 {template_id} 不在当前视频场景清单里")
            if require_ready and not (item.get("url") or item.get("key")):
                pending.append(template_id)
            chosen.append(dict(item))
        if pending:
            raise TemplateError(
                f"所选视频尚未上传到云端模板库（例如 {pending[0]}，共 {len(pending)} 条），"
                "先在管理页上传并重载模板清单后再锁定。")
        return chosen

    def preview(self, template_id: str) -> dict:
        item = self.by_id.get(_normalize_id(template_id))
        if not item:
            raise TemplateError("找不到该视频场景")
        return item
