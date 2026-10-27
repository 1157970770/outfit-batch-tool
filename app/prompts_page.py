#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库页面（第三个标签页）。

就六张卡片：点卡片 = 复制全文；卡片右下角「填入生产页」= 写进生产页的提示词框。
没有列表、没有筛选、没有分栏。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

if __package__ in (None, ""):  # 允许直接 python app/prompts_page.py 调试
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.prompts import Prompt, PromptLibrary, PromptLibraryError, library_path  # noqa: E402

COLUMNS = 3


class PromptCard(QFrame):
    """一张卡：名字 + 字数；点卡身复制，点按钮填进生产页。"""

    def __init__(self, page: "PromptLibraryPage", index: int, prompt: Prompt):
        super().__init__()
        self.page_ref = page
        self.prompt = prompt
        self.setObjectName("promptCard")
        self.setMinimumSize(240, 108)
        self.setCursor(Qt.PointingHandCursor)
        preview = prompt.body.replace("\n", " ")
        self.setToolTip(preview[:140] + ("…" if len(preview) > 140 else ""))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(2)

        self.title_label = QLabel(f"{index}. {prompt.title}")
        self.title_label.setObjectName("cardTitle")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        meta = QLabel(f"{prompt.chars} 字")
        meta.setObjectName("cardMeta")
        layout.addWidget(meta)

        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.fill_btn = QPushButton("填入生产页")
        self.fill_btn.setObjectName("ghost")
        self.fill_btn.setFixedHeight(26)
        self.fill_btn.setCursor(Qt.PointingHandCursor)
        self.fill_btn.clicked.connect(lambda: self.page_ref.fill_prompt(self.prompt))
        row.addWidget(self.fill_btn)
        layout.addLayout(row)

    def mousePressEvent(self, event):  # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton:
            self.page_ref.copy_prompt(self.prompt)
        super().mousePressEvent(event)


class PromptLibraryPage(QWidget):
    """按名字取沉淀好的提示词。只读，不写库。"""

    use_requested = Signal(str)

    def __init__(self, path: str | Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.path = Path(path) if path else library_path()
        self.lib: PromptLibrary | None = None
        self.cards: list[PromptCard] = []

        self._build()
        self.reload()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 14)
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

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 2, 0, 0)
        self.grid.setSpacing(12)
        root.addWidget(self.grid_host)

        root.addStretch(1)

        hint = QLabel("点卡片复制全文 · 「填入生产页」把原文写进提示词框")
        hint.setObjectName("libHint")
        root.addWidget(hint)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def reload(self) -> None:
        self._clear_cards()
        try:
            self.lib = PromptLibrary.load(self.path)
        except PromptLibraryError as exc:
            self.lib = None
            self._set_status(f"读不到提示词库：{exc}", bad=True)
            return

        self.cards = []
        for idx, prompt in enumerate(self.lib.prompts):
            card = PromptCard(self, idx + 1, prompt)
            self.grid.addWidget(card, idx // COLUMNS, idx % COLUMNS)
            self.cards.append(card)

        if not self.cards:
            self._set_status("库是空的，先往 JSON 里加一条", bad=True)
            return

        suffix = f"；{len(self.lib.issues)} 条被跳过：{self.lib.issues[0]}" if self.lib.issues else ""
        self._set_status(f"共 {len(self.cards)} 条 · {self.path.name}{suffix}", bad=bool(self.lib.issues))

    def _clear_cards(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards = []

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def copy_prompt(self, prompt: Prompt) -> None:
        QApplication.clipboard().setText(prompt.body)
        self._set_status(f"已复制：{prompt.title}（{prompt.chars} 字）")

    def fill_prompt(self, prompt: Prompt) -> None:
        self.use_requested.emit(prompt.body)
        self._set_status(f"已填入生产页：{prompt.title}")

    def _set_status(self, text: str, bad: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("tone", "bad" if bad else "ok")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
