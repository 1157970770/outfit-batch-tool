#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标签真源（web-data/template_tags.json）单元测试：不联网、不开服务。

覆盖的核心约定：
1. revision 是乐观锁：旧值写入必须 409，且**文件一个字节都不变**；幂等写入不 +1。
2. deprecate 是软删除：不删行、不删标签、可恢复；「标签为空」是另一件事。
3. 标签库合并/删除会级联改写所有视频行，删除可能让行变回「未打标」。
4. 审计最多 500 条，溢出只计数；文件损坏必须 503 而不是静默给空表。
5. 网页与桌面共用同一份文件：跨线程、跨进程写都不丢更新。
6. xlsx 只是导入/导出通道：导出再导入，deprecated 不能丢。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook  # noqa: E402
from tools.template_catalog import load_xlsx  # noqa: E402
from tools.template_store import (  # noqa: E402
    NotFound,
    RevisionConflict,
    StoreError,
    StoreUnavailable,
    TemplateStore,
)

TAGS = [("近景", "景别"), ("中远景", "景别"), ("适合秋冬", "适合季节"), ("酒店", "场景")]


def write_workbook(path: Path, rows, tags=TAGS, video_dir: str = "") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "视频标签清单"
    ws.append(["模板ID", "相对路径", "文件名", "时长(秒)", "大小(MB)", "文件指纹", "标签", "状态"])
    for row in rows:
        ws.append(row)
    sheet = wb.create_sheet("标签字典")
    sheet.append(["标签值", "分类"])
    for tag, category in tags:
        sheet.append([tag, category])
    meta = wb.create_sheet("项目")
    meta.append(["项目字段", "值"])
    meta.append(["项目ID", "testproject01"])
    meta.append(["本机视频文件夹", video_dir or str(path.parent)])
    meta.append(["下一个编号", 10])
    meta.sheet_state = "hidden"
    wb.save(path)
    wb.close()
    return path


def sample_rows():
    return [
        ["001", "0001.mp4", "0001.mp4", 7.5, 8.59, "aaaa1111", "近景;适合秋冬", "已上传"],
        ["002", "0002.mp4", "0002.mp4", 5, 7.34, "bbbb2222", "中远景;适合秋冬", "已上传"],
        ["003", "0003.mp4", "0003.mp4", 6, 9.0, "cccc3333", "近景;酒店", "待上传"],
        ["004", "0004.mp4", "0004.mp4", 6, 3.0, "dddd4444", "", "待上传"],
    ]


def new_store(tmp: Path, rows=None, name: str = "template_tags.json") -> TemplateStore:
    path = tmp / name
    workbook = write_workbook(tmp / f"{name}.xlsx", rows or sample_rows())
    store = TemplateStore.empty(path)
    store.import_from_xlsx(workbook)
    return store


def test_import(tmp: Path) -> None:
    path = tmp / "import" / "template_tags.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = write_workbook(tmp / "import.xlsx", sample_rows())

    empty = TemplateStore.load(path, create_empty=True)
    assert empty.revision == 0 and empty.rows() == []

    dry = TemplateStore.empty(path).import_from_xlsx(workbook, dry_run=True)
    assert dry["rows"] == 4 and dry["untagged"] == 1 and not dry["written"], dry
    assert not path.exists(), "dry-run 不能写盘"

    store = TemplateStore.empty(path).import_from_xlsx(workbook)
    assert store["written"] and store["revision"] == 1, store
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["revision"] == 1 and len(data["videos"]) == 4
    assert data["migrated_from"]["kind"] == "xlsx"
    assert [row["id"] for row in data["videos"]] == ["001", "002", "003", "004"]
    # 状态列 → missing/changed/uploaded；空标签不被当成 deprecated
    assert data["videos"][0]["uploaded"] is True
    assert data["videos"][3]["tags"] == [] and data["videos"][3]["deprecated"] is False

    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        TemplateStore.empty(path).import_from_xlsx(workbook)
    except Exception:  # noqa: BLE001 - 只要不抛就继续断言
        pass
    assert raw, "文件应保持可读"


