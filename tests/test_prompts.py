#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库测试：数据层 + 离屏界面（不联网、不花钱、不需要 Key）。

    python3 tests/test_prompts.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmpdir import tempdir  # noqa: E402

from app.prompts import (  # noqa: E402
    ALL_SCENES, PromptLibrary, PromptLibraryError, extract_variables,
    library_path, render_body,
)

LIB = ROOT / "prompts" / "prompt-library.json"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        failures.append(msg)


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------

def test_load() -> None:
    print("加载真实库文件")
    lib = PromptLibrary.load(LIB)
    check(len(lib) >= 8, f"条目数 {len(lib)} >= 8")
    check(not lib.issues, f"没有坏条目（issues={lib.issues}）")
    check("换装" in lib.scenes, f"场景含「换装」：{lib.scenes}")
    check(lib.path == LIB, "记录了来源路径")

    # 种子里的「生产默认」必须和代码常量一字不差，否则一改就悄悄分叉
    from app.core import DEFAULT_PROMPT
    default_entry = next((p for p in lib.prompts if p.id == "outfit-face-lock"), None)
    check(default_entry is not None, "存在 outfit-face-lock")
    check(default_entry is not None and default_entry.body == DEFAULT_PROMPT,
          "outfit-face-lock 正文与 app.core.DEFAULT_PROMPT 完全一致")


def test_filter() -> None:
    print("场景单选 + 标签全包含")
    lib = PromptLibrary.load(LIB)

    outfit = lib.filter(scene="换装")
    check(outfit and all(p.scene == "换装" for p in outfit), f"场景=换装 → {len(outfit)} 条且场景都对")

    all_of = lib.filter(scene=ALL_SCENES)
    check(len(all_of) == len(lib), "「全部场景」不筛")

    face = lib.filter(tags=["面部保持"])
    check(face and all("面部保持" in p.tags for p in face), f"标签=面部保持 → {len(face)} 条")

    combo = lib.filter(scene="换装", tags=["面部保持"])
    check(len(combo) == 1, f"换装 + 面部保持 → {len(combo)} 条（同时满足）")

    both = lib.filter(scene="换装", tags=["近景", "面料质感"])
    check(len(both) == 1 and both[0].id == "outfit-closeup-fabric", "两个标签都要命中")

    check(lib.filter(scene="换装", tags=["台词"]) == [], "换装 + 台词 → 0 条")
    check(lib.filter(tags=["不存在的标签"]) == [], "未知标签 → 0 条")
    check(lib.filter(scene=" 换装 ") == lib.filter(scene="换装"), "场景首尾空格忽略")

    # 顺序 = 文件顺序（人工排的优先级），不按标题字母排
    check([p.id for p in lib.filter(scene="换装")][0] == "outfit-face-lock", "换装第一条是生产默认")

    facets = dict(lib.scene_facets())
    check(facets.get("换装") == 3, f"场景计数换装=3（实际 {facets}）")
    check(lib.tag_facets()[0][1] >= 1, "标签按条数降序，首项有效")


def test_variables() -> None:
    print("{{变量}} 识别与渲染")
    check(extract_variables("{{a}} 和 {{ b }} 再来一次 {{a}}") == ["a", "b"], "按首次出现去重")
    check(extract_variables("没有变量的正文") == [], "无变量 → 空表")
    check(extract_variables("{{}} 空名字不算") == [], "空变量名忽略")

    text, missing = render_body("穿{{颜色}}的{{面料}}", {"颜色": "雾霾蓝", "面料": "亚麻"})
    check(text == "穿雾霾蓝的亚麻" and missing == [], "全部填完 → 无缺失")

    text, missing = render_body("穿{{颜色}}的{{面料}}", {"颜色": "雾霾蓝", "面料": ""})
    check(missing == ["面料"], f"漏填报告：{missing}")
    check("{{面料}}" in text, "漏填的位置保留占位符，肉眼可见")

    text, missing = render_body("穿{{ 颜色 }}", {"颜色": " 墨绿 "})
    check(text == "穿墨绿" and missing == [], "花括号内空格 + 值首尾空格都处理掉")

    seed = next(p for p in PromptLibrary.load(LIB).prompts if p.id == "outfit-closeup-fabric")
    check(seed.variables == ["性别", "部位", "面料", "颜色"], f"种子变量顺序：{seed.variables}")


