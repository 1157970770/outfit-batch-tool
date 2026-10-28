#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机视频标签项目与 XLSX 清单测试（不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from tools.template_catalog import (  # noqa: E402
    DEFAULT_TAGS, add_tag, load_xlsx, new_project, save_xlsx, scan_folder,
    set_video_tags, validate_project,
)


def main() -> int:
    tmp = tempdir("tagcatalog-")
    media = tmp / "local-videos"
    media.mkdir()
    (media / "autumn-coat.mp4").write_bytes(b"local-test-video-a")
    (media / "bracelet-demo.mov").write_bytes(b"local-test-video-b")
    project = new_project(media)
    assert len(project["videos"]) == 2
    assert project["videos"][0]["id"] == "001"
    assert project["videos"][1]["id"] == "002"
    assert project["tags"] == DEFAULT_TAGS
    add_tag(project, "校园场景")
    set_video_tags(project, "autumn-coat.mp4", ["适合秋冬", "中远景", "校园场景"])
    set_video_tags(project, "bracelet-demo.mov", ["手串展示", "近景"])
    xlsx = tmp / "template-tags.xlsx"
    save_xlsx(project, xlsx)
    loaded = load_xlsx(xlsx)
    assert loaded["project_id"] == project["project_id"]
    assert loaded["videos"][0]["tags"] == ["适合秋冬", "中远景", "校园场景"]
    assert loaded["videos"][1]["tags"] == ["手串展示", "近景"]
    assert not validate_project(loaded)

    # 新增文件追加编号，不因排序变化改变现有视频 ID/标签。
    (media / "00-new.mp4").write_bytes(b"local-test-video-c")
    refreshed = scan_folder(media, loaded)
    by_path = {row["relative_path"]: row for row in refreshed["videos"]}
    assert by_path["autumn-coat.mp4"]["id"] == "001"
    assert by_path["autumn-coat.mp4"]["tags"] == ["适合秋冬", "中远景", "校园场景"]
    assert by_path["00-new.mp4"]["id"] == "003"
    (media / "autumn-coat.mp4").write_bytes(b"replacement-video-content")
    changed = scan_folder(media, refreshed)
    changed_row = next(row for row in changed["videos"] if row["relative_path"] == "autumn-coat.mp4")
    assert changed_row["changed"] is True
    assert any("内容变化尚未确认" in error for error in validate_project(changed))

    # 标签字典外的值不能被写入单条视频。
    try:
        set_video_tags(refreshed, "00-new.mp4", ["不存在的标签"])
    except ValueError:
        pass
    else:
        raise AssertionError("未知标签应拒绝")
    print("OK 本机标签项目、XLSX 读写、稳定编号和标签校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
