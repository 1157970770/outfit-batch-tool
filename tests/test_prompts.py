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


def visible_text(label) -> str:
    """把 QLabel 里的富文本还原成可见文本，用来核对「全文没有丢字」。"""
    import html

    text = re.sub(r"<br\s*/?>", "\n", label.text())
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


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
# 离屏界面（六条全文 + 复制按钮）
# ---------------------------------------------------------------------------

def test_ui() -> None:
    print("离屏界面：六条全文可见 / 复制按钮")
    from PySide6.QtWidgets import QApplication

    from app.prompts_page import PromptLibraryPage

    app = QApplication.instance() or QApplication([])
    page = PromptLibraryPage(path=LIB)
    page.resize(980, 700)
    page.show()
    app.processEvents()

    lib = PromptLibrary.load(LIB)
    check(len(page.blocks) == 6, f"六个区块（实际 {len(page.blocks)}）")
    check(page.blocks[0].title_label.text() == "1. 服装定妆", f"首块标题：{page.blocks[0].title_label.text()}")
    check(page.blocks[5].title_label.text() == "6. 换装同时加手串/项链", "第六块名字对")

    # 全文必须完整呈现，且与库里一字不差
    same = all(visible_text(b.body_label) == lib.prompts[i].body for i, b in enumerate(page.blocks))
    check(same, "六条全文与库正文一致（逐字）")
    check(visible_text(page.blocks[0].body_label).startswith("负责定妆照提示词"), "开头就是原文第一行")
    check(page.blocks[2].body_label.wordWrap(), "长句自动换行，不被截断")

    page.blocks[3].copy_btn.click()      # 第 4 块：手串
    app.processEvents()
    check(QApplication.clipboard().text() == lib.get("p04").body, "复制按钮 = 复制全文")
    check("已复制" in page.status_label.text() and "手串" in page.status_label.text(),
          f"状态反馈：{page.status_label.text()}")

    # 空库：不该崩，也不该留半个区块
    empty = tempdir("prompts-empty-") / "empty.json"
    empty.write_text(json.dumps({"version": 1, "prompts": []}, ensure_ascii=False), encoding="utf-8")
    empty_page = PromptLibraryPage(path=empty)
    app.processEvents()
    check(len(empty_page.blocks) == 0, "空库 0 个区块")
    check("空" in empty_page.status_label.text(), f"空库提示：{empty_page.status_label.text()}")

    # 坏文件：状态栏报错，不崩
    bad_page = PromptLibraryPage(path=tempdir("prompts-bad-") / "nope.json")
    app.processEvents()
    check(len(bad_page.blocks) == 0 and "读不到" in bad_page.status_label.text(), "坏路径 → 状态栏报错")

    page.close()
    empty_page.close()
    bad_page.close()


def test_window_integration() -> None:
    print("主窗口：第三个标签页")
    from PySide6.QtWidgets import QApplication

    import app.main as gui

    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    check(labels == ["小批量试跑", "大批量生产", "提示词库"], f"标签页：{labels}")
    check(len(window.prompts_page.blocks) == 6, "主窗口里的提示词库也铺开六条")
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
