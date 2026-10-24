#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换装批量生产器 · 桌面界面（PySide6）。

两个页面：
  · 小批量试跑：随机 3 条，用来调提示词、验效果
  · 大批量生产：按 5 组固定编号或随机 100 条
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from app import config as appconfig  # noqa: E402
from app import license as licensemod  # noqa: E402
from app.core import (  # noqa: E402
    DEFAULT_PROMPT, GROUP_SIZE, TOTAL_TEMPLATES, BatchRunner, BatchSpec,
    EngineError, check_account, load_manifest, plan_batch, unique_batch_dir,
)
from app.storage import StorageConfig, StorageError, open_storage  # noqa: E402
from app.startup import ensure_license  # noqa: E402

APP_TITLE = "换装批量生产器 · 测试版"
APP_VERSION = "0.7"

# 内置授权地址：配置文件里留空就用它，避免"删掉那行 = 免授权"。
# 部署好授权服务后，把地址填到这里并重新打包。
EMBEDDED_LICENSE_SERVER = "https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com"


# ---------------------------------------------------------------------------
# 配置：全部来自 exe 同目录的 配置.ini，界面上不出现任何输入框
# ---------------------------------------------------------------------------

def resolve_license_server(cfg: dict) -> str:
    """配置文件优先，其次用内置地址；两者都空 = 未配置（打包版会拒绝启动）。"""
    return (cfg.get("LICENSE_SERVER") or "").strip() or EMBEDDED_LICENSE_SERVER.strip()


def load_config() -> dict:
    return appconfig.load()


def build_storage(cfg: dict):
    """按后台配置造存储实例；未启用返回 None。配置不合法时抛 StorageError。"""
    if not appconfig.truthy(cfg.get("COS_ENABLED", "")):
        return None
    manual = {
        "bucket": (cfg.get("COS_BUCKET") or "").strip(),
        "region": (cfg.get("COS_REGION") or "").strip(),
        "prefix": (cfg.get("COS_PREFIX") or "").strip(),
        "domain": (cfg.get("COS_DOMAIN") or "").strip(),
    }
    # 配置文件里不放任何密钥：一律走本机已有凭证（环境变量 / ~/.cos / tccli）
    resolved = StorageConfig.from_local_profile(**manual)
    if not (resolved.secret_id and resolved.secret_key):
        raise StorageError(
            "启用了 COS 归档，但本机找不到可用凭证。"
            "请设置环境变量 COS_SECRET_ID/COS_SECRET_KEY，或放进 ~/.cos/credentials.json，"
            "或先跑 tccli auth login --profile agent1"
        )
    return open_storage(resolved)


# ---------------------------------------------------------------------------
# 后台线程
# ---------------------------------------------------------------------------

class BatchWorker(QThread):
    progressed = Signal(dict)
    finished_ok = Signal(dict)
    failed = Signal(str)
    logged = Signal(str)

    def __init__(self, spec: BatchSpec):
        super().__init__()
        self.spec = spec
        self.runner: BatchRunner | None = None

    def run(self) -> None:
        try:
            self.runner = BatchRunner(self.spec, on_event=self._on_event, log=self.logged.emit)
            summary = self.runner.run()
            self.finished_ok.emit(summary)
        except Exception as exc:  # 不让异常炸掉界面
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")

    def _on_event(self, evt: dict) -> None:
        if evt.get("type") == "progress":
            self.progressed.emit(evt)


class ManifestWorker(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, source: str):
        super().__init__()
        self.source = source

    def run(self) -> None:
        try:
            self.loaded.emit(load_manifest(self.source))
        except Exception as exc:
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# 设置对话框
# ---------------------------------------------------------------------------

