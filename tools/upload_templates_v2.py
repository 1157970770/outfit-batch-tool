#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""模板上传 v2：编号与标签**一律以标签工作簿为准**（替代 tools/upload_templates.py）。

为什么要 v2
    旧脚本按「目录名推断标签 + 目录顺序 enumerate 编号」，产出 input/tpl/001.mp4；
    而标签工作簿里其实是两套互不相干的编号：模板ID=001 / 文件名=0001.mp4。
    两边 basename 永远对不上（001.mp4 ≠ 0001.mp4），于是 app/web/templates.py 的
    _pick_source（只按 basename + 指纹 join）一条也补不到 url —— 0 / 1263。

v2 的约定
    模板ID    ← 工作簿「模板ID」列，保留原始文本格式（如 001），只做清单标识
    对象名    ← 工作簿「文件名」列（如 0001.mp4），即 input/tpl/0001.mp4
    标签      ← 工作簿「标签」列（分号分隔），按「标签字典」归类后写入 COS 对象标签
    标签为空  ← 视为废弃素材：不上传、不写清单、不计入待办
    清单      ← 每次全量重建，不增量追加（避免已删素材在清单里残留）

COS 对象标签的硬限制（腾讯云文档 436/42993）
    标签键 1-127 个字符、标签值 1-255 个字符、单对象最多 10 个标签；
    字符集允许中文、a-z A-Z 0-9 与 + - _ = / . : @（本脚本额外容忍空格）。
    工作簿当前最多 7 个标签，加 3 个元数据标签刚好 10 个，逼近上限，故按「分类」合并：
    每个标签分类只占 1 个标签位，值用 / 连接，实际稳定在 7 个以内。

用法：
    python tools/upload_templates_v2.py --dry-run              # 只体检，不联网
    python tools/upload_templates_v2.py --dry-run --limit 5    # 只看前 5 条编号与标签
    python tools/upload_templates_v2.py --limit 5              # 真上传 5 条试水
    python tools/upload_templates_v2.py                        # 全量
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
MULTIPART_MIN = 8 * 1024 * 1024
BUCKET = "video-input-1463907213"
REGION = "ap-guangzhou"
PREFIX = "input/tpl"
BASE_URL = f"https://{BUCKET}.cos.{REGION}.myqcloud.com"

DEFAULT_WORKBOOK = r"C:\Users\EDY\Desktop\新标签清单.xlsx"
DEFAULT_VIDEO_DIR = r"D:\星伙素材\磊哥外拍视频（总）"
DEFAULT_SHEET = "视频标签清单"
DEFAULT_OUT = "build/manifest/manifest.json"

# COS 对象标签规格
TAG_KEY_MAX = 127
TAG_VALUE_MAX = 255
TAG_MAX_COUNT = 10
# 分类 → 标签键（用 ASCII 短名，避免 REST 头里出现中文键的编码争议；值仍是中文）
CATEGORY_SLUGS = {"景别": "jingbie", "适合季节": "jijie", "场景": "changjing", "姿势": "zishi"}
# 文档允许的字符集（中文 + ASCII 可见字符子集），额外容忍空格
TAG_CHAR_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9+\-_=/:\.@ ]*$")
# 模板ID / 文件名 的合法形态：只允许安全字符，挡掉 path traversal 与对象名注入
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
FILENAME_RE = re.compile(r"^[^\\/:*?\"<>|]{1,128}$")
# 绝对路径（盘符或根斜杠）：工作簿里的相对路径不该出现，出现就拒掉，防越出视频目录
ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:|/|\\)")