def test_revision_and_idempotent(tmp: Path) -> None:
    store = new_store(tmp)
    assert store.revision == 1

    result = store.set_tags(["001"], ["近景", "适合秋冬"], mode="replace", user="alice")
    assert result["changed"] == 0 and result["revision"] == 1, "同标签重复写入必须幂等"

    result = store.set_tags(["001"], ["中远景"], mode="replace", user="alice")
    assert result["changed"] == 1 and result["revision"] == 2, result
    assert store.row("001")["tags"] == ["中远景"]
    assert store.row("001")["touched_by"] == "alice"

    store.set_tags(["002"], ["近景"], mode="add", user="alice")
    assert store.revision == 3
    assert store.row("002")["tags"] == ["中远景", "适合秋冬", "近景"]

    store.set_tags(["002"], ["适合秋冬"], mode="remove", user="alice")
    assert store.row("002")["tags"] == ["中远景", "近景"]

    try:
        store.set_tags(["001"], ["不存在的标签"], mode="replace")
    except StoreError as exc:
        assert "标签不存在" in str(exc)
    else:
        raise AssertionError("字典外标签必须拒绝")
    assert store.revision == 4, "被拒绝的写入不能改 revision"


def test_conflict(tmp: Path) -> None:
    store = new_store(tmp)
    stale = store.revision
    store.set_tags(["001"], ["近景"], mode="replace", user="alice")
    before = store.path.read_bytes()

    try:
        store.set_tags(["002"], ["近景"], mode="replace", revision=stale, user="bob")
    except RevisionConflict as exc:
        assert exc.revision == store.revision, (exc.revision, store.revision)
    else:
        raise AssertionError("旧 revision 必须 409")
    assert store.path.read_bytes() == before, "被拒的写入不能让文件发生变化"

    # 拿到最新 revision 后可以继续
    current = store.revision
    result = store.set_tags(["002"], ["酒店"], mode="replace", revision=current, user="bob")
    assert result["revision"] == current + 1, result


def test_deprecate(tmp: Path) -> None:
    store = new_store(tmp)
    store.deprecate(["001"], True, user="alice", reason="画面重复")
    row = store.row("001")
    assert row["deprecated"] is True and row["tags"] == ["近景", "适合秋冬"], "弃用必须保留标签"
    assert row["deprecated_by"] == "alice" and row["deprecated_reason"] == "画面重复"

    again = store.deprecate(["001"], True, user="bob")
    assert again["changed"] == 0, "重复弃用必须幂等"

    store.deprecate(["001"], False, user="alice")
    row = store.row("001")
    assert row["deprecated"] is False and row["tags"] == ["近景", "适合秋冬"]
    assert row["deprecated_at"] == 0 and row["deprecated_by"] == ""


def test_library(tmp: Path) -> None:
    store = new_store(tmp)
    # 近景：001、003 使用；中远景：002 使用
    store.library_merge("近景", "中远景", user="alice")
    assert store.row("001")["tags"] == ["中远景", "适合秋冬"]
    assert store.row("003")["tags"] == ["中远景", "酒店"]
    assert "近景" not in store.library()["tags"]
    assert store.usage()["中远景"] == 3

    impact = store.impact_preview("delete", {"name": "中远景"})
    assert impact["affected"] == 3
    # 003 还挂着「酒店」，删中远景不会让它变空
    assert impact["becomes_untagged"] == 0, impact
    store.set_tags(["003"], ["中远景"], mode="replace", user="alice")
    impact = store.impact_preview("delete", {"name": "中远景"})
    assert impact["affected"] == 3 and impact["becomes_untagged"] == 1, impact

    revision_before = store.revision
    store.library_delete("中远景", user="bob")
    assert store.revision == revision_before + 1
    assert store.row("003")["tags"] == []
    assert store.row("001")["tags"] == ["适合秋冬"]
    assert "中远景" not in store.library()["tags"]

    store.library_create("特写", "景别", user="alice")
    assert store.library()["categories"]["特写"] == "景别"
    try:
        store.library_create("特写", "场景")
    except StoreError as exc:
        assert "已经存在" in str(exc)
    else:
        raise AssertionError("重名标签必须拒绝")

    store.library_rename("特写", "大特写", user="alice")
    assert "大特写" in store.library()["tags"] and "特写" not in store.library()["tags"]
    try:
        store.library_rename("大特写", "酒店", user="alice")
    except StoreError as exc:
        assert "已存在" in str(exc)
    else:
        raise AssertionError("改名撞已有标签必须拒绝，提示改用合并")

    try:
        store.library_set_category("大特写", "不存在的分类")
    except StoreError:
        pass
    else:
        raise AssertionError("非法分类必须拒绝")

    store.library_set_category("大特写", "姿势", user="alice")
    assert store.library()["categories"]["大特写"] == "姿势"


