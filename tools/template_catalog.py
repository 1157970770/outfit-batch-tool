#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机视频标签项目与 XLSX 交接清单。"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
FINGERPRINT_CHUNK = 1 << 20


def _probe_video(path: Path) -> dict:
    """本地读取时长；不导入生产应用模块。"""
    exe = shutil.which("ffprobe")
    if not exe:
        fallback = Path(r"D:\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")
        exe = str(fallback) if fallback.is_file() else ""
    if not exe:
        return {"warn": "未找到 ffprobe；标签仍可保存，时长留空"}
    try:
        result = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        duration = None
    return {"duration": round(duration, 2)} if duration and duration > 0 else {"warn": "无法读取视频时长"}


def _file_fingerprint(path: Path) -> str:
    """轻量指纹用于本机文件变更检测，不作为安全哈希。"""
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as stream:
        digest.update(stream.read(FINGERPRINT_CHUNK))
        if size > 2 * FINGERPRINT_CHUNK:
            stream.seek(-FINGERPRINT_CHUNK, 2)
            digest.update(stream.read(FINGERPRINT_CHUNK))
    return digest.hexdigest()[:16]

DEFAULT_TAGS = [
    "适合秋冬", "适合春夏", "近景", "中远景",
    "手串展示", "A场景", "B场景", "C场景",
]
TAG_CATEGORIES = ["景别", "适合季节", "场景", "姿势"]


def infer_tag_category(tag: str) -> str:
    name = str(tag or "").strip()
    if name in {"近景", "中景", "中远景", "远景", "特写"}:
        return "景别"
    if name in {"适合春夏", "适合秋冬", "适合春季", "适合夏季", "适合秋季", "适合冬季"}:
        return "适合季节"
    if name in {"手串展示", "只有上半身", "坐在椅子上", "裤子展示", "站立", "坐姿"}:
        return "姿势"
    return "场景"


def default_tag_categories(tags: list[str]) -> dict[str, str]:
    return {tag: infer_tag_category(tag) for tag in tags}


def normalize_tag_categories(tags: list[str], categories: dict | None = None,
                             allowed: list[str] | tuple[str, ...] | None = None) -> dict[str, str]:
    """把每个标签归到分类；`allowed` 给定时以它为准（支持自定义大类）。"""
    allowed_list = [str(value).strip() for value in (allowed or TAG_CATEGORIES) if str(value).strip()]
    result = default_tag_categories(tags)
    for tag in tags:
        category = (categories or {}).get(tag)
        if category in allowed_list:
            result[tag] = category
        elif result.get(tag) not in allowed_list:
            result[tag] = allowed_list[0] if allowed_list else "场景"
    return result


VIDEO_SHEET = "视频标签清单"
TAGS_SHEET = "标签字典"
PROJECT_SHEET = "项目"


class CatalogError(ValueError):
    pass


def library_path() -> Path:
    """全局标签库：跨文件夹、跨次启动保留。"""
    override = (os.getenv("OUTFIT_TAG_LIBRARY") or "").strip()
    if override:
        return Path(override)
    base = (os.getenv("APPDATA") or os.getenv("LOCALAPPDATA") or "").strip()
    if base:
        return Path(base) / "OutfitVideoTagger" / "tags.json"
    return Path.home() / ".outfit-video-tagger" / "tags.json"


def load_tag_library(path: str | Path | None = None) -> list[str]:
    """读取标签库；只有文件不存在/损坏时才用默认标签，已删除的默认标签不会复活。"""
    target = Path(path) if path else library_path()
    if not target.is_file():
        return list(DEFAULT_TAGS)
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return list(DEFAULT_TAGS)
    values = data.get("tags") if isinstance(data, dict) else data
    if not isinstance(values, list):
        return list(DEFAULT_TAGS)
    tags: list[str] = []
    for value in values:
        try:
            name = validate_tag_name(value)
        except CatalogError:
            continue
        if name.casefold() not in {tag.casefold() for tag in tags}:
            tags.append(name)
    return tags


def load_tag_categories(path: str | Path | None = None) -> dict[str, str]:
    target = Path(path) if path else library_path()
    tags = load_tag_library(target)
    saved = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("categories"), dict):
                saved = data["categories"]
        except (OSError, json.JSONDecodeError):
            pass
    return normalize_tag_categories(tags, saved)


def state_path() -> Path:
    """工作状态：上次项目、最近文件夹，供退出后继续。"""
    return library_path().with_name("state.json")