def test_bad_data() -> None:
    print("坏数据不炸整页")
    lib = PromptLibrary.from_data({
        "prompts": [
            {"title": "正常", "body": "正文", "scene": "展示", "tags": "白底, 特写"},
            {"title": "没正文"},
            {"title": "", "body": "没标题"},
            "不是对象",
        ]
    })
    check(len(lib) == 1, "只保留 1 条可用")
    check(len(lib.issues) == 3, f"3 条坏条目进 issues：{lib.issues}")
    check(lib.prompts[0].tags == ("白底", "特写"), "字符串标签按中英文逗号拆开")
    check(lib.prompts[0].scene == "展示", "场景保留")

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
    print("离屏界面：筛选 / 详情 / 变量 / 复制 / 填入生产页")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from app.prompts_page import PromptLibraryPage

    app = QApplication.instance() or QApplication([])
    page = PromptLibraryPage(path=LIB)
    page.resize(980, 620)
    page.show()
    app.processEvents()

    total = len(PromptLibrary.load(LIB))
    check(page.table.rowCount() == total, f"默认「全部场景」列出 {page.table.rowCount()} 条")
    check(page.copy_btn.isEnabled(), "默认自动选中第一条，复制按钮可用")

    # 场景单选
    index = page.scene_combo.findData("换装")
    page.scene_combo.setCurrentIndex(index)
    app.processEvents()
    check(page.table.rowCount() == 3, f"场景=换装 → {page.table.rowCount()} 行")

    # 标签（同时满足）
    for row in range(page.tag_list.count()):
        item = page.tag_list.item(row)
        if str(item.data(Qt.UserRole)) == "面部保持":
            item.setCheckState(Qt.Checked)
    app.processEvents()
    check(page.checked_tags() == ["面部保持"], f"勾选状态：{page.checked_tags()}")
    check(page.table.rowCount() == 1, f"换装 + 面部保持 → {page.table.rowCount()} 行")

    # 取消勾选 + 回全部场景 → 验证变量填值
    for row in range(page.tag_list.count()):
        page.tag_list.item(row).setCheckState(Qt.Unchecked)
    page.scene_combo.setCurrentIndex(0)
    app.processEvents()

    target = next(i for i in range(page.table.rowCount())
                  if "近景" in page.table.item(i, 0).text())
    page.table.selectRow(target)
    app.processEvents()
    check(len(page._var_edits) == 4, f"详情里生成 4 个变量输入框（实际 {len(page._var_edits)}）")
    check(page.hint.isVisible(), "未填时显示缺值提示")

    values = {"性别": "女孩", "部位": "领口", "面料": "粗花呢", "颜色": "奶油白"}
    for name, value in values.items():
        page._var_edits[name].setText(value)
    app.processEvents()
    text = page.final_text()
    check(all(v in text for v in values.values()), "预览实时反映了填值")
    check("{{" not in text, "填完后没有残留占位符")
    check(not page.hint.isVisible(), "填完后缺值提示消失")

    page.copy_btn.click()
    app.processEvents()
    check(QApplication.clipboard().text() == text, "复制进剪贴板的是最终提示词")
    check("已复制" in page.status_label.text(), f"状态栏反馈：{page.status_label.text()}")

    captured: list[str] = []
    page.use_requested.connect(captured.append)
    page.use_btn.click()
    app.processEvents()
    check(captured == [text], "「填入生产页」发出最终文本")

    # 无结果时给出去掉条件的提示
    page.scene_combo.setCurrentIndex(page.scene_combo.findData("展示"))
    for row in range(page.tag_list.count()):
        item = page.tag_list.item(row)
        if str(item.data(Qt.UserRole)) == "台词":
            item.setCheckState(Qt.Checked)
    app.processEvents()
    check(page.table.rowCount() == 0, "展示 + 台词 → 0 行")
    check("去掉一个标签" in page.detail_title.text(), "无结果时提示怎么放宽条件")
    check(not page.copy_btn.isEnabled(), "无结果时复制不可用")

    page.close()
    return app


def test_window_integration() -> None:
    print("主窗口：第三个标签页 + 填入生产页")
    from PySide6.QtWidgets import QApplication

    import app.main as gui

    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    check(labels == ["小批量试跑", "大批量生产", "提示词库"], f"标签页：{labels}")

    window._apply_prompt("视频1中的人物，把服装换成图1这套")
    check(window.small_panel.prompt_edit.toPlainText() == "视频1中的人物，把服装换成图1这套", "小批量页已填入")
    check(window.big_panel.prompt_edit.toPlainText() == "视频1中的人物，把服装换成图1这套", "大批量页已填入")
    check(window.tabs.currentWidget() is window.small_panel, "切回小批量试跑")
    window.close()
    return app


def main() -> int:
    test_load()
    test_filter()
    test_variables()
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