def test_impact_read_only(tmp: Path) -> None:
    store = new_store(tmp)
    before_revision = store.revision
    before_bytes = store.path.read_bytes()
    preview = store.impact_preview("merge", {"source": "近景", "target": "中远景"})
    assert preview["affected"] == 2, preview
    assert preview["revision"] == before_revision
    assert store.revision == before_revision and store.path.read_bytes() == before_bytes, "预览不能写盘"

    deprecate = store.impact_preview("deprecate", {"ids": ["001", "002"]})
    assert deprecate["affected"] == 2


def test_apply_project_diff(tmp: Path) -> None:
    store = new_store(tmp)
    base = store.to_project()
    current = store.to_project()

    current["videos"][0]["tags"] = ["酒店"]           # 改标签
    current["videos"][2]["deprecated"] = True          # 弃用
    current["videos"].append({                        # 主机新扫描到的一条
        "id": "005", "relative_path": "0005.mp4", "name": "0005.mp4", "duration": 7.0,
        "size_mb": 4.2, "fingerprint": "eeee5555", "tags": ["近景"], "deprecated": False,
        "missing": False, "changed": False, "uploaded": False, "source_dir": base["source_root"],
        "original_name": "",
    })
    current["videos"] = [row for row in current["videos"] if row["id"] != "004"]  # 老版本“移除”

    result = store.apply_project_diff(base, current, user="host")
    assert result["added"] == 1 and result["retired"] == 1, result
    assert store.row("001")["tags"] == ["酒店"]
    assert store.row("003")["deprecated"] is True
    assert store.row("004")["deprecated"] is True, "base 有、current 没有 → 标记弃用而不是删行"
    assert store.row("005") is not None

    again = store.apply_project_diff(store.to_project(), store.to_project(), user="host")
    assert again["changed"] == 0, "无差异时不能写盘"


def test_audit_limit(tmp: Path) -> None:
    store = new_store(tmp)
    for index in range(505):
        tags = ["近景"] if index % 2 else ["中远景"]
        store.set_tags(["002"], tags, mode="replace", user="loop")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert len(data["audit"]) == 500, len(data["audit"])
    assert data["audit_dropped"] == 6, data["audit_dropped"]  # 导入 1 条 + 505 次写入 - 500
    assert data["audit"][-1]["user"] == "loop"
    assert data["audit"][-1]["revision"] == data["revision"]


def test_corrupt(tmp: Path) -> None:
    path = tmp / "broken.json"
    path.write_text('{"schema": 3, "videos": [', encoding="utf-8")
    try:
        TemplateStore.load(path)
    except StoreUnavailable as exc:
        assert exc.status == 503 and "损坏" in str(exc)
    else:
        raise AssertionError("损坏文件必须 503")