def load_state(path: str | Path | None = None) -> dict:
    target = Path(path) if path else state_path()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict, path: str | Path | None = None) -> Path:
    target = Path(path) if path else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def save_tag_library(tags: list[str], path: str | Path | None = None,
                     categories: dict[str, str] | None = None) -> Path:
    target = Path(path) if path else library_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if categories is None:
        categories = load_tag_categories(target)
    category_map = normalize_tag_categories(list(tags), categories)
    payload = {"version": 2, "tags": list(tags), "categories": category_map}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def add_library_tag(tags: list[str], value: str) -> str:
    name = validate_tag_name(value)
    if name.casefold() in {tag.casefold() for tag in tags}:
        raise CatalogError("这个标签已经存在")
    tags.append(name)
    return name


def new_project(root: str | Path, progress=None) -> dict:
    folder = Path(root).expanduser().resolve()
    if not folder.is_dir():
        raise CatalogError(f"视频文件夹不存在：{folder}")
    project = {
        "project_id": uuid.uuid4().hex[:12],
        "source_root": str(folder),
        "next_id": 1,
        "tags": [],
        "tag_categories": {},
        "videos": [],
    }
    return scan_folder(folder, project, progress=progress)


def _next_id(project: dict) -> int:
    numeric = [int(str(v.get("id"))) for v in project.get("videos", [])
               if str(v.get("id", "")).isdigit()]
    return max(int(project.get("next_id", 1)), max(numeric, default=0) + 1)


def scan_folder(root: str | Path, project: dict | None = None, progress=None) -> dict:
    folder = Path(root).expanduser().resolve()
    if not folder.is_dir():
        raise CatalogError(f"视频文件夹不存在：{folder}")
    result = dict(project or {})
    result.setdefault("project_id", uuid.uuid4().hex[:12])
    result.setdefault("tags", [])
    result["tag_categories"] = normalize_tag_categories(result.get("tags", []), result.get("tag_categories", {}))
    # 多文件夹：扫描新目录时把它的视频追加到现有清单末尾，旧目录的行原样保留。
    base_root = str(result.get("source_root") or folder)
    previous: dict[tuple[str, str], dict] = {}
    for row in result.get("videos", []):
        row.setdefault("source_dir", base_root)
        previous[(str(row.get("source_dir")), str(row.get("relative_path")))] = row
    result["source_root"] = str(folder)
    result.setdefault("roots", [])
    if str(folder) not in result["roots"]:
        result["roots"].append(str(folder))
    found: set[str] = set()
    rows: list[dict] = []
    appended = 0
    preexisting = bool(result.get("videos"))
    next_id = _next_id(result)
    paths = sorted((p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT),
                   key=lambda p: p.relative_to(folder).as_posix().casefold())
    total_paths = len(paths)
    for index, path in enumerate(paths, 1):
        if progress:
            progress(index - 1, total_paths, path.name)
        relative = path.relative_to(folder).as_posix()
        found.add(relative)
        old = previous.get((str(folder), relative))
        info = _probe_video(path)
        fingerprint = _file_fingerprint(path)
        if old:
            row = old
            old_fingerprint = str(row.get("fingerprint") or "")
            changed_now = bool(old_fingerprint and old_fingerprint != fingerprint)
            row["changed"] = bool(row.get("changed")) or changed_now
            if changed_now and not row.get("previous_fingerprint"):
                row["previous_fingerprint"] = old_fingerprint
            row["id"] = str(row.get("id") or f"{next_id:03d}")
        else:
            row = {"id": f"{next_id:03d}", "tags": [], "changed": False,
                   "source_dir": str(folder), "original_name": ""}
            if preexisting:
                appended += 1
            next_id += 1
        row.update({
            "source_dir": str(folder),
            "relative_path": relative,
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            "duration": info.get("duration"),
            "fingerprint": fingerprint,
            "missing": False,
            "validation_warning": info.get("warn", ""),
        })
        row["tags"] = normalize_tags(row.get("tags", []), result["tags"])
        rows.append(row)
        if progress:
            progress(index, total_paths, path.name)
    # 本次目录里消失的文件标记为缺失；其他目录的行原样保留，避免误删人工标签和 ID。
    for (row_dir, relative), old in previous.items():
        if row_dir == str(folder) and relative not in found:
            old["missing"] = True
            old["changed"] = False
            rows.append(old)
        elif row_dir != str(folder) and old not in rows:
            rows.append(old)
    rows.sort(key=_row_sort_key)
    result["videos"] = rows
    result["next_id"] = max(next_id, _next_id(result))
    result["appended"] = appended
    return result


