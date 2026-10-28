#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机视频打标工具：只读本地文件、管理自由标签并导出 XLSX，不联网。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QProgressDialog, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QInputDialog,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.template_catalog import (  # noqa: E402
    CatalogError, DEFAULT_TAGS, add_library_tag, add_tag, load_state,
    load_tag_library, new_project, normalize_tags, save_state, save_tag_library,
    scan_folder, save_xlsx, load_xlsx, set_video_tags,
)


class TagLibraryDialog(QDialog):
    """查看、删除全局标签库中的标签。"""

    def __init__(self, tags: list[str], usage: dict[str, int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("标签库管理")
        self.resize(430, 430)
        self.tags = list(tags)
        self.usage = dict(usage)
        layout = QVBoxLayout(self)
        hint = QLabel("删除标签会同时从当前项目移除；不会改动本机视频文件。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for tag in self.tags:
            count = self.usage.get(tag, 0)
            item = QListWidgetItem(f"{tag}（当前项目 {count} 条）" if count else tag)
            item.setData(Qt.ItemDataRole.UserRole, tag)
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        self.delete_btn = QPushButton("删除选中标签")
        self.delete_btn.clicked.connect(self.delete_selected)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def delete_selected(self):
        items = self.list.selectedItems()
        if not items:
            QMessageBox.information(self, "未选择标签", "先在列表中选择要删除的标签。")
            return
        names = [str(item.data(Qt.ItemDataRole.UserRole)) for item in items]
        used = sum(self.usage.get(name, 0) for name in names)
        message = f"删除 {len(names)} 个标签：{'、'.join(names)}？"
        if used:
            message += f"\n其中标签已用于当前项目 {used} 条视频，删除后会从这些视频移除。"
        if QMessageBox.question(self, "确认删除标签", message) != QMessageBox.StandardButton.Yes:
            return
        for item in items:
            self.list.takeItem(self.list.row(item))
        removed = set(names)
        self.tags = [tag for tag in self.tags if tag not in removed]
        for tag in removed:
            self.usage.pop(tag, None)


class BatchTagDialog(QDialog):
    """把标签批量添加、移除或替换到选中的视频。"""

    def __init__(self, tags: list[str], selected_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("批量打标签")
        self.resize(420, 430)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"将对选中的 {selected_count} 条视频执行："))
        self.mode = QComboBox()
        self.mode.addItem("添加到现有标签", "add")
        self.mode.addItem("从现有标签移除", "remove")
        self.mode.addItem("替换为选中的标签", "replace")
        layout.addWidget(self.mode)
        self.list = QListWidget()
        for tag in tags:
            item = QListWidgetItem(tag)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        quick = QHBoxLayout()
        all_btn = QPushButton("全选标签")
        none_btn = QPushButton("清空勾选")
        all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        quick.addWidget(all_btn)
        quick.addWidget(none_btn)
        quick.addStretch(1)
        layout.addLayout(quick)
        buttons = QHBoxLayout()
        cancel_btn = QPushButton("取消")
        ok_btn = QPushButton("确定")
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(ok_btn)
        layout.addLayout(buttons)

    def _set_all(self, state):
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(state)

    def selected_tags(self) -> list[str]:
        return [self.list.item(index).text() for index in range(self.list.count())
                if self.list.item(index).checkState() == Qt.CheckState.Checked]


class TagManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("本机视频标签整理工具")
        self.resize(1220, 790)
        self.project: dict | None = None
        self.xlsx_path: Path | None = None
        self.batch_mode = False
        self.tag_library = load_tag_library()
        state = load_state()
        self.recent = state.get("recent") if isinstance(state.get("recent"), list) else []
        self._build_ui()
        self._set_enabled(False)
        self._refresh_recent_menu()
        self.status.setText(f"先选择本机视频文件夹，或打开已有标签 Excel。当前标签库 {len(self.tag_library)} 个标签，可直接新建。")
        self._restore_last_session()
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(20000)
        self.autosave_timer.timeout.connect(self.save_working_state)
        self.autosave_timer.start()

    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        toolbar = QHBoxLayout()
        self.new_btn = QPushButton("选择视频文件夹…")
        self.open_btn = QPushButton("打开标签 Excel…")
        self.save_btn = QPushButton("保存标签 Excel")
        self.export_btn = QPushButton("另存为 Excel…")
        self.remove_btn = QPushButton("移除选中行")
        self.quick_tag_btn = QPushButton("＋ 新建标签")
        self.new_btn.clicked.connect(self.choose_folder)
        self.open_btn.clicked.connect(self.open_xlsx)
        self.save_btn.clicked.connect(self.save_current)
        self.export_btn.clicked.connect(self.export_xlsx)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.quick_tag_btn.clicked.connect(self.add_tag_value)
        for button in (self.new_btn, self.open_btn, self.quick_tag_btn, self.save_btn, self.export_btn, self.remove_btn):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        outer.addLayout(toolbar)

        self.status = QLabel("先选择本机视频文件夹，或打开已有标签 Excel。此工具只整理本地视频并导出标签清单。")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#53627a;padding:6px 2px;")
        outer.addWidget(self.status)
        self.library_label = QLabel()
        self.library_label.setWordWrap(True)
        self.library_label.setStyleSheet("color:#687386;font-size:11px;padding:0 2px 4px 2px;")
        library_row = QHBoxLayout()
        library_row.addWidget(self.library_label, 1)
        self.manage_tags_btn = QPushButton("管理标签库…")
        self.manage_tags_btn.clicked.connect(self.manage_tags)
        library_row.addWidget(self.manage_tags_btn)
        outer.addLayout(library_row)
        self._update_library_label()

        list_bar = QHBoxLayout()
        list_bar.addWidget(QLabel("视频列表（Ctrl/Shift 可多选）"))
        list_bar.addStretch(1)
        self.recent_btn = QPushButton("最近文件夹…")
        self.recent_menu = QMenu(self.recent_btn)
        self.recent_btn.setMenu(self.recent_menu)
        list_bar.addWidget(self.recent_btn)
        self.rescan_btn = QPushButton("重新扫描")
        self.rescan_btn.clicked.connect(self.rescan_current_folder)
        list_bar.addWidget(self.rescan_btn)
        self.batch_mode_btn = QPushButton("进入批量打标")
        self.batch_mode_btn.clicked.connect(self.toggle_batch_mode)
        list_bar.addWidget(self.batch_mode_btn)
        self.select_all_btn = QPushButton("全选视频")
        self.clear_sel_btn = QPushButton("清空选择")
        self.batch_tag_btn = QPushButton("批量打标签…")
        self.select_all_btn.clicked.connect(self.table_select_all)
        self.clear_sel_btn.clicked.connect(self.table_clear_selection)
        self.batch_tag_btn.clicked.connect(self.batch_tag_selected)
        list_bar.addWidget(self.select_all_btn)
        list_bar.addWidget(self.clear_sel_btn)
        list_bar.addWidget(self.batch_tag_btn)
        outer.addLayout(list_bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["模板ID", "文件名", "时长", "大小", "标签", "状态", "选择"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 74)
        self.table.setColumnWidth(1, 260)
        self.table.setColumnWidth(2, 76)
        self.table.setColumnWidth(3, 88)
        self.table.setColumnWidth(4, 250)
        self.table.setColumnWidth(6, 52)
        self.table.setColumnHidden(6, True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemChanged.connect(self._table_item_changed)
        split.addWidget(self.table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("视频预览"))
        self.video = QVideoWidget()
        self.video.setMinimumSize(360, 235)
        self.video.setStyleSheet("background:#111;border-radius:8px;")
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.player.errorOccurred.connect(self._preview_error)
        self.player.mediaStatusChanged.connect(self._preview_status_changed)
        right_layout.addWidget(self.video)
        controls = QHBoxLayout()
        self.play_btn = QPushButton("播放")
        self.stop_btn = QPushButton("停止")
        self.play_btn.clicked.connect(self.player.play)
        self.stop_btn.clicked.connect(self.player.stop)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.stop_btn)
        self.preview_status = QLabel("选择一条视频开始预览")
        controls.addWidget(self.preview_status, 1)
        right_layout.addLayout(controls)
        nav = QHBoxLayout()
        self.prev_btn = QPushButton("上一个视频 (A)")
        self.next_btn = QPushButton("下一个视频 (D)")
        self.prev_btn.setToolTip("快捷键 A")
        self.next_btn.setToolTip("快捷键 D")
        self.prev_btn.clicked.connect(self.select_previous_video)
        self.next_btn.clicked.connect(self.select_next_video)
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.next_btn)
        nav.addStretch(1)
        right_layout.addLayout(nav)
        self.prev_shortcut = QShortcut(QKeySequence("A"), self)
        self.prev_shortcut.activated.connect(self.select_previous_video)
        self.next_shortcut = QShortcut(QKeySequence("D"), self)
        self.next_shortcut.activated.connect(self.select_next_video)
        self.prev_tag_shortcut = QShortcut(QKeySequence("W"), self)
        self.prev_tag_shortcut.activated.connect(lambda: self._move_tag(-1))
        self.next_tag_shortcut = QShortcut(QKeySequence("S"), self)
        self.next_tag_shortcut.activated.connect(lambda: self._move_tag(1))

        tag_bar = QHBoxLayout()
        tag_bar.addWidget(QLabel("标签（当前选中视频）"))
        tag_bar.addStretch(1)
        self.add_tag_btn = QPushButton("＋ 新建")
        self.remove_tag_btn = QPushButton("删除…")
        self.add_tag_btn.clicked.connect(self.add_tag_value)
        self.remove_tag_btn.clicked.connect(self.remove_tag_value)
        tag_bar.addWidget(self.add_tag_btn)
        tag_bar.addWidget(self.remove_tag_btn)
        right_layout.addLayout(tag_bar)
        self.tag_list = QListWidget()
        self.tag_list.setMaximumHeight(220)
        self.tag_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tag_list.itemChanged.connect(self._tag_item_changed)
        right_layout.addWidget(self.tag_list)
        self.tags_note = QLabel("W/S 在标签间移动，空格键勾选/取消；勾选会应用到当前选中的视频。")
        self.tags_note.setWordWrap(True)
        self.tags_note.setStyleSheet("color:#687386;font-size:11px;")
        right_layout.addWidget(self.tags_note)
        right_layout.addStretch(1)
        split.addWidget(right)
        split.setSizes([740, 430])
        outer.addWidget(split, 1)
        self.setCentralWidget(central)

    def _set_enabled(self, enabled: bool):
        for control in (self.save_btn, self.export_btn, self.remove_btn, self.select_all_btn,
                        self.clear_sel_btn, self.rescan_btn, self.batch_mode_btn, self.play_btn,
                        self.stop_btn, self.prev_btn, self.next_btn):
            control.setEnabled(enabled)
        # 标签库独立于文件夹，新建/删除标签始终可点。
        self.add_tag_btn.setEnabled(True)
        self.quick_tag_btn.setEnabled(True)
        self.remove_tag_btn.setEnabled(True)
        self._update_batch_button()

    def choose_folder(self):
        start = self.project.get("source_root", "") if self.project else str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择本机视频文件夹", start)
        if not selected:
            return
        try:
            if self.project:
                self.project = self._merge_library_into(self.project)
                self.project = self._scan_folder(selected, self.project)
            else:
                self.project = self._scan_folder(selected, None)
                self.project = self._merge_library_into(self.project)
            self.xlsx_path = None
            self._confirm_changed_files()
            self._refresh()
            self._select_first()
            self.status.setText(f"扫描完成：{len([v for v in self.project['videos'] if not v.get('missing')])} 个视频。请逐条添加标签，然后保存 Excel。")
            self._set_enabled(True)
            self.save_working_state()
        except Exception as exc:
            QMessageBox.critical(self, "无法读取视频文件夹", str(exc))

    def open_xlsx(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开标签清单", str(Path.home()), "Excel 标签清单 (*.xlsx)")
        if not path:
            return
        try:
            project = load_xlsx(path)
            root = Path(project["source_root"])
            if not root.is_dir():
                selected = QFileDialog.getExistingDirectory(self, "请选择原视频文件夹", str(Path.home()))
                if not selected:
                    return
                root = Path(selected)
            project = self._merge_library_into(project)
            project = self._scan_folder(root, project)
            self.project = project
            self.xlsx_path = Path(path)
            self._confirm_changed_files()
            self._refresh()
            self._select_first()
            self.status.setText(f"已打开：{path}。修改标签后保存，Excel 清单可单独留存或交接。")
            self._set_enabled(True)
            self.save_working_state()
        except Exception as exc:
            QMessageBox.critical(self, "无法打开标签清单", str(exc))

    def _update_library_label(self):
        text = "、".join(self.tag_library)
        self.library_label.setText(f"当前标签库（{len(self.tag_library)}）：{text}")

    @staticmethod
    def _same_root(left, right) -> bool:
        try:
            return Path(str(left)).resolve() == Path(str(right)).resolve()
        except OSError:
            return str(left) == str(right)

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        if not self.recent:
            action = self.recent_menu.addAction("暂无记录")
            action.setEnabled(False)
            return
        for entry in self.recent[:8]:
            root = str(entry.get("source_root") or "")
            label = f"{Path(root).name or root}  —  {root}"
            action = self.recent_menu.addAction(label)
            action.triggered.connect(lambda checked=False, item=entry: self._open_recent(item))

    def _open_recent(self, entry: dict):
        project = entry.get("project")
        if not isinstance(project, dict):
            return
        if not self._restore_project(project, entry.get("xlsx_path", ""), startup=False):
            QMessageBox.warning(self, "无法打开最近文件夹", f"文件夹不存在或无法读取：\n{project.get('source_root', '')}")

    def _restore_project(self, project: dict, xlsx_path: str = "", startup: bool = False) -> bool:
        root = Path(str(project.get("source_root") or ""))
        if not root.is_dir():
            self.status.setText("上次工作的视频文件夹不存在；请从“最近文件夹…”选择，或重新选择文件夹。")
            return False
        try:
            project = self._merge_library_into(dict(project))
        except Exception as exc:
            self.status.setText(f"恢复工作失败：{exc}")
            return False
        self.project = project
        self.xlsx_path = Path(xlsx_path) if xlsx_path and Path(xlsx_path).is_file() else None
        changed = [row for row in project.get("videos", []) if row.get("changed")]
        self._refresh()
        self._select_first()
        self._set_enabled(True)
        if startup:
            self.status.setText(
                f"已恢复上次工作（使用缓存，未重新扫描）：{root}"
                + (f"；{len(changed)} 个视频上次标记为有变化" if changed else "")
                + "。需要检查新增/删除/改动视频时，请点“重新扫描”。"
            )
        else:
            self.status.setText(f"已打开最近文件夹（使用缓存）：{root}。需要检查文件变化请点“重新扫描”。")
        self.save_working_state()
        return True

    def rescan_current_folder(self):
        if not self.project:
            return
        root = Path(str(self.project.get("source_root") or ""))
        if not root.is_dir():
            QMessageBox.warning(self, "文件夹不存在", f"找不到：{root}")
            return
        try:
            self.project = self._merge_library_into(self.project)
            self.project = self._scan_folder(root, self.project)
            self._confirm_changed_files()
            self._refresh()
            self._select_first()
            self._set_enabled(True)
            self.save_working_state()
            self.status.setText(f"已重新扫描：{root}")
        except Exception as exc:
            QMessageBox.critical(self, "重新扫描失败", str(exc))

    def _restore_last_session(self) -> bool:
        state = load_state()
        self.recent = state.get("recent") if isinstance(state.get("recent"), list) else self.recent
        self._refresh_recent_menu()
        project = state.get("last_project")
        if not isinstance(project, dict):
            return False
        return self._restore_project(project, str(state.get("last_xlsx") or ""), startup=True)

    def save_working_state(self):
        state = load_state()
        state["version"] = 1
        state["recent"] = self.recent
        if self.project:
            root = str(self.project.get("source_root") or "")
            entry = {"source_root": root, "xlsx_path": str(self.xlsx_path) if self.xlsx_path else "",
                     "project": self.project, "updated_at": time.time()}
            self.recent = [entry] + [item for item in self.recent
                                     if not self._same_root(item.get("source_root"), root)]
            self.recent = self.recent[:8]
            state["recent"] = self.recent
            state["last_project"] = self.project
            state["last_xlsx"] = entry["xlsx_path"]
        try:
            save_state(state)
        except OSError as exc:
            if not getattr(self, "_state_warning_shown", False):
                self._state_warning_shown = True
                QMessageBox.warning(self, "工作状态保存失败", f"无法保存自动缓存：{exc}")
        self._refresh_recent_menu()

    def closeEvent(self, event):
        self.save_working_state()
        super().closeEvent(event)

    def _merge_library_into(self, project: dict) -> dict:
        """把全局标签库与当前文件夹标签合并，跨文件夹保留。"""
        tags = list(project.get("tags") or [])
        known = {tag.casefold() for tag in tags}
        for tag in tags:
            if tag.casefold() not in {item.casefold() for item in self.tag_library}:
                self.tag_library.append(tag)
        for tag in self.tag_library:
            if tag.casefold() not in known:
                tags.append(tag)
                known.add(tag.casefold())
        project["tags"] = tags
        try:
            save_tag_library(self.tag_library)
        except OSError:
            pass
        self._update_library_label()
        return project

    def _scan_folder(self, root, project=None):
        dialog = QProgressDialog("正在扫描本机视频…", "", 0, 0, self)
        dialog.setCancelButton(None)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.show()
        def update(done, total, name):
            dialog.setRange(0, max(1, total))
            dialog.setValue(done)
            dialog.setLabelText(f"正在检查视频 {done}/{total}：{name}")
            QApplication.processEvents()
        try:
            if project is None:
                return new_project(root, progress=update)
            return scan_folder(root, project, progress=update)
        finally:
            dialog.close()

    def _confirm_changed_files(self):
        if not self.project:
            return
        changed = [row for row in self.project.get("videos", []) if row.get("changed")]
        if not changed:
            return
        names = "\n".join(row.get("name", row.get("relative_path", "")) for row in changed[:12])
        suffix = "\n……" if len(changed) > 12 else ""
        answer = QMessageBox.question(
            self, "视频内容有变化",
            f"检测到 {len(changed)} 个文件与上次打标时的内容不同。标签会保留。\n\n{names}{suffix}\n\n确认将新文件内容作为这些视频的当前版本？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            for row in changed:
                row["changed"] = False
                row.pop("previous_fingerprint", None)

    @staticmethod
    def _status_text(row_data: dict) -> str:
        if row_data.get("missing"):
            return "文件不存在"
        if row_data.get("changed"):
            return "文件已变化"
        return "已打标" if row_data.get("tags") else "未打标"

    def _refresh(self):
        checked = set(self._checked_paths())
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            if not self.project:
                return
            for row_data in self.project.get("videos", []):
                row = self.table.rowCount()
                self.table.insertRow(row)
                relative = row_data.get("relative_path")
                values = [row_data.get("id", ""), row_data.get("name", ""),
                          f"{row_data['duration']:.1f}s" if row_data.get("duration") is not None else "未知",
                          f"{row_data.get('size_mb', 0):.1f} MB", "；".join(row_data.get("tags", [])),
                          self._status_text(row_data)]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole, relative)
                    self.table.setItem(row, col, item)
                check = QTableWidgetItem("")
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                check.setCheckState(Qt.CheckState.Checked if relative in checked else Qt.CheckState.Unchecked)
                self.table.setItem(row, 6, check)
            self.table.setColumnHidden(6, not self.batch_mode)
        finally:
            self.table.blockSignals(False)
        self._refresh_tag_checkboxes()
        self._update_batch_button()

    def _select_first(self):
        if self.table.rowCount() and not self.table.selectionModel().selectedRows():
            self.table.selectRow(0)

    def table_select_all(self):
        if not self.table.rowCount():
            return
        if self.batch_mode:
            self.table.blockSignals(True)
            try:
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, 6)
                    if item:
                        item.setCheckState(Qt.CheckState.Checked)
            finally:
                self.table.blockSignals(False)
            self._update_batch_button()
        else:
            self.table.selectAll()

    def table_clear_selection(self):
        if self.batch_mode:
            self.table.blockSignals(True)
            try:
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, 6)
                    if item:
                        item.setCheckState(Qt.CheckState.Unchecked)
            finally:
                self.table.blockSignals(False)
            self._update_batch_button()
        else:
            self.table.clearSelection()

    def batch_tag_selected(self):
        if not self.project:
            return
        paths = self._checked_paths() if self.batch_mode else self._selected_paths()
        if not paths:
            hint = "先在“选择”列勾选一条或多条视频。" if self.batch_mode else "先在视频列表中选中一条或多条视频。"
            QMessageBox.information(self, "未选择视频", hint)
            return
        if not self.tag_library:
            QMessageBox.information(self, "标签库为空", "请先新建标签。")
            return
        dialog = BatchTagDialog(self.tag_library, len(paths), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.selected_tags()
        mode = str(dialog.mode.currentData())
        if not chosen and mode != "replace":
            QMessageBox.information(self, "未勾选标签", "请勾选要添加或移除的标签。")
            return
        if mode == "replace" and not chosen:
            if QMessageBox.question(self, "清空标签", f"将清空选中的 {len(paths)} 条视频的全部标签，继续？") != QMessageBox.StandardButton.Yes:
                return
        for tag in chosen:
            if tag not in self.project.get("tags", []):
                self.project.setdefault("tags", []).append(tag)
        changed = {}
        for relative in paths:
            video = next((v for v in self.project.get("videos", []) if v.get("relative_path") == relative), None)
            if not video:
                continue
            current = set(video.get("tags", []))
            if mode == "add":
                current.update(chosen)
            elif mode == "remove":
                current.difference_update(chosen)
            else:
                current = set(chosen)
            set_video_tags(self.project, relative, list(current))
            changed[relative] = video
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            relative = item.data(Qt.ItemDataRole.UserRole) if item else None
            video = changed.get(relative)
            if video:
                self.table.setItem(row, 4, QTableWidgetItem("；".join(video.get("tags", []))))
                self.table.setItem(row, 5, QTableWidgetItem(self._status_text(video)))
        self._refresh_tag_checkboxes()
        self.save_working_state()
        action = {"add": "添加", "remove": "移除", "replace": "替换"}.get(mode, "更新")
        self.status.setText(f"已对 {len(changed)} 条视频{action}标签。")

    def _checked_paths(self) -> list[str]:
        paths = []
        for row in range(self.table.rowCount()):
            check = self.table.item(row, 6)
            if check and check.checkState() == Qt.CheckState.Checked:
                item = self.table.item(row, 0)
                if item:
                    paths.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return paths

    def _table_item_changed(self, item):
        if item and item.column() == 6:
            self._update_batch_button()

    def _update_batch_button(self):
        if not hasattr(self, "batch_tag_btn"):
            return
        if not self.project:
            self.batch_tag_btn.setEnabled(False)
        elif self.batch_mode:
            self.batch_tag_btn.setEnabled(bool(self._checked_paths()))
        else:
            self.batch_tag_btn.setEnabled(bool(self._selected_paths()))

    def toggle_batch_mode(self):
        self.batch_mode = not self.batch_mode
        self.table.setColumnHidden(6, not self.batch_mode)
        self.batch_mode_btn.setText("退出批量模式" if self.batch_mode else "进入批量打标")
        if not self.batch_mode:
            self.table.blockSignals(True)
            try:
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, 6)
                    if item:
                        item.setCheckState(Qt.CheckState.Unchecked)
            finally:
                self.table.blockSignals(False)
        self._update_batch_button()
        if self.batch_mode:
            self.status.setText("批量打标模式：勾选“选择”列中的视频，再点“批量打标签…”。")
        else:
            self.status.setText("已退出批量打标模式。")

    def _selected_paths(self) -> list[str]:
        paths = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item:
                paths.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return paths

    def _current_video(self) -> dict | None:
        if not self.project:
            return None
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        path = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((v for v in self.project["videos"] if v.get("relative_path") == path), None)

    def select_previous_video(self):
        if not self.project or not self.table.rowCount():
            return
        current = self.table.currentRow()
        previous_row = self.table.rowCount() - 1 if current <= 0 else current - 1
        self.table.selectRow(previous_row)
        item = self.table.item(previous_row, 0)
        if item:
            self.table.scrollToItem(item)
        self.status.setText(f"已切换到第 {previous_row + 1} 条视频。")

    def select_next_video(self):
        if not self.project or not self.table.rowCount():
            return
        current = self.table.currentRow()
        next_row = 0 if current < 0 or current + 1 >= self.table.rowCount() else current + 1
        self.table.selectRow(next_row)
        item = self.table.item(next_row, 0)
        if item:
            self.table.scrollToItem(item)
        self.status.setText(f"已切换到第 {next_row + 1} 条视频。")

    def _selection_changed(self):
        self._update_batch_button()
        video = self._current_video()
        if not video or video.get("missing"):
            self.player.stop()
            self.preview_status.setText("选择一条存在的视频开始预览")
            self._refresh_tag_checkboxes()
            return
        full = Path(self.project["source_root"]) / video["relative_path"]
        self.player.setSource(QUrl.fromLocalFile(str(full)))
        self.player.play()
        self.preview_status.setText(f"正在加载预览：{full.name}")
        self._refresh_tag_checkboxes()

    def _preview_error(self, _error, message: str):
        self.preview_status.setText(f"无法预览（{message or '格式不支持'}）；仍可按文件名打标。")

    def _preview_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.preview_status.setText("预览已加载")
        elif status == QMediaPlayer.MediaStatus.BufferingMedia:
            self.preview_status.setText("预览缓冲中…")
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.preview_status.setText("该视频格式无法预览；仍可按文件名打标。")
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.preview_status.setText("循环播放中")

    def _move_tag(self, delta: int):
        if not self.project or not self.tag_list.count():
            return
        row = self.tag_list.currentRow()
        next_row = 0 if row < 0 else (row + delta) % self.tag_list.count()
        self.tag_list.setCurrentRow(next_row)
        self.tag_list.setFocus()

    def _tag_item_changed(self, item: QListWidgetItem):
        if not self.project or item is None:
            return
        if not self._toggle_tag(item.text(), item.checkState() == Qt.CheckState.Checked, refresh_list=False):
            self._refresh_tag_checkboxes()

    def _refresh_tag_checkboxes(self):
        current_tag = self.tag_list.currentItem().text() if self.tag_list.currentItem() else ""
        current = self._current_video()
        selected_tags = set(current.get("tags", [])) if current else set()
        self.tag_list.blockSignals(True)
        try:
            self.tag_list.clear()
            for tag in self.project.get("tags", []) if self.project else []:
                item = QListWidgetItem(tag)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(Qt.CheckState.Checked if tag in selected_tags else Qt.CheckState.Unchecked)
                self.tag_list.addItem(item)
            if current_tag:
                for row in range(self.tag_list.count()):
                    if self.tag_list.item(row).text() == current_tag:
                        self.tag_list.setCurrentRow(row)
                        break
                else:
                    self.tag_list.setCurrentRow(0)
            elif self.tag_list.count():
                self.tag_list.setCurrentRow(0)
        finally:
            self.tag_list.blockSignals(False)

    def _toggle_tag(self, tag: str, checked: bool, refresh_list: bool = True) -> bool:
        if not self.project:
            return False
        paths = self._selected_paths()
        if not paths:
            return False
        changed = {}
        proposed = {}
        for relative in paths:
            video = next((v for v in self.project["videos"] if v.get("relative_path") == relative), None)
            if not video:
                continue
            current = set(video.get("tags", []))
            if checked:
                current.add(tag)
            else:
                current.discard(tag)
            proposed[relative] = list(current)
        for relative, labels in proposed.items():
            video = next(v for v in self.project["videos"] if v.get("relative_path") == relative)
            set_video_tags(self.project, relative, labels)
            changed[relative] = video
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            relative = item.data(Qt.ItemDataRole.UserRole) if item else None
            video = changed.get(relative)
            if video:
                self.table.setItem(row, 4, QTableWidgetItem("；".join(video.get("tags", []))))
                self.table.setItem(row, 5, QTableWidgetItem(self._status_text(video)))
        if refresh_list:
            self._refresh_tag_checkboxes()
        self.save_working_state()
        return True

    def add_tag_value(self):
        value, ok = QInputDialog.getText(self, "新建标签", "标签名称：")
        if not ok or not value.strip():
            return
        try:
            add_library_tag(self.tag_library, value)
            save_tag_library(self.tag_library)
            self._update_library_label()
            if self.project:
                add_tag(self.project, value)
                self._refresh()
                self.save_working_state()
                self.status.setText(f"已创建标签：{value.strip()}。它会保留在本机标签库，并可用于其他文件夹。")
            else:
                self.status.setText(f"已创建标签：{value.strip()}。当前标签库 {len(self.tag_library)} 个；选择文件夹后即可使用。")
        except CatalogError as exc:
            QMessageBox.warning(self, "标签无效", str(exc))
        except OSError as exc:
            QMessageBox.warning(self, "标签库保存失败", str(exc))

    def manage_tags(self):
        usage: dict[str, int] = {}
        if self.project:
            for row in self.project.get("videos", []):
                for tag in row.get("tags", []):
                    usage[tag] = usage.get(tag, 0) + 1
        dialog = TagLibraryDialog(self.tag_library, usage, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        removed = [tag for tag in self.tag_library if tag not in dialog.tags]
        if not removed:
            return
        self.tag_library = list(dialog.tags)
        if self.project:
            keep = set(self.tag_library)
            self.project["tags"] = [tag for tag in self.project.get("tags", []) if tag in keep]
            for row in self.project.get("videos", []):
                row["tags"] = [tag for tag in row.get("tags", []) if tag in keep]
        try:
            save_tag_library(self.tag_library)
        except OSError as exc:
            QMessageBox.warning(self, "标签库保存失败", str(exc))
        self._update_library_label()
        if self.project:
            self._refresh()
            self.save_working_state()
        self.status.setText(f"已从标签库删除 {len(removed)} 个标签。")

    def remove_tag_value(self):
        self.manage_tags()

    def remove_selected(self):
        if not self.project:
            return
        paths = set(self._selected_paths())
        if not paths:
            return
        answer = QMessageBox.question(self, "移除视频", f"从当前标签清单移除 {len(paths)} 条视频？\n不会删除本机原视频文件。")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.project["videos"] = [row for row in self.project["videos"] if row.get("relative_path") not in paths]
        self._refresh()
        self.save_working_state()
        self.status.setText(f"已从清单移除 {len(paths)} 条；本机视频文件不会被删除。")

    def save_current(self):
        if not self.project:
            return
        if self.xlsx_path is None:
            return self.export_xlsx()
        self._save_to(self.xlsx_path)

    def export_xlsx(self):
        if not self.project:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出视频标签 Excel", str(Path(self.project["source_root"]) / "template-tags.xlsx"), "Excel 标签清单 (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        self.xlsx_path = Path(path)
        self._save_to(self.xlsx_path)

    def _save_to(self, path: Path):
        try:
            save_xlsx(self.project, path)
            self.save_working_state()
            self.status.setText(f"已保存标签清单：{path}。这份 Excel 可独立保存、查看或交接。")
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("本机视频标签整理工具")
    window = TagManager()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