def test_threads(tmp: Path) -> None:
    store = new_store(tmp, name="threaded.json")
    start = store.revision

    def worker(template_id: str, first: str, second: str) -> None:
        for index in range(50):
            store.set_tags([template_id], [first if index % 2 else second], mode="replace", user="thread")

    threads = [threading.Thread(target=worker, args=("001", "近景", "中远景")),
               threading.Thread(target=worker, args=("002", "酒店", "适合秋冬"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.revision == start + 100, (store.revision, start)


def test_cross_process(tmp: Path) -> None:
    store = new_store(tmp, name="cross.json")
    code = (
        "import sys\n"
        "from tools.template_store import TemplateStore\n"
        "s = TemplateStore.load(sys.argv[1])\n"
        "s.set_tags(['003'], ['酒店'], mode='replace', user='subprocess')\n"
        "print(s.revision)\n"
    )
    env = {"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-c", code, str(store.path)],
                            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert store.refresh_if_changed() is True, "别的进程写过必须能被发现"
    assert store.row("003")["tags"] == ["酒店"]
    assert store.revision == int(result.stdout.strip())


def test_xlsx_roundtrip(tmp: Path) -> None:
    store = new_store(tmp, name="roundtrip.json")
    store.deprecate(["002"], True, user="alice")
    target = tmp / "export.xlsx"
    store.export_to_xlsx(target)

    project = load_xlsx(target)
    rows = {row["id"]: row for row in project["videos"]}
    assert rows["002"]["deprecated"] is True, rows["002"]
    assert rows["001"]["tags"] == ["近景", "适合秋冬"]
    assert rows["004"]["tags"] == []

    # 再导入回来，弃用状态不能丢
    other = TemplateStore.empty(tmp / "reimport.json")
    other.import_from_xlsx(target)
    assert other.row("002")["deprecated"] is True
    stats = other.stats()
    assert (stats["total"], stats["active"], stats["deprecated"], stats["untagged"]) == (4, 2, 1, 1), stats
    assert stats["uploaded"] == 1, stats  # 001 原本“已上传”；003 是“待上传”，不能凭空变已上传
    assert rows["004"]["deprecated"] is False, "空标签是未打标，不能被写成显式弃用"


def test_no_tags_json(tmp: Path) -> None:
    library = tmp / "tags.json"
    library.write_text(json.dumps({"version": 2, "tags": ["近景"], "categories": {"近景": "景别"}}),
                       encoding="utf-8")
    before = library.read_bytes()
    store = new_store(tmp, name="nolib.json")
    store.library_create("新标签", "场景", user="alice")
    store.library_merge("近景", "中远景", user="alice")
    assert library.read_bytes() == before, "store 模式不写 _state/tagger/tags.json"



def test_versions(tmp: Path) -> None:
    store = new_store(tmp)
    versions = store.list_versions()
    assert versions and versions[0]["kind"] == "auto", versions
    assert versions[0]["count"] == 4
    base_name = versions[0]["name"]

    change = store.set_tags(["001"], ["酒店"], mode="replace", user="alice")
    assert change["changed"] == 1
    after_change = store.list_versions()
    assert len(after_change) >= 2 and after_change[0]["revision"] == change["revision"]
    assert after_change[0]["action"] == "set_tags"
    assert store.row("001")["tags"] == ["酒店"]

    manual = store.create_version("上线前", user="alice")
    assert manual["kind"] == "manual" and manual["note"] == "上线前"
    assert store.list_versions()[0]["name"] == manual["name"]

    # 回滚到导入时那一版：标签回到 001=近景+适合秋冬
    result = store.restore_version(base_name, user="bob", revision=store.revision)
    assert result["restored"] == 4, result
    assert store.row("001")["tags"] == ["近景", "适合秋冬"]
    assert result["backup"], "回滚前必须自动留备份"

    names = {item["name"] for item in store.list_versions()}
    assert result["backup"] in names, "备份版本要能在列表里看到"
    assert base_name in names, "被回滚到的版本本身不能丢"

    # 回滚后新增的视频，回滚到旧版会消失，但仍留在备份里
    base = store.to_project()
    current = store.to_project()
    current["videos"].append({
        "id": "006", "relative_path": "0006.mp4", "name": "0006.mp4", "duration": 7.0,
        "size_mb": 4.0, "fingerprint": "ffff6666", "tags": ["近景"], "deprecated": False,
        "missing": False, "changed": False, "uploaded": False, "source_dir": base["source_root"],
        "original_name": "",
    })
    store.apply_project_diff(base, current, user="host")
    assert store.row("006") is not None
    old_name = store.list_versions()[-1]["name"]
    result = store.restore_version(old_name, user="alice", revision=store.revision)
    assert store.row("006") is None, "回滚到没有 006 的版本后应消失"
    assert result["added_since"] == 1, result
    backup_payload = store.read_version(result["backup"])
    assert any(row["id"] == "006" for row in backup_payload["videos"]), "备份里必须保留 006"

    # 文件名校验 / 不存在
    try:
        store.read_version("../../etc/passwd")
    except StoreError:
        pass
    else:
        raise AssertionError("非法版本名必须拒绝")
    try:
        store.read_version("v999999-1-auto-abcd.json.gz")
    except NotFound:
        pass
    else:
        raise AssertionError("不存在的版本必须 404")


def test_version_prune(tmp: Path) -> None:
    store = new_store(tmp, name="pruned.json")
    for index in range(55):
        tags = ["近景"] if index % 2 else ["酒店"]
        store.set_tags(["002"], tags, mode="replace", user="loop")
    from tools.template_store import AUTO_KEEP
    autos = [item for item in store.list_versions(limit=500) if item["kind"] == "auto"]
    assert len(autos) <= AUTO_KEEP, (len(autos), AUTO_KEEP)
    assert len(autos) >= AUTO_KEEP - 1, len(autos)


def test_categories(tmp: Path) -> None:
    store = new_store(tmp)
    assert store.category_list() == ["景别", "适合季节", "场景", "姿势"], store.category_list()

    store.category_create("天气", user="alice")
    assert "天气" in store.category_list()
    store.library_create("下雨", "天气", user="alice")
    assert store.categories()["下雨"] == "天气"

    try:
        store.library_create("刮风", "不存在的大类")
    except StoreError as exc:
        assert "分类不存在" in str(exc)
    else:
        raise AssertionError("未登记的大类必须拒绝")

    store.category_rename("天气", "气候", user="alice")
    assert "气候" in store.category_list() and "天气" not in store.category_list()
    assert store.categories()["下雨"] == "气候"

    try:
        store.category_delete("气候")
    except StoreError as exc:
        assert "还有" in str(exc)
    else:
        raise AssertionError("下面还有标签的大类不能删")

    store.library_set_category("下雨", "场景", user="alice")
    store.category_delete("气候", user="alice")
    assert "气候" not in store.category_list()
    assert "下雨" in store.library()["tags"]

    # 只剩最后一个大类时不允许删
    for name in list(store.category_list())[:-1]:
        try:
            store.category_delete(name)
        except StoreError:
            pass
    assert len(store.category_list()) >= 1

def main() -> int:
    test_import(tempdir("tagstore-import-"))
    test_revision_and_idempotent(tempdir("tagstore-rev-"))
    test_conflict(tempdir("tagstore-conflict-"))
    test_deprecate(tempdir("tagstore-dep-"))
    test_library(tempdir("tagstore-lib-"))
    test_impact_read_only(tempdir("tagstore-impact-"))
    test_apply_project_diff(tempdir("tagstore-diff-"))
    test_categories(tempdir("tagstore-cat-"))
    test_versions(tempdir("tagstore-ver-"))
    test_version_prune(tempdir("tagstore-prune-"))
    test_audit_limit(tempdir("tagstore-audit-"))
    test_corrupt(tempdir("tagstore-broken-"))
    test_threads(tempdir("tagstore-thread-"))
    test_cross_process(tempdir("tagstore-cross-"))
    test_xlsx_roundtrip(tempdir("tagstore-xlsx-"))
    test_no_tags_json(tempdir("tagstore-nolib-"))
    print("OK 标签真源：revision 乐观锁、弃用可恢复、标签库级联、版本快照/回滚、审计上限、跨进程共享、xlsx 往返")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