def _row_sort_key(row: dict) -> tuple:
    """按模板ID数字排序，缺失行排在后面。"""
    key = str(row.get("id", ""))
    return (bool(row.get("missing", False)), int(key) if key.isdigit() else 0,
            str(row.get("source_dir", "")), str(row.get("relative_path", "")))


def validate_tag_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise CatalogError("标签名称不能为空")
    if len(name) > 64:
        raise CatalogError("标签名称最多 64 个字符")
    if any(ch in name for ch in ("|", ";", "\r", "\n")):
        raise CatalogError("标签不能包含分隔符 |、; 或换行")
    return name


def normalize_tags(values: Any, known: list[str]) -> list[str]:
    if isinstance(values, str):
        values = values.split(";") if values else []
    if not isinstance(values, (list, tuple, set)):
        return []
    allowed = set(known)
    out: list[str] = []
    for value in values:
        label = str(value).strip()
        if label in allowed and label not in out:
            out.append(label)
    return out


def add_tag(project: dict, value: str) -> str:
    name = validate_tag_name(value)
    tags = list(project.get("tags") or [])
    if name.casefold() in {tag.casefold() for tag in tags}:
        raise CatalogError("这个标签已经存在")
    tags.append(name)
    project["tags"] = tags
    return name


def set_video_tags(project: dict, key: str, selected: list[str]) -> None:
    """按模板ID（优先）或相对路径给某条视频设置标签。"""
    tags = list(project.get("tags") or [])
    for value in selected:
        if value not in tags:
            raise CatalogError(f"标签不存在：{value}")
    wanted = str(key)
    videos = project.get("videos", [])
    for row in videos:
        if str(row.get("id", "")) == wanted:
            row["tags"] = normalize_tags(selected, tags)
            return
    for row in videos:
        if row.get("relative_path") == key:
            row["tags"] = normalize_tags(selected, tags)
            return
    raise CatalogError("找不到指定视频")


def validate_project(project: dict, *, require_files: bool = True) -> list[str]:
    errors: list[str] = []
    root = Path(project.get("source_root") or "")
    ids: set[str] = set()
    paths: set[str] = set()
    known_tags = set(project.get("tags") or [])
    for row in project.get("videos", []):
        tid = str(row.get("id") or "")
        rel = str(row.get("relative_path") or "")
        if not tid.isdigit():
            errors.append(f"模板编号必须为数字：{tid or rel}")
        if not tid or tid in ids:
            errors.append(f"模板编号重复或为空：{tid or rel}")
        ids.add(tid)
        if not rel or rel in paths:
            errors.append(f"视频路径重复或为空：{rel}")
        paths.add(rel)
        if row.get("missing"):
            errors.append(f"本地视频不存在：{rel}")
            continue
        if row.get("changed"):
            errors.append(f"视频内容变化尚未确认：{rel}")
        source = root / Path(rel)
        if require_files and not source.is_file():
            errors.append(f"本地视频不存在：{rel}")
        elif require_files and row.get("fingerprint"):
            try:
                if _file_fingerprint(source) != row["fingerprint"]:
                    errors.append(f"视频内容在保存标签清单后发生变化：{rel}；请重新扫描并确认")
            except OSError:
                errors.append(f"无法读取视频：{rel}")
        row_tags = row.get("tags", [])
        if not row_tags:
            errors.append(f"尚未打标签：{rel}")
        unknown_tags = [tag for tag in row_tags if tag not in known_tags]
        if unknown_tags:
            errors.append(f"标签字典中不存在 {unknown_tags[0]}：{rel}")
    if not project.get("videos"):
        errors.append("项目中没有找到视频")
    return errors


