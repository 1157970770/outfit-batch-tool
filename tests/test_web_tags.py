#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页生产标签筛选测试：仅使用本地 mock 数据，不调用 Wan 或 COS。

覆盖的核心约定：
1. 标签工作簿是生产清单，候选集只来自它；manifest 缺条目不报错。
2. manifest 只按文件名/指纹 + 大小一致性补 url/key，**绝不按模板 ID 硬配**。
3. 同类别 OR、跨类别 AND；手选模式必须服从筛选。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook  # noqa: E402
from app.web.settings import load_settings  # noqa: E402
from app.web.templates import TemplateCatalog, TemplateError  # noqa: E402

TAG_DICT = [("近景", "景别"), ("中远景", "景别"), ("适合秋冬", "适合季节"), ("适合春夏", "适合季节")]


def write_workbook(path: Path, rows, *, video_dir: Path | None = None):
    wb = Workbook()
    ws = wb.active
    ws.title = "视频标签清单"
    ws.append(["模板ID", "相对路径", "文件名", "时长(秒)", "大小(MB)", "文件指纹", "标签", "状态"])
    for row in rows:
        ws.append(row)
    tags = wb.create_sheet("标签字典")
    tags.append(["标签值", "分类"])
    for tag, category in TAG_DICT:
        tags.append([tag, category])
    meta = wb.create_sheet("项目")
    meta.append(["项目字段", "值"])
    meta.append(["本机视频文件夹", str(video_dir or path.parent / "videos")])
    meta.sheet_state = "hidden"
    wb.save(path)
    wb.close()


def build_env(tmp: Path, *, manifest: dict | None, rows, video_dir: Path | None = None):
    workbook = tmp / "标签清单.xlsx"
    write_workbook(workbook, rows, video_dir=video_dir)
    web_file = tmp / "web.json"
    web_file.write_text(json.dumps({"session_secret": "test-secret", "users": {"admin": {"role": "admin"}},
                                    "data_root": str(tmp / "web-data"), "tag_workbook": str(workbook)}),
                        encoding="utf-8")
    config = {"wan": {"provider": "dashscope", "api_key": "mock-key", "model": "wan3.0-video"},
              "cos": {"enabled": False}}
    if manifest is not None:
        manifest_path = tmp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        config["manifest_url"] = str(manifest_path)
    api_file = tmp / "config.json"
    api_file.write_text(json.dumps(config), encoding="utf-8")
    return load_settings(base_dir=tmp, web_file=web_file, api_file=api_file)


def main() -> int:
    tmp = tempdir("webtags-")
    # manifest 用"文件名 + 大小"才能对上：001 对得上，003 大小不符必须拒绝。
    manifest = {"version": 1, "count": 2, "prefix": "input/tpl", "templates": [
        {"id": "001", "name": "10月8日(1)", "size_mb": 8.59, "url": "https://cdn.example.com/input/tpl/0001.mp4"},
        {"id": "003", "name": "10月8日(11)", "size_mb": 5.0, "url": "https://cdn.example.com/input/tpl/0003.mp4"},
    ]}
    rows = [
        ["001", "0001.mp4", "0001.mp4", 7.5, 8.59, "697aeb5d", "近景;适合秋冬", "待上传"],
        ["002", "0002.mp4", "0002.mp4", 5, 7.34, "07f25ca1", "中远景;适合秋冬", "待上传"],
        ["003", "0003.mp4", "0003.mp4", 6, 9.0, "eeacb437", "近景;适合春夏", "待上传"],
    ]
    settings = build_env(tmp, manifest=manifest, rows=rows)
    assert not settings.templates_error, settings.templates_error
    assert len(settings.templates) == 3, len(settings.templates)
    info = settings.tag_workbook_info
    assert info["workbook_rows"] == 3
    assert info["source_ready_templates"] == 1, info

    catalog = TemplateCatalog(settings.templates)
    ready = {item["id"]: bool(item.get("url") or item.get("key")) for item in catalog.items}
    # 001 文件名+大小都对得上 → 可以生成
    assert ready["001"] is True, ready
    # 003 文件名相同但大小差 4MB，属于不同视频 → 必须拒绝，避免拿错素材
    assert ready["003"] is False, ready
    # 002 云端没有 → 只是未上传，不是错误
    assert ready["002"] is False, ready

    # 同类别 OR / 跨类别 AND
    assert [i["id"] for i in catalog.filter_items({"景别": ["近景", "中远景"], "适合季节": ["适合秋冬"]})] == ["001", "002"]
    assert [i["id"] for i in catalog.filter_items({"景别": ["近景"], "适合季节": ["适合秋冬"]})] == ["001"]
    # 手选必须服从筛选
    try:
        catalog.choose("selected", template_id="002", tag_filters={"景别": ["近景"]})
    except TemplateError:
        pass
    else:
        raise AssertionError("手选模式必须服从标签筛选")

    # 空筛选结果给出明确错误，而不是随便挑
    try:
        catalog.choose("random", count=1, tag_filters={"景别": ["中远景"], "适合季节": ["适合春夏"]})
    except TemplateError:
        pass
    else:
        raise AssertionError("空候选集必须拒绝提交")

    # 清单外的视频不计入候选集：目录里多放一个未打标文件
    videos = tmp / "videos"
    videos.mkdir(exist_ok=True)
    for name in ("0001.mp4", "0002.mp4", "0003.mp4", "9999.mp4"):
        (videos / name).write_bytes(b"x")
    tmp2 = tempdir("webtags2-")
    settings2 = build_env(tmp2, manifest=manifest, rows=rows, video_dir=videos)
    assert settings2.tag_workbook_info["unlisted_files"] == 1, settings2.tag_workbook_info

    # 没有 manifest 也要能按工作簿筛选（生产清单不依赖云端索引）
    tmp3 = tempdir("webtags3-")
    settings3 = build_env(tmp3, manifest=None, rows=rows)
    assert not settings3.templates_error, settings3.templates_error
    assert len(settings3.templates) == 3
    assert settings3.tag_workbook_info["source_ready_templates"] == 0
    assert settings3.tag_workbook_info.get("manifest_warning"), "缺少云端索引时应给出提示而不是报错"

    # 重复 ID / 字典外标签必须阻断
    dup = tmp / "dup.xlsx"
    write_workbook(dup, [rows[0], rows[0]])
    try:
        TemplateCatalog([]).attach_tag_workbook(dup)
    except TemplateError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("重复模板ID必须阻断")

    bad_tag = tmp / "badtag.xlsx"
    write_workbook(bad_tag, [["001", "0001.mp4", "0001.mp4", 7.5, 8.59, "x", "近景;不存在的标签", "待上传"]])
    try:
        TemplateCatalog([]).attach_tag_workbook(bad_tag)
    except TemplateError as exc:
        assert "标签字典外" in str(exc)
    else:
        raise AssertionError("字典外标签必须阻断")

    print("OK 工作簿为生产清单、云端索引仅按文件名+大小补地址、68类排除、AND/OR 与防错配")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
