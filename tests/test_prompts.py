#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库测试：数据层 + 离屏界面（不联网、不花钱、不需要 Key）。

    python3 tests/test_prompts.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmpdir import tempdir  # noqa: E402
from app.prompts import PromptLibrary, PromptLibraryError, library_path  # noqa: E402

LIB = ROOT / "prompts" / "prompt-library.json"
RAW = ROOT / "prompts" / "raw"

TITLES = ["服装定妆", "换装有字幕", "换装无字幕", "手串", "项链", "换装同时加手串/项链"]

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        failures.append(msg)


def raw_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return re.search(r"```text\n(.*?)\n```", text, re.S).group(1).strip()


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------

def test_load() -> None:
    print("加载真实库文件")
    lib = PromptLibrary.load(LIB)
    check(len(lib) == 6, f"6 条（实际 {len(lib)}）")
    check(not lib.issues, f"没有坏条目（issues={lib.issues}）")
    check([p.title for p in lib.prompts] == TITLES, f"名字与顺序：{[p.title for p in lib.prompts]}")
    check([p.id for p in lib.prompts] == [f"p{i:02d}" for i in range(1, 7)], "编号 p01–p06")
    check(all(p.chars >= 100 for p in lib.prompts), f"每条都有正文：{[p.chars for p in lib.prompts]}")
    check(lib.path == LIB, "记录了来源路径")
    check(lib.get("p04").title == "手串", "按 id 取得到")


def test_raw_matches_json() -> None:
    """JSON 里的正文必须与 prompts/raw 里的逐字记录一致（防止哪天手滑改了一个字）。"""
    if not RAW.exists():
        print("跳过与原文比对（没有 prompts/raw）")
        return
    print("JSON 正文 vs 原始记录")
    lib = PromptLibrary.load(LIB)
    files = sorted(RAW.glob("P-*.md"))
    check(len(files) == len(lib), f"原文 {len(files)} 份 / 库 {len(lib)} 条")
    for prompt, path in zip(lib.prompts, files):
        check(prompt.body == raw_body(path), f"{prompt.id} {prompt.title} 逐字一致")


def test_bad_data() -> None:
    print("坏数据不炸整页")
    lib = PromptLibrary.from_data({
        "prompts": [
            {"title": "正常", "body": "正文"},
            {"title": "没正文"},
            {"title": "", "body": "没名字"},
            "不是对象",
        ]
    })
    check(len(lib) == 1, "只保留 1 条可用")
    check(len(lib.issues) == 3, f"3 条坏条目进 issues：{lib.issues}")
    check(lib.prompts[0].id == "p01", "缺 id 时按位置补 p01")

    try:
        PromptLibrary.from_data({"prompts": "nope"})
        check(False, "prompts 不是数组应报错")
    except PromptLibraryError:
        check(True, "prompts 不是数组 → PromptLibraryError")

    tmp = tempdir("prompts-")
    broken = tmp / "broken.json"
    broken.write_text("{ 这不是 json", encoding="utf-8")
    try:
        PromptLibrary.load(broken)
        check(False, "坏 JSON 应报错")
    except PromptLibraryError as exc:
        check("不是合法 JSON" in str(exc), "坏 JSON → 带行号的提示")

    try:
        PromptLibrary.load(tmp / "missing.json")
        check(False, "文件不存在应报错")
    except PromptLibraryError as exc:
        check("不存在" in str(exc), "文件不存在 → 明确提示")


def test_path_override() -> None:
    print("库路径可被环境变量覆盖")
    old = os.environ.get("PROMPT_LIBRARY")
    try:
        os.environ["PROMPT_LIBRARY"] = str(LIB)
        check(library_path() == LIB, "PROMPT_LIBRARY 优先")
    finally:
        if old is None:
            os.environ.pop("PROMPT_LIBRARY", None)
        else:
            os.environ["PROMPT_LIBRARY"] = old
    check(library_path() == LIB, "默认解析到仓库里的那份")


# ---------------------------------------------------------------------------
# 离屏界面
# ---------------------------------------------------------------------------

def test_ui() -> None:
    print("离屏界面：列表 / 全文 / 复制 / 填入生产页")
    from PySide6.QtWidgets import QApplication

    from app.prompts_page import PromptLibraryPage

    app = QApplication.instance() or QApplication([])
    page = PromptLibraryPage(path=LIB)
    page.resize(980, 620)
    page.show()
    app.processEvents()

    lib = PromptLibrary.load(LIB)
    check(page.list.count() == 6, f"列表 6 行（实际 {page.list.count()}）")
    check(page.list.item(0).text() == "1. 服装定妆", f"首项：{page.list.item(0).text()}")
    check(page.body_edit.toPlainText().strip() == lib.prompts[0].body, "默认选中第一条并显示全文")
    check(page.copy_btn.isEnabled(), "复制按钮可用")

    page.list.setCurrentRow(3)          # 第 4 条：手串
    app.processEvents()
    check(page.detail_title.text() == "手串", f"标题：{page.detail_title.text()}")
    check(page.body_edit.toPlainText().strip() == lib.get("p04").body, "全文与库一致")
    check("字" in page.detail_meta.text(), f"字数提示：{page.detail_meta.text()}")

    page.copy_btn.click()
    app.processEvents()
    check(QApplication.clipboard().text() == lib.get("p04").body, "复制进剪贴板的是全文")
    check("已复制" in page.status_label.text(), f"状态栏反馈：{page.status_label.text()}")

    captured: list[str] = []
    page.use_requested.connect(captured.append)
    page.use_btn.click()
    app.processEvents()
    check(captured == [lib.get("p04").body], "「填入生产页」发出全文")

    # 空库：不该崩，也不该给出可点的复制
    empty = tempdir("prompts-empty-") / "empty.json"
    empty.write_text(json.dumps({"version": 1, "prompts": []}, ensure_ascii=False), encoding="utf-8")
    empty_page = PromptLibraryPage(path=empty)
    app.processEvents()
    check(empty_page.list.count() == 0, "空库列表 0 行")
    check(not empty_page.copy_btn.isEnabled(), "空库时复制不可用")

    page.close()
    empty_page.close()


def test_window_integration() -> None:
    print("主窗口：第三个标签页 + 填入生产页")
    from PySide6.QtWidgets import QApplication

    import app.main as gui

    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    check(labels == ["小批量试跑", "大批量生产", "提示词库"], f"标签页：{labels}")

    text = "视频1中的人物，把服装换成图1这套"
    window._apply_prompt(text)
    check(window.small_panel.prompt_edit.toPlainText() == text, "小批量页已填入")
    check(window.big_panel.prompt_edit.toPlainText() == text, "大批量页已填入")
    check(window.tabs.currentWidget() is window.small_panel, "切回小批量试跑")
    window.close()


def main() -> int:
    test_load()
    test_raw_matches_json()
    test_bad_data()
    test_path_override()
    test_ui()
    test_window_integration()

    if failures:
        print(f"\nFAIL · {len(failures)} 项")
        for msg in failures:
            print("  -", msg)
        return 1
    print("\nOK 提示词库全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
