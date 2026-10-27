#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库页面（第三个标签页）。

六条提示词直接铺开：名字 + 全文 + 一个「复制提示词」按钮。没有列表、没有筛选、没有弹窗。
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

if __package__ in (None, ""):  # 允许直接 python app/prompts_page.py 调试
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.prompts import Prompt, PromptLibrary, PromptLibraryError, library_path  # noqa: E402


def _as_html(text: str) -> str:
    """纯文本 → 带行距的富文本：长段落不挤在一起。只影响显示，复制出去的仍是原文。"""
    escaped = html.escape(text).replace("\n", "<br>")
    return f"<div style='line-height:158%;'>{escaped}</div>"


class PromptBlock(QFrame):
    """一条提示词：标题行（序号+名字+字数+复制按钮）+ 全文。"""

    def __init__(self, page: "PromptLibraryPage", index: int, prompt: Prompt):
        super().__init__()
        self.page_ref = page
        self.prompt = prompt
        self.setObjectName("promptBlock")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)

        self.title_label = QLabel(f"{index}. {prompt.title}")
        self.title_label.setObjectName("cardTitle")
        header.addWidget(self.title_label)

        self.meta_label = QLabel(f"{prompt.chars} 字")
        self.meta_label.setObjectName("cardMeta")
        header.addWidget(self.meta_label)
        header.addStretch(1)

        self.copy_btn = QPushButton("复制提示词")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(lambda: self.page_ref.copy_prompt(self.prompt))
        header.addWidget(self.copy_btn)

        layout.addLayout(header)

        self.body_label = QLabel()
        self.body_label.setObjectName("promptBody")
        self.body_label.setWordWrap(True)
        self.body_label.setTextFormat(Qt.RichText)
        self.body_label.setText(_as_html(prompt.body))
        self.body_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.body_label)


class PromptLibraryPage(QWidget):
    """按名字取沉淀好的提示词。只读，不写库。"""

    def __init__(self, path: str | Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.path = Path(path) if path else library_path()
        self.lib: PromptLibrary | None = None
        self.blocks: list[PromptBlock] = []

        self._build()
        self.reload()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("libStatus")
        self.status_label.setWordWrap(True)
        top.addWidget(self.status_label, 1)
        self.reload_btn = QPushButton("重新加载")
        self.reload_btn.setObjectName("ghost")
        self.reload_btn.setToolTip(f"改完 {self.path.name} 后点这里，不用重启")
        self.reload_btn.clicked.connect(self.reload)
        top.addWidget(self.reload_btn)
        root.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.host = QWidget()
        self.host_layout = QVBoxLayout(self.host)
        self.host_layout.setContentsMargins(0, 0, 8, 0)
        self.host_layout.setSpacing(12)
        self.host_layout.addStretch(1)
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll, 1)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def reload(self) -> None:
        self._clear_blocks()
        try:
            self.lib = PromptLibrary.load(self.path)
        except PromptLibraryError as exc:
            self.lib = None
            self._set_status(f"读不到提示词库：{exc}", bad=True)
            return

        for idx, prompt in enumerate(self.lib.prompts):
            block = PromptBlock(self, idx + 1, prompt)
            self.host_layout.insertWidget(self.host_layout.count() - 1, block)
            self.blocks.append(block)

        if not self.blocks:
            self._set_status("库是空的，先往 JSON 里加一条", bad=True)
            return

        suffix = f"；{len(self.lib.issues)} 条被跳过：{self.lib.issues[0]}" if self.lib.issues else ""
        self._set_status(f"共 {len(self.blocks)} 条 · 改完 JSON 点右侧「重新加载」{suffix}",
                         bad=bool(self.lib.issues))

    def _clear_blocks(self) -> None:
        for block in self.blocks:
            self.host_layout.removeWidget(block)
            block.deleteLater()
        self.blocks = []

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def copy_prompt(self, prompt: Prompt) -> None:
        QApplication.clipboard().setText(prompt.body)
        self._set_status(f"已复制：{prompt.title}（{prompt.chars} 字）")

    def _set_status(self, text: str, bad: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("tone", "bad" if bad else "ok")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
