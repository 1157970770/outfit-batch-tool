#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标签真源加固回归：不联网、不开服务。

对应评审 docs/code-review-20261103.md 的 P1-6 ~ P1-10：
1. 导入是**合并**：清单里没有的行（网页刚上传的）不能被覆盖掉，空清单必须显式放行。
2. 回滚整库必须带 revision，否则两个人同时回滚会互相覆盖还不报错。
3. 写盘失败要把内存整体退回：不能出现「接口 503、界面却已改」的幻影数据。
4. xlsx 往返保住自定义大类、真实上传/修改时间、以 "=" 开头的标签与文件名。
5. 真源文件损坏时自动用 .bak 顶住，并标记 recovered_from_bak。
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
from tools.template_catalog import load_xlsx, save_xlsx  # noqa: E402
from tools.template_store import StoreError, StoreUnavailable, TemplateStore  # noqa: E402

TAGS = [("近景", "景别"), ("中远景", "景别"), ("适合秋冬", "适合季节"), ("酒店", "场景")]
ROWS = [
    ["001", "0001.mp4", "0001.mp4", 7.5, 8.59, "aaaa1111", "近景;适合秋冬", "已上传"],
    ["002", "0002.mp4", "0002.mp4", 5, 7.34, "bbbb2222", "中远景;适合秋冬", "已上传"],
    ["003", "0003.mp4", "0003.mp4", 6, 9.0, "cccc3333", "近景", "待上传"],
    ["004", "0004.mp4", "0004.mp4", 6, 3.0, "dddd4444", "", "待上传"],
]


def write_workbook(path: Path, rows=None, tags=TAGS) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "视频标签清单"
    sheet.append(["模板ID", "相对路径", "文件名", "时长(秒)", "大小(MB)", "文件指纹", "标签", "状态"])
    for row in (ROWS if rows is None else rows):
        sheet.append(row)
    tags_ws = workbook.create_sheet("标签字典")
    tags_ws.append(["标签值", "分类"])
    for tag, category in tags:
        tags_ws.append([tag, category])
    meta = workbook.create_sheet("项目")
    meta.append(["项目字段", "值"])
    meta.append(["项目ID", "truthproject01"])
    meta.append(["本机视频文件夹", str(path.parent)])
    meta.append(["下一个编号", 10])
    meta.sheet_state = "hidden"
    workbook.save(path)
    workbook.close()
    return path


def new_store(tmp: Path) -> TemplateStore:
    path = tmp / "template_tags.json"
    store = TemplateStore.empty(path)
    store.import_from_xlsx(write_workbook(tmp / "清单.xlsx"))
    return store


def test_import_merges(tmp: Path) -> None:
    store = new_store(tmp)
    added = store.add_video(name="0009.mp4", tags=["近景"], fingerprint="ffff9999", user="web")
    new_id = added["row"]["id"]
    assert store.row(new_id) is not None

    # 再导入一份不含新行的清单：网页刚上传的行必须还在
    result = store.import_from_xlsx(write_workbook(tmp / "清单2.xlsx"))
    assert result["rows"] == 4, result
    assert result["kept"] == 1, "清单里没有的行要计进 kept"
    assert store.row(new_id) is not None, "导入不能删掉网页刚上传的视频"
    assert store.row(new_id)["tags"] == ["近景"]
    assert {row["id"] for row in store.rows()} == {"001", "002", "003", "004", new_id}

    # 清单里有的行按清单更新
    rows = [list(row) for row in ROWS]
    rows[0][6] = "中远景"
    store.import_from_xlsx(write_workbook(tmp / "清单3.xlsx", rows))
    assert store.row("001")["tags"] == ["中远景"], store.row("001")["tags"]
    assert store.row(new_id) is not None


def test_import_empty_needs_allow(tmp: Path) -> None:
    store = new_store(tmp)
    empty = write_workbook(tmp / "空清单.xlsx", rows=[])
    for dry_run in (False, True):
        try:
            store.import_from_xlsx(empty, dry_run=dry_run)
        except StoreError as exc:
            assert "没有任何视频行" in str(exc), exc
        else:
            raise AssertionError("空清单必须报错，不能静默清库")
    result = store.import_from_xlsx(empty, allow_empty=True)
    assert result["rows"] == 0 and not result["written"], result
    assert len(store.rows()) == 4, "即便显式放行，也不能删掉已有行"


