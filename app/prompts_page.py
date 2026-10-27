#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库页面（第三个标签页）。

左：场景单选 + 标签多选（同时满足）
中：命中的提示词列表
右：选中后的详情——变量填值 → 实时预览 → 复制 / 填入生产页
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

if __package__ in (None, ""):  # 允许直接 python app/prompts_page.py 调试
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.prompts import (  # noqa: E402
    ALL_SCENES, Prompt, PromptLibrary, PromptLibraryError, library_path,
    render_body,
)

COLUMNS = ("标题", "场景", "标签", "说明")


class PromptLibraryPage(QWidget):
    """浏览 / 筛选 / 填变量 / 取用提示词。不写库，只读。"""

    use_requested = Signal(str)

    def __init__(self, path: str | Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.path = Path(path) if path else library_path()
        self.lib: PromptLibrary | None = None
        self._results: list[Prompt] = []
        self._current: Prompt | None = None
        self._var_edits: dict[str, QLineEdit] = {}
        self._checked: set[str] = set()

        self._build()
        self.reload()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        # 顶部：场景 + 标签 + 计数
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("场景"))
        self.scene_combo = QComboBox()
        self.scene_combo.setMinimumWidth(150)
        self.scene_combo.currentIndexChanged.connect(self._refresh)
        top.addWidget(self.scene_combo)

        top.addSpacing(12)
        self.tag_box = QGroupBox("标签（需同时满足）")
        tag_layout = QVBoxLayout(self.tag_box)
        tag_layout.setContentsMargins(10, 6, 10, 8)
        tag_layout.setSpacing(4)
        self.tag_list = QListWidget()
        self.tag_list.setFlow(QListWidget.LeftToRight)
        self.tag_list.setWrapping(True)
        self.tag_list.setResizeMode(QListWidget.Adjust)
        self.tag_list.setMaximumHeight(86)
        self.tag_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.tag_list.itemChanged.connect(self._on_tag_changed)
        tag_layout.addWidget(self.tag_list)
        top.addWidget(self.tag_box, 1)

        reload_btn = QPushButton("重新加载")
        reload_btn.setObjectName("ghost")
        reload_btn.setToolTip(f"改完 {self.path.name} 后点这里，不用重启")
        reload_btn.clicked.connect(self.reload)
        top.addWidget(reload_btn)
        root.addLayout(top)

        # 状态 / 错误
        self.status_label = QLabel()
        self.status_label.setObjectName("libStatus")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # 主体：列表 + 详情
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.count_label = QLabel()
        self.count_label.setObjectName("libMeta")
        left_layout.addWidget(self.count_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 186)
        self.table.setColumnWidth(1, 56)
        self.table.setColumnWidth(2, 168)
        left_layout.addWidget(self.table, 1)
        split.addWidget(left)

        split.addWidget(self._build_detail())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([560, 360])
        root.addWidget(split, 1)

    def _build_detail(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(8)

        self.detail_title = QLabel("← 从左边选一条")
        self.detail_title.setObjectName("libTitle")
        self.detail_title.setWordWrap(True)
        layout.addWidget(self.detail_title)

        self.detail_meta = QLabel()
        self.detail_meta.setObjectName("libMeta")
        self.detail_meta.setWordWrap(True)
        layout.addWidget(self.detail_meta)

        self.detail_summary = QLabel()
        self.detail_summary.setObjectName("libSummary")
        self.detail_summary.setWordWrap(True)
        layout.addWidget(self.detail_summary)

        self.var_box = QGroupBox("变量")
        self.var_form = QFormLayout(self.var_box)
        self.var_form.setContentsMargins(10, 8, 10, 10)
        self.var_form.setSpacing(6)
        self.var_box.hide()
        layout.addWidget(self.var_box)

        layout.addWidget(QLabel("最终提示词"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(130)
        self.preview.setPlaceholderText("选中一条后，这里显示可以直接用的提示词")
        layout.addWidget(self.preview, 1)

        self.hint = QLabel()
        self.hint.setObjectName("libWarn")
        self.hint.setWordWrap(True)
        self.hint.hide()
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        self.copy_btn = QPushButton("复制提示词")
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
        layout.addLayout(buttons)
        return panel

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def reload(self) -> None:
        try:
            self.lib = PromptLibrary.load(self.path)
        except PromptLibraryError as exc:
            self.lib = None
            self._results = []
            self.table.setRowCount(0)
            self.count_label.setText("")
            self.scene_combo.clear()
            self._set_status(f"读不到提示词库：{exc}", bad=True)
            self._clear_detail("库文件有问题，修好后点「重新加载」")
            return

        issues = self.lib.issues
        note = f"；{len(issues)} 条被跳过：{issues[0]}" if issues else ""
        self._set_status(f"已加载 {len(self.lib)} 条 · {self.path}{note}", bad=bool(issues))
        self._rebuild_scenes()
        self._rebuild_tags()
        self._refresh()

    def _rebuild_scenes(self) -> None:
        self.scene_combo.blockSignals(True)
        self.scene_combo.clear()
        self.scene_combo.addItem(ALL_SCENES, "")
        for scene, count in (self.lib.scene_facets() if self.lib else []):
            self.scene_combo.addItem(f"{scene}（{count}）", scene)
        self.scene_combo.blockSignals(False)

    def _rebuild_tags(self) -> None:
        self.tag_list.blockSignals(True)
        self.tag_list.clear()
        for tag, count in (self.lib.tag_facets() if self.lib else []):
            item = QListWidgetItem(f"{tag} ({count})")
            item.setData(Qt.UserRole, tag)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if tag in self._checked else Qt.Unchecked)
            self.tag_list.addItem(item)
        self.tag_list.blockSignals(False)

    # ------------------------------------------------------------------
    # 筛选
    # ------------------------------------------------------------------
    def checked_tags(self) -> list[str]:
        tags = []
        for row in range(self.tag_list.count()):
            item = self.tag_list.item(row)
            if item.checkState() == Qt.Checked:
                tags.append(str(item.data(Qt.UserRole)))
        return tags

    def current_scene(self) -> str:
        return str(self.scene_combo.currentData() or "")

    def _on_tag_changed(self, item: QListWidgetItem) -> None:
        tag = str(item.data(Qt.UserRole))
        if item.checkState() == Qt.Checked:
            self._checked.add(tag)
        else:
            self._checked.discard(tag)
        self._refresh()

    def _refresh(self) -> None:
        if self.lib is None:
            return
        self._results = self.lib.filter(scene=self.current_scene(), tags=self.checked_tags())
        total = len(self.lib)
        if self._results:
            self.count_label.setText(f"命中 {len(self._results)} 条（库内 {total} 条）")
        elif total:
            self.count_label.setText(f"没有同时满足条件的提示词（库内 {total} 条）")
        else:
            self.count_label.setText("库是空的，先往 JSON 里加一条")

        self.table.blockSignals(True)
        self.table.setRowCount(len(self._results))
        for row, prompt in enumerate(self._results):
            cells = (
                prompt.title,
                prompt.scene,
                "、".join(prompt.tags),
                prompt.summary,
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, col, item)
        if self._results:
            self.table.selectRow(0)      # 自动选中第一条，右栏永远有内容
        self.table.blockSignals(False)

        if self._results:
            # selectRow 在"选中行没变"时不发信号，这里显式刷一次，避免右栏停在旧条目
            self._show(self._results[0])
        else:
            self._clear_detail(
                "去掉一个标签，或把场景切回「全部场景」试试"
                if total else "库文件里还没有任何提示词"
            )

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------
    def _on_row_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        if 0 <= index < len(self._results):
            self._show(self._results[index])

    def _show(self, prompt: Prompt) -> None:
        self._current = prompt
        self.detail_title.setText(prompt.title)
        self.detail_meta.setText(f"{prompt.scene} · {'、'.join(prompt.tags) or '无标签'}")
        self.detail_summary.setText(prompt.summary)
        self._rebuild_vars(prompt)
        self.copy_btn.setEnabled(True)
        self.use_btn.setEnabled(True)
        self._update_preview()

    def _clear_detail(self, message: str) -> None:
        self._current = None
        self.detail_title.setText(message)
        self.detail_meta.setText("")
        self.detail_summary.setText("")
        self._rebuild_vars(None)
        self.preview.setPlainText("")
        self.hint.hide()
        self.copy_btn.setEnabled(False)
        self.use_btn.setEnabled(False)

    def _rebuild_vars(self, prompt: Prompt | None) -> None:
        while self.var_form.rowCount():
            self.var_form.removeRow(0)
        self._var_edits.clear()

        variables = prompt.variables if prompt else []
        if not variables:
            self.var_box.hide()
            return
        self.var_box.setTitle(f"变量（{len(variables)} 个，填完再复制）")
        self.var_box.show()
        for name in variables:
            edit = QLineEdit()
            edit.setPlaceholderText(f"填 {name} 的值")
            edit.textChanged.connect(self._update_preview)
            self.var_form.addRow(QLabel(name), edit)
            self._var_edits[name] = edit

    def _values(self) -> dict[str, str]:
        return {name: edit.text() for name, edit in self._var_edits.items()}

    def _update_preview(self) -> None:
        if self._current is None:
            return
        text, missing = render_body(self._current.body, self._values())
        self.preview.setPlainText(text)
        if missing:
            self.hint.setText("还有变量没填：" + "、".join(missing))
            self.hint.show()
        else:
            self.hint.hide()

    # ------------------------------------------------------------------
    # 取用
    # ------------------------------------------------------------------
    def final_text(self) -> str:
        return self.preview.toPlainText().strip()

    def _copy(self) -> None:
        text = self.final_text()
        if not text:
            return
        QApplication.clipboard().setText(text)
        missing = render_body(self._current.body, self._values())[1] if self._current else []
        if missing:
            self._set_status(f"已复制，但还有 {len(missing)} 个变量是占位符：{'、'.join(missing)}", bad=True)
        else:
            self._set_status("已复制到剪贴板")

    def _use(self) -> None:
        text = self.final_text()
        if not text:
            return
        self.use_requested.emit(text)
        self._set_status("已填入生产页的提示词框")

    def _set_status(self, text: str, bad: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("tone", "bad" if bad else "ok")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