class BatchPanel(QWidget):
    started = Signal()
    ended = Signal()

    def __init__(self, cfg: dict, small: bool, window: "MainWindow"):
        super().__init__()
        self.cfg = cfg
        self.small = small
        self.window_ref = window
        self.templates: list[dict] = []
        self.worker: BatchWorker | None = None
        self._started_at = 0.0
        self._log_file: Path | None = None
        self._build()

    # ---- 布局 ----
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        # 服装图
        image_row = QHBoxLayout()
        self.image_edit = QLineEdit()
        self.image_edit.setPlaceholderText("选择一张无头模特图（服装 + 上身效果）")
        pick = QPushButton("选择图片…")
        pick.clicked.connect(self._pick_image)
        image_row.addWidget(QLabel("服装图"), 0)
        image_row.addWidget(self.image_edit, 1)
        image_row.addWidget(pick)
        root.addLayout(image_row)

        # 批次号
        id_row = QHBoxLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("例如 20261024A（只用字母和数字；重复会自动加 -2）")
        id_row.addWidget(QLabel("批次号"), 0)
        id_row.addWidget(self.id_edit, 1)
        root.addLayout(id_row)

        # 批次参数
        param_row = QHBoxLayout()
        if self.small:
            tip = QLabel("固定随机 3 条 · 720P / 7s / 9:16 · 无音轨")
            tip.setStyleSheet("color:#666;")
            param_row.addWidget(tip)
        else:
            param_row.addWidget(QLabel("模板"))
            self.group_combo = QComboBox()
            for i in range(1, TOTAL_TEMPLATES // GROUP_SIZE + 1):
                start = (i - 1) * GROUP_SIZE + 1
                self.group_combo.addItem(f"第 {i} 组（{start:03d}–{start + GROUP_SIZE - 1:03d}）", i)
            self.group_combo.addItem("随机 100 条", 0)
            param_row.addWidget(self.group_combo)

            param_row.addSpacing(16)
            param_row.addWidget(QLabel("并发"))
            self.concurrency_combo = QComboBox()
            for value in (3, 10, 50, 100):
                self.concurrency_combo.addItem(str(value), value)
            self.concurrency_combo.setCurrentIndex(0)
            param_row.addWidget(self.concurrency_combo)
            param_row.addStretch(1)
        root.addLayout(param_row)

        # 提示词
        root.addWidget(QLabel("提示词（可修改，改完即用；原文会存进 batch.json）"))
        self.prompt_edit = QPlainTextEdit(DEFAULT_PROMPT)
        self.prompt_edit.setMinimumHeight(96)
        self.prompt_edit.setMaximumHeight(140)
        root.addWidget(self.prompt_edit)

        # 操作
        action_row = QHBoxLayout()
        self.start_btn = QPushButton("开始生产" if not self.small else "试跑 3 条")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start)
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.folder_btn = QPushButton("打开输出目录")
        self.folder_btn.setEnabled(False)
        self.folder_btn.clicked.connect(self._open_folder)
        action_row.addWidget(self.start_btn)
        action_row.addWidget(self.pause_btn)
        action_row.addWidget(self.stop_btn)
        action_row.addStretch(1)
        action_row.addWidget(self.folder_btn)
        root.addLayout(action_row)

        # 进度
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setMaximum(3 if self.small else 100)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m")
        root.addWidget(self.progress)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color:#555;")
        root.addWidget(self.status_label)

        # 结果表
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["模板", "状态", "耗时", "大小", "备注"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 80)
        root.addWidget(self.table, 1)

        self._set_running(False)

    # ---- 交互 ----
    def _pick_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择服装图", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if path:
            self.image_edit.setText(path)

    def apply_templates(self, templates: list[dict]):
        self.templates = templates

    def _toggle_pause(self):
        if not self.worker or not self.worker.runner:
            return
        if self.pause_btn.text() == "暂停":
            self.worker.runner.pause()
            self.pause_btn.setText("继续")
        else:
            self.worker.runner.resume()
            self.pause_btn.setText("暂停")

    def _stop(self):
        if self.worker and self.worker.runner:
            self.worker.runner.stop()
            self.stop_btn.setEnabled(False)
            self.status_label.setText("正在停止…")

    def _open_path(self, path: Path, what: str) -> None:
        """用系统默认程序打开文件/文件夹；打不开就说清楚为什么。

        原来只用 QDesktopServices.openUrl()：文件不存在或没有关联程序时它会静默失败，
        用户看到的就是"点了没反应"。这里三级兜底 + 明确提示。
        """
        target = Path(path).expanduser().resolve()
        if not target.exists():
            QMessageBox.warning(
                self, "还没生成",
                f"{what}不存在：\n{target}\n\n"
                "如果是「配置文件」，常见原因是程序所在目录没有写权限"
                "（例如直接在压缩包里双击了 exe）。请先完整解压再运行。",
            )
            return

        ok = False
        if os.name == "nt":
            try:
                os.startfile(str(target))   # Windows：交给 Shell，关联程序最可靠
                ok = True
            except OSError:
                ok = False

        if not ok:
            try:
                ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
            except Exception:  # noqa: BLE001
                ok = False

        if not ok:
            QApplication.clipboard().setText(str(target))
            QMessageBox.information(
                self, "打不开",
                f"系统没能自动打开，路径已复制到剪贴板：\n{target}\n\n"
                "可以粘到资源管理器地址栏，或用记事本手动打开。",
            )

    def _open_folder(self):
        target = self._out_dir()
        if target:
            self._open_path(target, "输出目录")

    def _out_dir(self) -> Path | None:
        if not self.id_edit.text().strip():
            return None
        root = Path(self.cfg["OUTPUT_ROOT"])
        safe = "".join(c for c in self.id_edit.text().strip() if c.isalnum() or c in "-_") or "batch"
        return root / safe

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        self.image_edit.setEnabled(not running)
        self.id_edit.setEnabled(not running)
        self.prompt_edit.setEnabled(not running)
        if not self.small:
            self.group_combo.setEnabled(not running)
            self.concurrency_combo.setEnabled(not running)
        if not running:
            self.pause_btn.setText("暂停")

    # ---- 启动 ----
    def start(self):
        if self.window_ref.busy:
            QMessageBox.information(self, "稍等", "已有批次在跑，先停下再开新的。")
            return
        image = self.image_edit.text().strip()
        if not image or not Path(image).exists():
            QMessageBox.warning(self, "缺少服装图", "请选择一张本地的服装图。")
            return
        batch_id = self.id_edit.text().strip()
        if not batch_id:
            QMessageBox.warning(self, "缺少批次号", "批次号会作为输出文件夹名，请先填写。")
            return
        if not self.templates:
            QMessageBox.warning(self, "模板未加载", "模板清单没有加载成功，请检查 配置.ini 里的 MANIFEST。")
            return

        key = (self.cfg.get("RH_API_KEY") or "").strip()
        if not key:
            QMessageBox.warning(
                self, "还没配置 Key",
                f"请在 配置.ini 里填写 RH_API_KEY 这一行，保存后点右上角「重载配置」。\n\n"
                f"文件位置：\n{appconfig.config_path()}",
            )
            return

        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "提示词为空", "提示词不能为空。")
            return

        try:
            storage = build_storage(self.cfg)
        except StorageError as exc:
            QMessageBox.warning(self, "COS 配置有误", f"{exc}\n\n可先到「设置」里点“测试连接”。")
            return

        if self.small:
            mode, group, count = "random", None, 3
            concurrency = 3
        else:
            group_value = self.group_combo.currentData()
            if group_value == 0:
                mode, group, count = "random", None, GROUP_SIZE
            else:
                mode, group, count = "group", int(group_value), GROUP_SIZE
            concurrency = int(self.concurrency_combo.currentData())

        try:
            picked = plan_batch(self.templates, mode, group=group, count=count)
        except EngineError as exc:
            QMessageBox.warning(self, "无法开始", str(exc))
            return

        root = Path(self.cfg["OUTPUT_ROOT"])
        out_dir = unique_batch_dir(root, batch_id)

        self.table.setRowCount(0)
        self.progress.setMaximum(len(picked))
        self.progress.setValue(0)
        self._started_at = time.time()
        self._log_file = out_dir / "log.txt"
        out_dir.mkdir(parents=True, exist_ok=True)

        spec = BatchSpec(
            batch_id=batch_id,
            image_path=image,
            prompt=prompt,
            out_dir=out_dir,
            templates=picked,
            api_key=key,
            base=self.cfg.get("base_url") or "https://www.runninghub.cn/openapi/v2",
            concurrency=concurrency,
            storage=storage,
        )
        self.worker = BatchWorker(spec)
        self.worker.progressed.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.logged.connect(self._on_log)

        self._set_running(True)
        self.folder_btn.setEnabled(True)
        self.status_label.setText(f"已提交 {len(picked)} 条 · 并发 {concurrency} · 输出 {out_dir}")
        self.window_ref.set_busy(True)
        self.started.emit()
        self.worker.start()

    # ---- 事件 ----
    def _on_log(self, msg: str):
        stamp = time.strftime("%H:%M:%S")
        self.window_ref.write_log(f"[{stamp}] {msg}")
        if self._log_file:
            try:
                with self._log_file.open("a", encoding="utf-8") as fh:
                    fh.write(f"[{stamp}] {msg}\n")
            except OSError:
                pass

    def _on_progress(self, evt: dict):
        rec = evt["record"]
        done, total = evt["done"], evt["total"]
        self.progress.setValue(done)

        row = self._row_for(rec["id"])
        status, extra = self._render(rec)
        self.table.setItem(row, 1, QTableWidgetItem(status))
        self.table.setItem(row, 2, QTableWidgetItem(f"{rec.get('seconds') or '-'}s"))
        size = (rec.get("verify") or {}).get("size_kb")
        self.table.setItem(row, 3, QTableWidgetItem(f"{size} KB" if size else "-"))
        self.table.setItem(row, 4, QTableWidgetItem(extra))
        color = QColor("#0a7d32") if status == "完成" else (QColor("#b3261e") if status == "失败" else QColor("#444"))
        if extra.startswith("⚠"):
            color = QColor("#a86400")
        for col in range(5):
            item = self.table.item(row, col)
            if item:
                item.setForeground(color)

        elapsed = time.time() - self._started_at
        if done:
            remain = elapsed / done * (total - done)
            self.status_label.setText(
                f"已完成 {done}/{total} · 已用 {self._fmt(elapsed)} · 预计剩余 {self._fmt(remain)}"
            )

    def _render(self, rec: dict) -> tuple[str, str]:
        status = rec.get("status")
        if status == "success":
            warn = (rec.get("verify") or {}).get("warn")
            return "完成", f"⚠ {warn}" if warn else ""
        if status == "failed":
            return "失败", (rec.get("error") or "")[:120]
        if status == "skipped":
            return "跳过", ""
        return "运行中", ""

    def _row_for(self, tid: str) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == tid:
                return row
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(tid))
        for col in range(1, 5):
            self.table.setItem(row, col, QTableWidgetItem(""))
        return row

    def _on_finished(self, summary: dict):
        self._set_running(False)
        self.window_ref.set_busy(False)
        self.progress.setValue(summary["total"])
        self.status_label.setText(
            f"完成 · 成功 {summary['success']} / 失败 {summary['failed']} · 扣费 ¥{summary['cost']}"
        )
        self.ended.emit()
        archive_line = ""
        if summary.get("archived") or summary.get("archive_failed"):
            archive_line = (f"\n已归档到 COS：{summary.get('archived', 0)} 条"
                            f"（失败 {summary.get('archive_failed', 0)} 条）")
        QMessageBox.information(
            self, "批次完成",
            f"批次 {self.id_edit.text()} 已结束\n\n"
            f"成功：{summary['success']}\n失败：{summary['failed']}\n"
            f"扣费：¥{summary['cost']}{archive_line}\n\n输出目录：\n{summary['out_dir']}",
        )
        self._open_folder()

    def _on_failed(self, msg: str):
        self._set_running(False)
        self.window_ref.set_busy(False)
        self.ended.emit()
        QMessageBox.critical(self, "批次异常", msg[:800])

    @staticmethod
    def _fmt(seconds: float) -> str:
        seconds = int(seconds)
        return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# 配置编辑窗口（不依赖系统文件关联）
