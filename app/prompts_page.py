#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库页面（第三个标签页）。

左边是名字列表，右边是全文 + 复制 / 填入生产页。没有分类、没有筛选——条数少时名字就是索引。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout,
    QWidget,
)

if __package__ in (None, ""):  # 允许直接 python app/prompts_page.py 调试
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.prompts import Prompt, PromptLibrary, PromptLibraryError, library_path  # noqa: E402


class PromptLibraryPage(QWidget):
    """按名字取沉淀好的提示词。只读，不写库。"""

    use_requested = Signal(str)

    def __init__(self, path: str | Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.path = Path(path) if path else library_path()
        self.lib: PromptLibrary | None = None
        self._current: Prompt | None = None

        self._build()
        self.reload()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(8)

        self.status_label = QLabel()
        self.status_label.setObjectName("libStatus")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        split = QSplitter(Qt.Horizontal)

        # 左：名字列表
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        head = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setObjectName("libMeta")
        head.addWidget(self.count_label)
        head.addStretch(1)
        reload_btn = QPushButton("重新加载")
        reload_btn.setObjectName("ghost")
        reload_btn.setToolTip(f"改完 {self.path.name} 后点这里，不用重启")
        reload_btn.clicked.connect(self.reload)
        head.addWidget(reload_btn)
        left_layout.addLayout(head)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setAlternatingRowColors(True)
        self.list.setWordWrap(True)
        self.list.currentRowChanged.connect(self._on_row_changed)
        left_layout.addWidget(self.list, 1)
        split.addWidget(left)

        # 右：全文
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        self.detail_title = QLabel("← 从左边选一条")
        self.detail_title.setObjectName("libTitle")
        self.detail_title.setWordWrap(True)
        right_layout.addWidget(self.detail_title)

        self.detail_meta = QLabel()
        self.detail_meta.setObjectName("libMeta")
        right_layout.addWidget(self.detail_meta)

        self.body_edit = QPlainTextEdit()
        self.body_edit.setReadOnly(True)
        self.body_edit.setPlaceholderText("选中一条后，这里显示可以直接复制的提示词全文")
        right_layout.addWidget(self.body_edit, 1)

        buttons = QHBoxLayout()
        self.copy_btn = QPushButton("复制全文")
        self.copy_btn.setObjectName("primary")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy)
        self.use_btn = QPushButton("填入生产页")
        self.use_btn.setToolTip("把这段提示词填进生产页的提示词框，直接开跑")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use)
        buttons.addWidget(self.copy_btn)
        buttons.addWidget(self.use_btn)
        buttons.addStretch(1)
        right_layout.addLayout(buttons)
        split.addWidget(right)

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setSizes([260, 700])
        root.addWidget(split, 1)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def reload(self) -> None:
        try:
            self.lib = PromptLibrary.load(self.path)
        except PromptLibraryError as exc:
            self.lib = None
            self.list.clear()
            self.count_label.setText("")
            self._clear_detail("读不到提示词库，修好后点「重新加载」")
            self._set_status(f"{exc}", bad=True)
            return

        self.list.blockSignals(True)
        self.list.clear()
        for idx, prompt in enumerate(self.lib.prompts, 1):
            item = QListWidgetItem(f"{idx}. {prompt.title}")
            item.setData(Qt.UserRole, prompt.id)
            item.setToolTip(f"{prompt.title} · {prompt.chars} 字")
            self.list.addItem(item)
        self.list.blockSignals(False)

        self.count_label.setText(f"共 {len(self.lib)} 条")
        note = f"；{len(self.lib.issues)} 条被跳过：{self.lib.issues[0]}" if self.lib.issues else ""
        self._set_status(f"{self.path}{note}", bad=bool(self.lib.issues))

        if self.lib.prompts:
            self.list.setCurrentRow(0)
        else:
            self._clear_detail("库是空的，先往 JSON 里加一条")

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------
    def _on_row_changed(self, row: int) -> None:
        if self.lib is None or not (0 <= row < len(self.lib.prompts)):
            return
        self._show(self.lib.prompts[row])

    def _show(self, prompt: Prompt) -> None:
        self._current = prompt
        self.detail_title.setText(prompt.title)
        self.detail_meta.setText(f"{prompt.chars} 字 · 原文照抄")
        self.body_edit.setPlainText(prompt.body)
        self.copy_btn.setEnabled(True)
        self.use_btn.setEnabled(True)

    def _clear_detail(self, message: str) -> None:
        self._current = None
        self.detail_title.setText(message)
        self.detail_meta.setText("")
        self.body_edit.setPlainText("")
        self.copy_btn.setEnabled(False)
        self.use_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # 取用
    # ------------------------------------------------------------------
    def current_text(self) -> str:
        return self.body_edit.toPlainText().strip()

    def _copy(self) -> None:
        text = self.current_text()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._set_status(f"已复制（{len(text)} 字）")

    def _use(self) -> None:
        text = self.current_text()
        if not text:
            return
        self.use_requested.emit(text)
        self._set_status("已填入生产页的提示词框")

    def _set_status(self, text: str, bad: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("tone", "bad" if bad else "ok")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