def _iso_time(value: Any) -> str:
    """时间戳 → 清单里可读的时间文本；0/非法一律留空。"""
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _parse_time(value: Any) -> float:
    """清单里的时间文本 → 时间戳；兼容旧清单里的纯数字时间戳。"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def save_xlsx(project: dict, destination: str | Path) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise CatalogError("本机缺少 Excel 文件支持模块 openpyxl，请联系管理员部署") from None
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = VIDEO_SHEET
    headers = ["模板ID", "相对路径", "文件名", "时长(秒)", "大小(MB)", "文件指纹", "标签", "状态",
               "来源目录", "原始文件名", "上传时间", "最后修改时间"]
    ws.append(headers)
    for row in project.get("videos", []):
        if row.get("missing"):
            state = "文件不存在"
        elif row.get("changed"):
            state = "文件已变化"
        elif row.get("deprecated"):
            state = "已弃用"  # 网页/桌面显式弃用：与“标签为空”的被动废弃区分开，可恢复原标签
        elif row.get("uploaded"):
            state = "已上传"
        elif not row.get("tags"):
            state = "废弃"  # 标签列为空 = 不上传的废弃素材
        else:
            state = "待上传"
        ws.append([str(row.get("id", "")), str(row.get("relative_path", "")), str(row.get("name", "")),
                   row.get("duration"), row.get("size_mb"), row.get("fingerprint", ""),
                   ";".join(row.get("tags", [])), state,
                   str(row.get("source_dir") or project.get("source_root") or ""),
                   str(row.get("original_name") or ""),
                   _iso_time(row.get("uploaded_at")), _iso_time(row.get("touched_at"))])
    # 一律按文本写：标签/文件名以 "=" 开头时，openpyxl 默认会写成公式，回读就成了空值
    for cells in ws.iter_rows(min_row=2):
        for cell in cells:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    tags_ws = wb.create_sheet(TAGS_SHEET)
    tags_ws.append(["标签值", "分类"])
    order = [str(name) for name in (project.get("category_order") or []) if str(name)]
    for name in list(TAG_CATEGORIES) + [str(value or "").strip()
                                        for value in (project.get("tag_categories") or {}).values()]:
        if name and name not in order:
            order.append(name)
    categories = normalize_tag_categories(project.get("tags", []), project.get("tag_categories", {}),
                                          allowed=order)
    for tag in project.get("tags", []):
        tags_ws.append([tag, categories[tag]])
    for cells in tags_ws.iter_rows(min_row=2):
        for cell in cells:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    meta = wb.create_sheet(PROJECT_SHEET)
    meta.append(["项目字段", "值"])
    meta.append(["项目ID", str(project.get("project_id") or "")])
    meta.append(["本机视频文件夹", str(project.get("source_root") or "")])
    meta.append(["下一个编号", int(project.get("next_id") or 1)])
    meta.sheet_state = "hidden"
    for sheet in (ws, tags_ws):
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2768DF")
            cell.alignment = Alignment(vertical="center")
        for idx, column in enumerate(sheet.columns, 1):
            values = [len(str(c.value or "")) for c in list(column)[:300]]
            sheet.column_dimensions[get_column_letter(idx)].width = min(max(max(values, default=12) + 2, 12), 54)
    wb.save(path)
    return path


def load_xlsx(source: str | Path) -> dict:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise CatalogError("本机缺少 Excel 文件支持模块 openpyxl") from None
    path = Path(source)
    try:
        # data_only=False：标签/文件名以 "=" 开头时按原文读回来，不要指望 Excel 里存了缓存值
        wb = load_workbook(path, read_only=True, data_only=False)
    except Exception as exc:
        raise CatalogError(f"无法打开标签清单：{exc}") from None
    if VIDEO_SHEET not in wb.sheetnames or TAGS_SHEET not in wb.sheetnames:
        wb.close()
        raise CatalogError("Excel 缺少“视频标签清单”或“标签字典”工作表")
    tags_ws = wb[TAGS_SHEET]
    tags = []
    saved_categories = {}
    category_order = list(TAG_CATEGORIES)
    for row in tags_ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        tag = validate_tag_name(row[0])
        if tag not in tags:
            tags.append(tag)
        category = str(row[1] or "").strip() if len(row) > 1 else ""
        if category:
            saved_categories[tag] = category
            if category not in category_order:
                category_order.append(category)  # 自定义大类按出现顺序保留，不再折叠回内置四类
    tag_categories = normalize_tag_categories(tags, saved_categories, allowed=category_order)
    meta_values = {}
    if PROJECT_SHEET in wb.sheetnames:
        meta = wb[PROJECT_SHEET]
        for key, value in meta.iter_rows(min_row=2, values_only=True):
            if key:
                meta_values[str(key)] = value
    ws = wb[VIDEO_SHEET]
    header = [str(c or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    column = {name: idx for idx, name in enumerate(header)}
    required = ("模板ID", "相对路径", "文件名", "标签")
    if any(name not in column for name in required):
        wb.close()
        raise CatalogError("视频标签清单缺少必要列（模板ID、相对路径、文件名、标签）")
    videos = []
    for values in ws.iter_rows(min_row=2, values_only=True):
        tid = values[column["模板ID"]]
        relative = values[column["相对路径"]]
        if not tid or not relative:
            continue
        def get(name, default=None):
            idx = column.get(name)
            return values[idx] if idx is not None and idx < len(values) and values[idx] is not None else default
        rel = Path(str(relative).replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts:
            wb.close()
            raise CatalogError(f"清单包含越界相对路径：{relative}")
        raw_tags = [part.strip() for part in str(get("标签", "")).split(";") if part.strip()]
        unknown = [tag for tag in raw_tags if tag not in tags]
        if unknown:
            wb.close()
            raise CatalogError(f"标签字典中不存在“{unknown[0]}”（视频 {rel.as_posix()}）")
        videos.append({"id": str(tid).strip(), "relative_path": rel.as_posix(),
                       "name": str(get("文件名", rel.name)), "duration": get("时长(秒)"),
                       "size_mb": get("大小(MB)", 0), "fingerprint": str(get("文件指纹", "")),
                       "tags": normalize_tags(raw_tags, tags), "missing": get("状态") == "文件不存在",
                       "changed": get("状态") == "文件已变化",
                       # 显式弃用与“空标签的被动废弃”分开：老清单只写了“废弃”，
                       # 所以「废弃 + 有标签」也按显式弃用回读，避免丢掉可恢复性。
                       "deprecated": get("状态") == "已弃用" or (get("状态") == "废弃" and bool(raw_tags)),
                       "uploaded": get("状态") == "已上传",
                       "source_dir": str(get("来源目录", "") or ""),
                       "original_name": str(get("原始文件名", "") or ""),
                       "uploaded_at": _parse_time(get("上传时间")),
                       "touched_at": _parse_time(get("最后修改时间"))})
    wb.close()
    source_root = str(meta_values.get("本机视频文件夹") or path.parent)
    for video in videos:  # 老清单没有来源目录列：一律回落到项目文件夹
        if not video["source_dir"]:
            video["source_dir"] = source_root
    project_id = str(meta_values.get("项目ID") or uuid.uuid4().hex[:12])
    try:
        next_id = int(meta_values.get("下一个编号") or (max((int(v["id"]) for v in videos if v["id"].isdigit()), default=0) + 1))
    except (TypeError, ValueError):
        next_id = 1
    return {"project_id": project_id, "source_root": source_root, "next_id": next_id,
            "tags": tags, "tag_categories": tag_categories,
            "videos": videos, "xlsx_path": str(path),
            "roots": sorted({video["source_dir"] for video in videos if video["source_dir"]})}


def next_movie_number(project: dict) -> int:
    """清单里下一个可用的视频编号（文件名数字最大值 + 1）。"""
    numbers = []
    for row in project.get("videos", []):
        for raw in (row.get("name"), row.get("relative_path"), row.get("original_name")):
            stem = Path(str(raw or "")).stem
            if stem.isdigit():
                numbers.append(int(stem))
    return max(numbers, default=0) + 1


def pending_renames(project: dict, folder: str | Path) -> list[dict]:
    """待改名：来源目录==folder、已打标、未上传、还叫原名。"""
    target = _norm_dir(folder)
    result = []
    for row in project.get("videos", []):
        if row.get("missing") or row.get("uploaded") or row.get("original_name"):
            continue
        if not row.get("tags"):
            continue
        if _norm_dir(row.get("source_dir") or project.get("source_root")) != target:
            continue
        result.append(row)
    result.sort(key=lambda row: str(row.get("name", "")).casefold())
    return result


def rename_tagged_videos(project: dict, folder: str | Path, start: int | None = None) -> list[dict]:
    """按顺延编号重命名 folder 里待上传的已打标视频，返回 [{id, old, new, row}]。"""
    base = Path(str(folder))
    number = int(start or next_movie_number(project))
    done: list[dict] = []
    for row in pending_renames(project, folder):
        source = base / str(row.get("relative_path") or row.get("name") or "")
        if not source.is_file():
            continue
        suffix = source.suffix or ".mp4"
        while True:
            target_name = f"{number:04d}{suffix}"
            target = source.with_name(target_name)
            if not target.exists():
                break
            number += 1
        old_name = source.name
        try:
            source.rename(target)
        except OSError as exc:
            raise CatalogError(f"重命名失败：{old_name} → {target_name}（{exc}）") from None
        row["original_name"] = old_name
        row["name"] = target_name
        row["relative_path"] = target.relative_to(base).as_posix()
        row["size_bytes"] = target.stat().st_size
        row["size_mb"] = round(target.stat().st_size / 1024 / 1024, 2)
        row["fingerprint"] = _file_fingerprint(target)
        row.pop("previous_fingerprint", None)
        done.append({"id": row.get("id", ""), "old": old_name, "new": target_name, "row": row})
        number += 1
    if done:
        project["next_movie_number"] = number
    return done


def _norm_dir(value) -> str:
    try:
        return str(Path(str(value)).resolve()).casefold()
    except OSError:
        return str(value).casefold()
