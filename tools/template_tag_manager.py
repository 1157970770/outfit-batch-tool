#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机视频打标工具：只读本地文件、管理自由标签并导出 XLSX，不联网。"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QProgressDialog,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QInputDialog,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.template_catalog import (  # noqa: E402
    CatalogError, DEFAULT_TAGS, add_library_tag, add_tag, load_tag_library,
    new_project, normalize_tags, save_tag_library, scan_folder, save_xlsx,
    load_xlsx, set_video_tags,
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


class TagManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("本机视频标签整理工具")
        self.resize(1220, 790)
        self.project: dict | None = None
        self.xlsx_path: Path | None = None
        self.checks: dict[str, QCheckBox] = {}
        self.tag_library = load_tag_library()
        self._build_ui()
        self._set_enabled(False)
        self.status.setText(f"先选择本机视频文件夹，或打开已有标签 Excel。当前标签库 {len(self.tag_library)} 个标签，可直接新建。")

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

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["模板ID", "文件名", "时长", "大小", "标签", "状态"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 74)
        self.table.setColumnWidth(1, 260)
        self.table.setColumnWidth(2, 76)
        self.table.setColumnWidth(3, 88)
        self.table.setColumnWidth(4, 250)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        split.addWidget(self.table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("视频预览"))
        self.video = QVideoWidget()
        self.video.setMinimumSize(360, 235)
        self.video.setStyleSheet("background:#111;border-radius:8px;")
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
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
        right_layout.addWidget(self.tag_list)
        self.tags_note = QLabel("勾选/取消标签会应用到当前选中的视频；多选视频时应用到所有选中行。")
        self.tags_note.setWordWrap(True)
        self.tags_note.setStyleSheet("color:#687386;font-size:11px;")
        right_layout.addWidget(self.tags_note)
        right_layout.addStretch(1)
        split.addWidget(right)
        split.setSizes([740, 430])
        outer.addWidget(split, 1)
        self.setCentralWidget(central)

    def _set_enabled(self, enabled: bool):
        for control in (self.save_btn, self.export_btn, self.remove_btn, self.play_btn, self.stop_btn):
            control.setEnabled(enabled)
        # 标签库独立于文件夹，新建/删除标签始终可点。
        self.add_tag_btn.setEnabled(True)
        self.quick_tag_btn.setEnabled(True)
        self.remove_tag_btn.setEnabled(True)

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
        except Exception as exc:
            QMessageBox.critical(self, "无法打开标签清单", str(exc))

    def _update_library_label(self):
        text = "、".join(self.tag_library)
        self.library_label.setText(f"当前标签库（{len(self.tag_library)}）：{text}")

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
        self.table.setRowCount(0)
        if not self.project:
            return
        for row_data in self.project.get("videos", []):
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [row_data.get("id", ""), row_data.get("name", ""),
                      f"{row_data['duration']:.1f}s" if row_data.get("duration") is not None else "未知",
                      f"{row_data.get('size_mb', 0):.1f} MB", "；".join(row_data.get("tags", [])),
                      self._status_text(row_data)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row_data.get("relative_path"))
                self.table.setItem(row, col, item)
        self._refresh_tag_checkboxes()

    def _select_first(self):
        if self.table.rowCount() and not self.table.selectionModel().selectedRows():
            self.table.selectRow(0)

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

    def _selection_changed(self):
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
            self.preview_status.setText("播放结束")

    def _refresh_tag_checkboxes(self):
        self.tag_list.clear()
        self.checks.clear()
        current = self._current_video()
        selected_tags = set(current.get("tags", [])) if current else set()
        for tag in self.project.get("tags", []) if self.project else []:
            item = QListWidgetItem(self.tag_list)
            checkbox = QCheckBox(tag)
            checkbox.setChecked(tag in selected_tags)
            checkbox.toggled.connect(lambda checked, value=tag: self._toggle_tag(value, checked))
            self.tag_list.setItemWidget(item, checkbox)
            item.setSizeHint(checkbox.sizeHint())
            self.checks[tag] = checkbox

    def _toggle_tag(self, tag: str, checked: bool):
        if not self.project:
            return
        paths = self._selected_paths()
        if not paths:
            return
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
        self._refresh_tag_checkboxes()

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