def _s(value) -> str:
    """单元格转干净字符串（openpyxl 可能给出 int / float / None）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _f(value):
    try:
        return float(_s(value))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- 工作簿
def read_workbook(path: Path, sheet_name: str) -> dict:
    """只读解析标签工作簿。返回 {rows, categories, video_dir}；行级问题进 errors。"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("主机缺少 openpyxl，无法读取标签工作簿（先 pip install openpyxl）")
    if not path.is_file():
        sys.exit(f"标签工作簿不存在：{path}")
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"无法读取标签工作簿：{exc}")

    try:
        if sheet_name not in workbook.sheetnames:
            sys.exit(f"工作簿里没有「{sheet_name}」工作表，实际有：{workbook.sheetnames}")
        sheet = workbook[sheet_name]
        header = [_s(v) for v in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
        columns = {name: i for i, name in enumerate(header)}
        required = ("模板ID", "相对路径", "文件名", "标签")
        missing_cols = [name for name in required if name not in columns]
        if missing_cols:
            sys.exit(f"工作簿缺少必需列：{'、'.join(missing_cols)}；实际表头：{header}")

        categories = _read_tag_dictionary(workbook)
        video_dir = _read_project_video_dir(workbook)

        def cell(values, name):
            index = columns.get(name)
            if index is None or index >= len(values):
                return None
            return values[index]

        rows: list[dict] = []
        errors: list[str] = []
        for row_no, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if not values:
                continue
            template_id = _s(cell(values, "模板ID"))
            if not template_id:
                continue  # 空行
            filename = _s(cell(values, "文件名"))
            relative = _s(cell(values, "相对路径")).replace("\\", "/")
            raw_tags = _s(cell(values, "标签"))

            if not ID_RE.match(template_id):
                errors.append(f"第 {row_no} 行 模板ID 含非法字符：{template_id!r}")
            if not filename or not FILENAME_RE.match(filename) or ".." in filename:
                errors.append(f"第 {row_no} 行 文件名为空或含非法字符/路径分隔符：{filename!r}")
            elif Path(filename).suffix.lower() not in VIDEO_EXT:
                errors.append(f"第 {row_no} 行 不是视频后缀：{filename!r}")

            tags = []
            for part in raw_tags.replace("；", ";").split(";"):
                text = part.strip()
                if text and text not in tags:
                    tags.append(text)
            unknown = [tag for tag in tags if categories and tag not in categories]
            if unknown:
                errors.append(f"第 {row_no} 行使用了标签字典外的值：{unknown[0]!r}")

            rows.append({
                "row_no": row_no,
                "id": template_id,
                "relative_path": relative or filename,
                "filename": filename,
                "size_mb": _f(cell(values, "大小(MB)")),
                "fingerprint": _s(cell(values, "文件指纹")).lower(),
                "duration": _f(cell(values, "时长(秒)")),
                "tags": tags,
                "status": _s(cell(values, "状态")),
            })
    finally:
        workbook.close()

    if errors:
        print(f"[错误] 工作簿数据校验失败 {len(errors)} 处，为免上传错素材，直接中止：")
        for line in errors[:20]:
            print(f"    {line}")
        if len(errors) > 20:
            print(f"    …（共 {len(errors)} 处）")
        sys.exit(2)
    return {"rows": rows, "categories": categories, "video_dir": video_dir}


def _read_tag_dictionary(workbook) -> dict[str, str]:
    """「标签字典」→ {标签值: 分类}；没有这张表就退化为空字典（不强制）。"""
    if "标签字典" not in workbook.sheetnames:
        return {}
    sheet = workbook["标签字典"]
    rows = sheet.iter_rows(values_only=True)
    header = [_s(v) for v in next(rows)]
    columns = {name: i for i, name in enumerate(header)}
    if "标签值" not in columns:
        return {}
    categories: dict[str, str] = {}
    for values in rows:
        if not values or columns["标签值"] >= len(values):
            continue
        tag = _s(values[columns["标签值"]])
        if not tag:
            continue
        category = ""
        if "分类" in columns and columns["分类"] < len(values):
            category = _s(values[columns["分类"]])
        categories[tag] = category or "其他"
    return categories


def _read_project_video_dir(workbook) -> Path | None:
    """「项目」表里的「本机视频文件夹」，作为 --video-dir 的默认值。"""
    if "项目" not in workbook.sheetnames:
        return None
    for values in workbook["项目"].iter_rows(values_only=True):
        if values and _s(values[0]) == "本机视频文件夹" and len(values) > 1:
            text = _s(values[1])
            return Path(text) if text else None
    return None


# --------------------------------------------------------------------- 体检
def audit(rows: list[dict], extra_files: set[str]) -> dict:
    """编号冲突 / 空洞 / 编号错位 / 目录孤儿文件，四项体检一次做完。"""
    # 1) 重复检查（全表口径，避免废弃行里的重复被漏看）
    seen_id: dict[str, list[str]] = {}
    seen_name: dict[str, list[str]] = {}
    for row in rows:
        seen_id.setdefault(row["id"], []).append(row["filename"])
        seen_name.setdefault(row["filename"].lower(), []).append(row["id"])
    dup_ids = {k: v for k, v in seen_id.items() if len(v) > 1}
    dup_names = {k: v for k, v in seen_name.items() if len(v) > 1}

    # 2) 编号空洞：连续号段里缺了哪些
    numeric = sorted(int(row["id"]) for row in rows if row["id"].isdigit())
    holes: list[int] = []
    if numeric:
        holes = sorted(set(range(numeric[0], numeric[-1] + 1)) - set(numeric))

    # 3) 模板ID 与文件名数字不一致的行（两套编号本来就独立，专门统计便于核对）
    mismatch = [(row["id"], row["filename"]) for row in rows
                if row["filename"].endswith(".mp4")
                and row["id"].isdigit()
                and row["filename"][:-4].isdigit()
                and int(row["id"]) != int(row["filename"][:-4])]

    return {
        "rows_total": len(rows),
        "dup_ids": {k: v for k, v in list(dup_ids.items())[:10]},
        "dup_ids_count": len(dup_ids),
        "dup_filenames": {k: v for k, v in list(dup_names.items())[:10]},
        "dup_filenames_count": len(dup_names),
        "id_min": numeric[0] if numeric else None,
        "id_max": numeric[-1] if numeric else None,
        "holes_count": len(holes),
        "holes_preview": holes[:20],
        "id_filename_mismatch_count": len(mismatch),
        "id_filename_mismatch_preview": mismatch[:10],
        "extra_files_in_dir": sorted(extra_files)[:20],
        "extra_files_count": len(extra_files),
    }


def resolve_source(video_dir: Path, row: dict) -> Path | None:
    """按工作簿找源文件；优先相对路径，再退文件名，最后不区分大小写匹配。"""
    for raw in (row["relative_path"], row["filename"]):
        text = str(raw or "").replace("\\", "/").strip()
        if not text or ".." in text.split("/") or ABS_PATH_RE.match(text):
            continue  # 空、上跳、绝对路径一律不认，只认视频目录下的相对路径
        path = (video_dir / text)
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    target = row["filename"].lower()
    try:
        for child in video_dir.iterdir():
            if child.is_file() and child.name.lower() == target:
                return child
    except OSError:
        return None
    return None


def duration_of(path: Path):
    """兜底用 ffprobe 读时长（工作簿已有时长时不必调用）。"""
    for exe in ("ffprobe", r"D:\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe"):
        try:
            out = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0 and out.stdout.strip():
                return round(float(out.stdout.strip()), 2)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


# --------------------------------------------------------------------- 标签
def shot_of(tags: list[str]) -> str:
    """景别标签 → 旧的 shot 字段值（near / medium_far / mixed），保持清单字段兼容。"""
    near = "近景" in tags
    far = "中远景" in tags
    if near and far:
        return "mixed"
    if near:
        return "near"
    if far:
        return "medium_far"
    return ""


def _clean_tag_text(text: str, issues: list[str], label: str) -> str:
    if TAG_CHAR_RE.match(text):
        return text
    bad = sorted({ch for ch in text if not TAG_CHAR_RE.match(ch)})
    issues.append(f"{label} 含 COS 标签不允许的字符 {bad}，已替换为 -")
    return "".join(ch if TAG_CHAR_RE.match(ch) else "-" for ch in text)


def build_tagset(item: dict, categories: dict[str, str], issues: list[str]) -> list[dict]:
    """把一条素材的标签拼成 COS TagSet，并就地校验长度/数量/字符集。"""
    tagset: list[dict] = [
        {"Key": "kind", "Value": "tpl"},
        {"Key": "tpl_id", "Value": item["id"]},
    ]
    shot = shot_of(item["tags"])
    if shot:
        tagset.append({"Key": "shot", "Value": shot})

    # 按分类合并：同一分类的多个标签用一个标签位，值用 / 连接
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for tag in item["tags"]:
        category = categories.get(tag) or "其他"
        if category not in grouped:
            grouped[category] = []
            order.append(category)
        grouped[category].append(tag)

    for index, category in enumerate(order):
        key = CATEGORY_SLUGS.get(category) or f"cat{index + 1}"
        value = "/".join(grouped[category])
        key = _clean_tag_text(key, issues, f"[{item['id']}] 标签键 {key}")
        value = _clean_tag_text(value, issues, f"[{item['id']}] 标签值 {value}")
        if len(key) > TAG_KEY_MAX:
            issues.append(f"[{item['id']}] 标签键超过 {TAG_KEY_MAX} 字符，已截断：{key}")
            key = key[:TAG_KEY_MAX]
        if len(value) > TAG_VALUE_MAX:
            issues.append(f"[{item['id']}] 标签值超过 {TAG_VALUE_MAX} 字符，已截断：{value}")
            value = value[:TAG_VALUE_MAX]
        tagset.append({"Key": key, "Value": value})

    # 单对象最多 10 个标签（COS 硬限制），超了丢弃靠后的分类并告警
    if len(tagset) > TAG_MAX_COUNT:
        dropped = tagset[TAG_MAX_COUNT:]
        issues.append(f"[{item['id']}] 标签数 {len(tagset)} 超过 COS 上限 {TAG_MAX_COUNT}，"
                      f"丢弃：{[t['Key'] for t in dropped]}")
        tagset = tagset[:TAG_MAX_COUNT]
    return tagset


# --------------------------------------------------------------------- COS
def build_client():
    """延迟导入 + 延迟加载凭证：--dry-run 全程不碰网络、不读密钥值。"""
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        sys.exit("缺少 cos-python-sdk-v5，无法真上传：\n"
                 "    pip install cos-python-sdk-v5\n"
                 "（--dry-run 不需要它，可先跑体检）")

    cred = Path.home() / ".cos" / "credentials.json"
    if not cred.exists():
        sys.exit(f"缺少管理员凭证：{cred}")
    data = json.loads(cred.read_text(encoding="utf-8-sig"))
    secret_id, secret_key = data.get("secret_id"), data.get("secret_key")
    if not secret_id or not secret_key:
        sys.exit(f"凭证文件缺 secret_id / secret_key：{cred}")
    # STS 临时凭证必须带 token，否则签名无效；永久密钥的 token 为空
    token = data.get("token") or None
    config = CosConfig(Region=REGION, SecretId=secret_id, SecretKey=secret_key, Token=token)
    print(f"[凭证] 已从 {cred} 读取（{('STS 临时凭证' if token else '永久密钥')}，值不打印）")
    return CosS3Client(config)


def upload_one(client, item: dict, key: str, force: bool, counters: dict) -> bool:
    """上传单个对象并设 ACL + 标签；返回是否最终就绪。幂等：大小一致就跳过上传。"""
    size = item["size"]
    need = True
    if not force:
        try:
            head = client.head_object(Bucket=BUCKET, Key=key)
            need = int(head.get("Content-Length") or 0) != size
        except Exception as exc:  # noqa: BLE001 - 不存在就走上传，其它异常只告警不中断
            need = True
            if "404" not in str(exc) and "NoSuchKey" not in str(exc):
                counters["head_errors"] += 1
                print(f"  [{item['id']}] HEAD 失败（按需上传处理）：{str(exc)[:80]}")
    if need:
        try:
            if size >= MULTIPART_MIN:
                client.upload_file(Bucket=BUCKET, Key=key, LocalFilePath=str(item["src"]),
                                   PartSize=10, MAXThread=5, EnableMD5=False)
            else:
                client.put_object(Bucket=BUCKET, Key=key, Body=item["src"].read_bytes(),
                                  ContentType="video/mp4")
        except Exception as exc:  # noqa: BLE001
            counters["failed"] += 1
            print(f"  [{item['id']}] 上传失败 {key}：{str(exc)[:100]}")
            return False
        counters["uploaded"] += 1
    else:
        counters["skipped"] += 1

    # 模型要匿名直链拉取，对象必须公有读（桶策略暂无权限，只能逐对象设）
    try:
        client.put_object_acl(Bucket=BUCKET, Key=key, ACL="public-read")
    except Exception as exc:  # noqa: BLE001
        counters["acl_failed"] += 1
        print(f"  [{item['id']}] 设公有读失败：{str(exc)[:100]}")

    # 标签放最后：标签成功即代表对象已完全就绪
    try:
        client.put_object_tagging(Bucket=BUCKET, Key=key, Tagging={
            "TagSet": {"Tag": item["tagset"]}})
    except Exception as exc:  # noqa: BLE001
        counters["tag_failed"] += 1
        print(f"  [{item['id']}] 打标签失败：{str(exc)[:100]}")
    return True


# --------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="模板上传 v2（工作簿驱动）+ 打标签 + 生成清单")
    ap.add_argument("--workbook", default=DEFAULT_WORKBOOK, help="标签工作簿路径")
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help="工作簿里的视频标签工作表名")
    ap.add_argument("--video-dir", default="", help=f"视频所在目录（默认取工作簿「项目」表，缺省 {DEFAULT_VIDEO_DIR}）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="清单输出路径")
    ap.add_argument("--report", default="", help="把体检/上传结果写到这个 JSON 便于留证")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用；不会覆盖正式清单）")
    ap.add_argument("--force", action="store_true", help="忽略已存在对象，强制重传")
    ap.add_argument("--probe-duration", action="store_true", help="工作簿缺时长时用 ffprobe 兜底")
    ap.add_argument("--dry-run", action="store_true", help="只体检与打印计划，不联网、不上传")
    args = ap.parse_args()

    workbook_path = Path(args.workbook)
    parsed = read_workbook(workbook_path, args.sheet)
    rows = parsed["rows"]
    categories = parsed["categories"]

    video_dir = Path(args.video_dir) if args.video_dir else (
        parsed["video_dir"] or Path(DEFAULT_VIDEO_DIR))
    if not video_dir.is_dir():
        sys.exit(f"视频目录不存在：{video_dir}")

    print(f"[工作簿] {workbook_path}")
    print(f"[视频源] {video_dir}")
    print(f"[行数] 共 {len(rows)} 行 | 标签字典 {len(categories)} 个值")

    # 废弃 = 标签列为空：不上传、不写清单
    deprecated = [row for row in rows if not row["tags"]]
    candidates = [row for row in rows if row["tags"]]
    print(f"[筛选] 上标 {len(candidates)} 条 | 废弃（标签为空）{len(deprecated)} 条")

    # 目录里有、工作簿里没登记的视频（多为废弃素材或未入库的新片）
    listed = {row["filename"].lower() for row in rows}
    try:
        dir_files = {p.name.lower() for p in video_dir.iterdir()
                     if p.is_file() and p.suffix.lower() in VIDEO_EXT}
    except OSError as exc:
        sys.exit(f"无法读取视频目录：{exc}")
    extra_files = dir_files - listed

    audit_result = audit(rows, extra_files)

    # 逐条落地：找源文件、量真实大小、拼标签
    items: list[dict] = []
    missing: list[dict] = []
    size_mismatch: list[dict] = []
    issues: list[str] = []
    for row in candidates:
        src = resolve_source(video_dir, row)
        if src is None:
            missing.append(row)
            continue
        size = src.stat().st_size
        real_mb = round(size / 1024 / 1024, 2)
        # 清单里写真实大小；同时比对工作簿大小，超容差会导致 _pick_source 匹配不上
        if row["size_mb"] is not None:
            tolerance = max(0.05, row["size_mb"] * 0.01)
            if abs(real_mb - row["size_mb"]) > tolerance:
                size_mismatch.append({
                    "id": row["id"], "filename": row["filename"],
                    "workbook_mb": row["size_mb"], "real_mb": real_mb,
                    "diff_mb": round(abs(real_mb - row["size_mb"]), 3),
                })
        duration = row["duration"]
        if duration is None and args.probe_duration:
            duration = duration_of(src)
        item = {
            "id": row["id"],
            "filename": row["filename"],
            "relative_path": row["relative_path"],
            "fingerprint": row["fingerprint"],
            "tags": row["tags"],
            "duration": duration,
            "shot": shot_of(row["tags"]),
            "src": src,
            "size": size,
            "size_mb": real_mb,
            "key": f"{PREFIX}/{row['filename']}",
        }
        item["tagset"] = build_tagset(item, categories, issues)
        items.append(item)

    if args.limit:
        items = items[: args.limit]

    # ---------------- 体检报告
    print()
    print("=" * 68)
    print("[体检 1] 计划上传 / 跳过废弃")
    print(f"    计划上传：{len(items)} 条（上标 {len(candidates)} 条，--limit {args.limit or '不限'}）")
    print(f"    跳过废弃：{len(deprecated)} 条（标签列为空，不上传、不写清单）")

    print("[体检 2] 编号冲突")
    print(f"    模板ID 重复：{audit_result['dup_ids_count']} 组 {audit_result['dup_ids'] or ''}")
    print(f"    文件名 重复：{audit_result['dup_filenames_count']} 组 {audit_result['dup_filenames'] or ''}")
    print(f"    编号空洞：{audit_result['holes_count']} 个（范围 {audit_result['id_min']}-{audit_result['id_max']}，"
          f"前 20：{audit_result['holes_preview']}）")
    print(f"    模板ID≠文件名数字：{audit_result['id_filename_mismatch_count']} 条"
          f"（两套编号本就独立，对象名一律取文件名；前 10：{audit_result['id_filename_mismatch_preview']}）")

    print("[体检 3] 文件存在性")
    print(f"    上标行找不到源文件：{len(missing)} 条")
    for row in missing[:10]:
        print(f"        缺 id={row['id']}  文件名={row['filename']}  相对路径={row['relative_path']}")
    print(f"    目录里有、工作簿没登记：{audit_result['extra_files_count']} 个 {audit_result['extra_files_in_dir']}")

    print("[体检 4] 大小一致性（工作簿 大小(MB) vs 实际文件）")
    print(f"    超 1% 容差的：{len(size_mismatch)} 条"
          + ("（容差内，_pick_source 能正常命中）" if not size_mismatch else "（这些行会 join 不上，需改工作簿）"))
    for bad in sorted(size_mismatch, key=lambda x: -x["diff_mb"])[:5]:
        print(f"        偏差 id={bad['id']} {bad['filename']} "
              f"工作簿={bad['workbook_mb']} 实际={bad['real_mb']} 差={bad['diff_mb']}MB")

    print("[体检 5] 对象标签（COS 限制：键≤127字符 值≤255字符 每对象≤10个）")
    max_tags = max((len(it["tagset"]) for it in items), default=0)
    print(f"    每条标签数：最多 {max_tags} 个（上限 {TAG_MAX_COUNT}）")
    print(f"    标签规则告警：{len(issues)} 条")
    for line in issues[:10]:
        print(f"        {line}")
    if items:
        sample = items[0]
        print(f"    样例 id={sample['id']} key={sample['key']}")
        for tag in sample["tagset"]:
            print(f"        {tag['Key']} = {tag['Value']}")

    report = {
        "mode": "dry-run" if args.dry_run else "upload",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "workbook": str(workbook_path),
        "video_dir": str(video_dir),
        "planned_upload": len(items),
        "tagged_rows": len(candidates),
        "deprecated_skipped": len(deprecated),
        "missing_files": len(missing),
        "missing_preview": [{"id": r["id"], "filename": r["filename"]} for r in missing[:10]],
        "size_mismatch": len(size_mismatch),
        "size_mismatch_top5": sorted(size_mismatch, key=lambda x: -x["diff_mb"])[:5],
        "tag_issues": len(issues),
        "tag_issues_preview": issues[:20],
        "max_tags_per_object": max_tags,
        "audit": audit_result,
    }

    print("=" * 68)

    if args.dry_run:
        if args.report:
            out = Path(args.report)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[报告] 已写入 {out}")
        print("[dry-run] 未创建 COS 客户端、未发起任何网络请求、未写入任何对象。结束。")
        return 0

    # ---------------- 真上传
    client = build_client()
    counters = {"uploaded": 0, "skipped": 0, "failed": 0,
                "tag_failed": 0, "acl_failed": 0, "head_errors": 0}
    entries: list[dict] = []
    total = len(items)
    for index, item in enumerate(items, start=1):
        key = item["key"]
        if not upload_one(client, item, key, args.force, counters):
            continue
        entries.append({
            "id": item["id"],
            "name": item["filename"],
            "filename": item["filename"],
            "relative_path": item["relative_path"],
            "size_mb": item["size_mb"],
            "fingerprint": item["fingerprint"],
            "duration": item["duration"],
            "shot": item["shot"],
            "tags": item["tags"],
            "key": key,
            "url": f"{BASE_URL}/{key}",
        })
        if index % 25 == 0 or index == total:
            print(f"  …进度 {index}/{total}（上传 {counters['uploaded']} 跳过 {counters['skipped']} "
                  f"失败 {counters['failed']}）")

    if not entries:
        print("[结果] 没有任何对象就绪，拒绝写出空清单")
        return 4

    # 全量重建：直接覆盖，不追加；--limit 时改写到旁路文件，避免污染正式清单
    out = Path(args.out)
    full_run = not args.limit or args.limit >= len(candidates)
    if not full_run:
        out = out.with_name(f"{out.stem}.limit{args.limit}{out.suffix}")
        print(f"[提醒] 本次是 --limit 局部运行，清单写到 {out}，正式清单 {args.out} 保持不变")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "count": len(entries),
        "prefix": PREFIX,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "workbook": str(workbook_path),
        "video_dir": str(video_dir),
        "deprecated_skipped": len(deprecated),
        "templates": entries,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[清单] 已写入 {out}（{len(entries)} 条，全量重建）")
    print(f"[结果] 上传 {counters['uploaded']} / 跳过 {counters['skipped']} / 失败 {counters['failed']} "
          f"| 打标签失败 {counters['tag_failed']} / 公有读失败 {counters['acl_failed']}")

    report.update({
        "counters": counters, "manifest": str(out), "manifest_entries": len(entries),
        "planned_upload": total,
    })
    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[报告] 已写入 {rp}")
    return 0 if counters["failed"] == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