def test_restore_requires_revision(tmp: Path) -> None:
    store = new_store(tmp)
    name = store.list_versions()[0]["name"]
    try:
        store.restore_version(name, user="alice")
    except StoreError as exc:
        assert "revision" in str(exc), exc
    else:
        raise AssertionError("整库回滚不带 revision 必须拒绝")
    result = store.restore_version(name, user="alice", revision=store.revision)
    assert result["restored"] == 4, result


def test_save_failure_rolls_back(tmp: Path) -> None:
    store = new_store(tmp)
    before_revision = store.revision
    before_tags = list(store.row("001")["tags"])
    before_disk = store.path.read_bytes()

    def boom() -> None:
        raise StoreUnavailable("磁盘满了")

    store._atomic_save = boom  # type: ignore[assignment]
    try:
        store.set_tags(["001"], ["酒店"], mode="replace", user="alice",
                       revision=before_revision)
    except StoreUnavailable:
        pass
    else:
        raise AssertionError("写盘失败必须把异常抛给调用方")

    assert store.revision == before_revision, "写盘失败后 revision 不能自增"
    assert store.row("001")["tags"] == before_tags, "写盘失败必须回滚内存"
    assert store.path.read_bytes() == before_disk, "磁盘上不应该有半截写入"


def test_xlsx_keeps_odd_values(tmp: Path) -> None:
    project = {
        "project_id": "odd", "source_root": str(tmp), "next_id": 3,
        "tags": ["=SUM(A1)", "近景"], "tag_categories": {"=SUM(A1)": "天气", "近景": "景别"},
        "category_order": ["景别", "天气"],
        "videos": [{
            "id": "001", "relative_path": "0001.mp4", "name": "=A1.mp4", "duration": 7.0,
            "size_mb": 3.0, "fingerprint": "aaa", "tags": ["=SUM(A1)"], "deprecated": False,
            "missing": False, "changed": False, "uploaded": True, "source_dir": str(tmp),
            "original_name": "=原始.mp4", "uploaded_at": 1793600000.0, "touched_at": 1793610000.0,
        }],
    }
    target = tmp / "odd.xlsx"
    save_xlsx(project, target)
    loaded = load_xlsx(target)
    assert loaded["tags"] == ["=SUM(A1)", "近景"], loaded["tags"]
    assert loaded["tag_categories"]["=SUM(A1)"] == "天气", "自定义大类不能被折叠回内置四类"
    row = loaded["videos"][0]
    assert row["name"] == "=A1.mp4", "以 = 开头的文件名必须原样回来"
    assert row["original_name"] == "=原始.mp4"
    assert row["tags"] == ["=SUM(A1)"]
    assert abs(row["uploaded_at"] - 1793600000.0) < 1, row["uploaded_at"]
    assert abs(row["touched_at"] - 1793610000.0) < 1, row["touched_at"]

    # 再导入真源：时间与自定义大类都要活下来
    store = TemplateStore.empty(tmp / "odd.json")
    store.import_from_xlsx(target)
    assert store.row("001")["uploaded_at"] > 0
    assert store.row("001")["name"] == "=A1.mp4"
    assert store.categories()["=SUM(A1)"] == "天气"
    assert "天气" in store.category_list()


def test_load_recovers_from_bak(tmp: Path) -> None:
    store = new_store(tmp)
    store.set_tags(["002"], ["酒店"], mode="replace", user="alice")
    backup = store.path.with_name(store.path.name + ".bak")
    assert backup.exists(), "写入前必须留 .bak"
    store.path.write_text('{"schema": 3, "videos": [', encoding="utf-8")

    recovered = TemplateStore.load(store.path)
    assert recovered.recovered_from_bak is True
    assert {row["id"] for row in recovered.rows()} == {"001", "002", "003", "004"}
    assert recovered.row("002")["tags"] == ["中远景", "适合秋冬"], ".bak 是上一版，回退到它不该丢行"


def main() -> int:
    test_import_merges(tempdir("tagtruth-merge-"))
    test_import_empty_needs_allow(tempdir("tagtruth-empty-"))
    test_restore_requires_revision(tempdir("tagtruth-restore-"))
    test_save_failure_rolls_back(tempdir("tagtruth-rollback-"))
    test_xlsx_keeps_odd_values(tempdir("tagtruth-xlsx-"))
    test_load_recovers_from_bak(tempdir("tagtruth-bak-"))
    print("OK 标签真源加固：导入合并、空清单拦截、回滚要 revision、写盘失败回滚、xlsx 不丢值、坏文件自愈")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