# ---------------------------------------------------------------------------

class ConfigDialog(QDialog):
    """在程序内编辑 配置.ini：读不出、写不进、没关联程序，都不会再"点了没反应"。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("配置文件 · 配置.ini")
        self.resize(760, 640)
        self.path = appconfig.config_path()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        path_label = QLabel(f"文件位置：{self.path}")
        path_label.setWordWrap(True)
        path_label.setObjectName("pathhint")
        layout.addWidget(path_label)

        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setPlaceholderText("配置内容为空")
        layout.addWidget(self.editor, 1)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        row = QHBoxLayout()
        save_btn = QPushButton("保存并重载")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save)
        notepad_btn = QPushButton("用记事本打开")
        notepad_btn.clicked.connect(self._open_notepad)
        saveas_btn = QPushButton("另存为…")
        saveas_btn.clicked.connect(self._save_as)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        row.addWidget(save_btn)
        row.addWidget(notepad_btn)
        row.addWidget(saveas_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)

        self._load()

    # ---- 读写 ----
    def _load(self):
        text = ""
        if self.path.exists():
            for encoding in ("utf-8-sig", "gbk"):
                try:
                    text = self.path.read_text(encoding=encoding)
                    break
                except (OSError, UnicodeDecodeError):
                    continue
        if not text:
            appconfig.load()          # 尝试按模板生成
            if self.path.exists():
                try:
                    text = self.path.read_text(encoding="utf-8-sig")
                except OSError:
                    text = ""
        self.editor.setPlainText(text or appconfig.TEMPLATE)
        self._refresh_hint()

    def _refresh_hint(self):
        values = appconfig._parse(self.editor.toPlainText()) if hasattr(appconfig, "_parse") else {}
        key = "已填" if (values.get("RH_API_KEY") or "").strip() else "未填"
        manifest = "已填" if (values.get("MANIFEST") or "").strip() else "未填"
        exists = "存在" if self.path.exists() else "尚未生成"
        self.hint.setText(
            f"当前状态：文件{exists}　RH_API_KEY {key}　MANIFEST {manifest}　"
            "（改完点「保存并重载」立即生效，无需重启）"
        )
        self.hint.setStyleSheet("color:#6b6b70; font-size:12px;")

    def _save(self):
        text = self.editor.toPlainText()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(
                self, "保存失败",
                f"{exc}\n\n常见原因：程序目录没有写权限"
                "（比如直接在压缩包里双击了 exe）。\n"
                "请先完整解压到 D 盘等可写目录，或用「另存为」存到文档目录。",
            )
            return
        self.accept()

    def _save_as(self):
        target, _ = QFileDialog.getSaveFileName(self, "另存配置", "配置.ini", "配置 (*.ini);;所有文件 (*)")
        if not target:
            return
        try:
            Path(target).write_text(self.editor.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        QMessageBox.information(
            self, "已另存",
            f"已保存到：\n{target}\n\n注意：程序只读取原位置 {self.path}，"
            "另存的文件需要自己改名替换过去。",
        )

    def _open_notepad(self):
        try:
            if not self.path.exists():
                self.path.write_text(self.editor.toPlainText(), encoding="utf-8")
            os.startfile(str(self.path))
        except OSError as exc:
            QApplication.clipboard().setText(str(self.path))
            QMessageBox.information(
                self, "打不开记事本",
                f"{exc}\n\n路径已复制：\n{self.path}",
            )


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.busy = False
        self.templates: list[dict] = []
        self.manifest_worker: ManifestWorker | None = None
        self._log_path = Path(self.cfg["OUTPUT_ROOT"]) / "logs" / f"app-{time.strftime('%Y%m%d')}.log"

        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.resize(940, 760)
        self._build_ui()
        self._load_manifest_async()

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QWidget()
        header.setObjectName("header")
        head_col = QVBoxLayout(header)
        head_col.setContentsMargins(18, 10, 18, 10)
        head_col.setSpacing(6)

        head_row = QHBoxLayout()
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        reload_btn = QPushButton("重载配置")
        reload_btn.setObjectName("ghost")
        reload_btn.setToolTip("改完 配置.ini 点这里，不用重启")
        reload_btn.clicked.connect(self._reload_config)
        config_btn = QPushButton("配置文件")
        config_btn.setObjectName("ghost")
        config_btn.setToolTip("打开 配置.ini：填 RH_API_KEY / MANIFEST / COS / 授权地址")
        config_btn.clicked.connect(self._open_config_file)
        head_row.addWidget(title)
        head_row.addStretch(1)
        head_row.addWidget(reload_btn)
        head_row.addSpacing(6)
        head_row.addWidget(config_btn)

        # 状态条：四个灯泡，一眼看出还缺什么
        status_row = QHBoxLayout()
        status_row.setSpacing(14)
        self.badge_key = QLabel()
        self.badge_manifest = QLabel("模板清单：加载中…")
        self.badge_license = QLabel()
        self.badge_cos = QLabel()
        self.manifest_label = self.badge_manifest   # 兼容旧引用
        for label in (self.badge_key, self.badge_manifest, self.badge_license, self.badge_cos):
            label.setObjectName("badge")
        status_row.addWidget(self.badge_key)
        status_row.addWidget(self.badge_manifest)
        status_row.addWidget(self.badge_license)
        status_row.addWidget(self.badge_cos)
        status_row.addStretch(1)

        head_col.addLayout(head_row)
        head_col.addLayout(status_row)
        layout.addWidget(header)

        # 缺必填项时的提示条（不阻塞操作，只是提醒）
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)

        self.tabs = QTabWidget()
        self.small_panel = BatchPanel(self.cfg, small=True, window=self)
        self.big_panel = BatchPanel(self.cfg, small=False, window=self)
        self.tabs.addTab(self.small_panel, "小批量试跑")
        self.tabs.addTab(self.big_panel, "大批量生产")
        layout.addWidget(self.tabs, 1)

        footer = QLabel("720P · 7s · 9:16 · 无音轨 ｜ 结果链接 24 小时失效，本工具已自动下载归档")
        footer.setObjectName("footer")
        layout.addWidget(footer)

        self.setCentralWidget(central)
        self.setStyleSheet(STYLE)
        self._refresh_status()

    # ---- 状态条 ----
    def _refresh_status(self):
        """四个状态：RH Key / 清单 / 授权 / COS。只读配置与本地缓存，不联网。"""
        cfg = self.cfg

        def badge(label: QLabel, text: str, tone: str) -> None:
            label.setText(text)
            label.setProperty("tone", tone)
            label.style().unpolish(label)
            label.style().polish(label)

        key_ok = bool((cfg.get("RH_API_KEY") or "").strip())
        badge(self.badge_key, "RH Key：已配置" if key_ok else "RH Key：未配置",
              "ok" if key_ok else "bad")

        cos_on = appconfig.truthy(cfg.get("COS_ENABLED", ""))
        badge(self.badge_cos, "COS：开" if cos_on else "COS：关", "ok" if cos_on else "idle")

        server = resolve_license_server(cfg)
        if not server:
            badge(self.badge_license, "授权：未配置（无法启动）", "bad")
        else:
            cache = licensemod.load_cache()
            expire_at = int(cache.get("expire_at") or 0)
            if cache.get("token") and expire_at > time.time():
                day = time.strftime("%Y-%m-%d", time.localtime(expire_at))
                badge(self.badge_license, f"授权：至 {day}", "ok")
            else:
                badge(self.badge_license, "授权：待激活", "warn")

        missing = []
        if not key_ok:
            missing.append("RH_API_KEY")
        if not (cfg.get("MANIFEST") or "").strip():
            missing.append("MANIFEST")
        if missing:
            self.banner.setText(
                f"⚠ 还差 {'、'.join(missing)}：点右上角「配置文件」填写，保存后点「重载配置」，不用重启。"
            )
            self.banner.show()
        else:
            self.banner.hide()

    # ---- 清单 ----
    def _load_manifest_async(self):
        source = (self.cfg.get("MANIFEST") or "").strip()
        if not source:
            self.manifest_label.setText("模板清单：未配置（打开“配置文件”填 MANIFEST）")
            return
        self.manifest_label.setText("模板清单：加载中…")
        self.manifest_worker = ManifestWorker(source)
        self.manifest_worker.loaded.connect(self._manifest_ready)
        self.manifest_worker.failed.connect(self._manifest_failed)
        self.manifest_worker.start()

    def _manifest_ready(self, templates: list):
        self.templates = templates
        self.small_panel.apply_templates(templates)
        self.big_panel.apply_templates(templates)
        self.manifest_label.setText(f"模板清单：{len(templates)} 个")
        self._refresh_status()

    def _manifest_failed(self, msg: str):
        self.templates = []
        self.manifest_label.setText("模板清单：加载失败")
        self._log(f"模板清单加载失败：{msg}")
        self._refresh_status()

    # ---- 状态 ----
    def set_busy(self, busy: bool):
        self.busy = busy
        other = self.big_panel if self.tabs.currentWidget() is self.small_panel else self.small_panel
        other.start_btn.setEnabled(not busy)
        other.image_edit.setEnabled(not busy)

    def write_log(self, line: str):
        self._log(line)

    def _log(self, line: str):
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def _reload_config(self, silent: bool = False):
        """改完 配置.ini 点这里：不用重启就生效。"""
        self.cfg = load_config()
        for panel in (self.small_panel, self.big_panel):
            panel.cfg = self.cfg
        self._load_manifest_async()
        self._refresh_status()

        missing = [name for name in ("RH_API_KEY", "MANIFEST") if not (self.cfg.get(name) or "").strip()]
        hint = f"\n\n还缺：{'、'.join(missing)}（在 配置.ini 里填）" if missing else ""
        if not silent:
            QMessageBox.information(self, "配置已重载", f"已重新读取 配置.ini{hint}")

    def _open_config_file(self):
        """在程序里直接编辑 配置.ini。

        不再依赖系统文件关联：远程桌面、没有 .ini 关联程序、压缩包内运行等场景
        都会让"用默认程序打开"静默失败，用户看到的就是"点了没反应"。
        """
        try:
            dialog = ConfigDialog(self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._reload_config(silent=True)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要让人看见
            QMessageBox.critical(self, "打不开配置窗口", f"{type(exc).__name__}: {exc}")


STYLE = """
QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #1d1d1f; }
#header { background: #f5f5f7; border-bottom: 1px solid #e3e3e6; }
#title { font-size: 15px; font-weight: 600; }
#manifest { color: #6b6b70; }
#badge { color: #6b6b70; font-size: 12px; }
#badge[tone="ok"] { color: #0a7d32; font-weight: 600; }
#badge[tone="bad"] { color: #b3261e; font-weight: 600; }
#badge[tone="warn"] { color: #a86400; font-weight: 600; }
#badge[tone="idle"] { color: #86868b; }
#banner { background: #fff8e6; border-bottom: 1px solid #f0dfae; color: #8a5a00; padding: 8px 18px; font-size: 12px; }
#footer { color: #86868b; padding: 6px 18px; border-top: 1px solid #ececef; }
QPushButton { border: 1px solid #d6d6da; border-radius: 6px; padding: 6px 14px; background: #ffffff; }
QPushButton:hover { background: #f2f2f5; }
QPushButton:disabled { color: #b0b0b5; background: #f7f7f9; }
QPushButton#primary { background: #1f6feb; border-color: #1f6feb; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #1a5fcc; }
QPushButton#primary:disabled { background: #a9c4f5; border-color: #a9c4f5; }
QPushButton#ghost { background: transparent; border: 1px solid #d6d6da; }
QLineEdit, QPlainTextEdit, QComboBox { border: 1px solid #d6d6da; border-radius: 6px; padding: 6px 8px; background: white; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #1f6feb; }
QProgressBar { border: 1px solid #e0e0e4; border-radius: 5px; height: 18px; text-align: center; background: #f4f4f7; }
QProgressBar::chunk { background: #1f6feb; border-radius: 4px; }
QTableWidget { border: 1px solid #e6e6ea; border-radius: 6px; gridline-color: #f0f0f3; }
QHeaderView::section { background: #fafafc; border: none; border-bottom: 1px solid #e6e6ea; padding: 6px; color: #6b6b70; }
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 8px 18px; border: none; color: #6b6b70; }
QTabBar::tab:selected { color: #1f6feb; border-bottom: 2px solid #1f6feb; }
"""


def main() -> int:
    if "--selftest" in sys.argv:
        # 打包后用来确认 exe 能正常加载依赖
        print(f"{APP_TITLE} v{APP_VERSION} selftest ok")
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
    window = MainWindow()

    # 默认强制授权：配置里留空就用内置地址；两者都空则拒绝启动（不再"留空=放行"）
    license_server = resolve_license_server(window.cfg)
    if not license_server:
        if not getattr(sys, "frozen", False) and os.environ.get("RH_DEV_NO_LICENSE") == "1":
            print("RH_DEV_NO_LICENSE=1：源码运行跳过授权（打包版不会生效）", file=sys.stderr)
        else:
            QMessageBox.critical(
                None, "未配置授权服务",
                "本程序必须通过密码授权才能使用，但当前没有配置授权服务器。\n\n"
                "请打开「配置文件」填写 LICENSE_SERVER，"
                "或向管理员索取已内置授权地址的版本。",
            )
            return 2
    elif not ensure_license(license_server):
        return 2

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
