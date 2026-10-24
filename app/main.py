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

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from app.core import (  # noqa: E402
    DEFAULT_PROMPT, GROUP_SIZE, TOTAL_TEMPLATES, BatchRunner, BatchSpec,
    EngineError, check_account, load_manifest, plan_batch, unique_batch_dir,
)
from app.storage import StorageConfig, StorageError, open_storage  # noqa: E402

APP_TITLE = "换装批量生产器 · 测试版"
APP_VERSION = "0.1"
EMBEDDED_KEY = os.environ.get("RUNNINGHUB_API_KEY", "")  # 分发给别人时把 Key 填在这里
CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home()) / "RHOutfitTool"
CONFIG_PATH = CONFIG_DIR / "config.json"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config() -> dict:
    defaults = {
        "output_root": str(Path.home() / "换装输出"),
        "manifest": "",
        "api_key": "",
        "concurrency": 3,
        # 对象存储（腾讯云 COS）：默认关闭，填好并勾选后成品自动归档
        "cos_enabled": False,
        "cos_bucket": "",
        "cos_region": "ap-guangzhou",
        "cos_secret_id": "",
        "cos_secret_key": "",
        "cos_prefix": "",
        "cos_domain": "",
    }
    if CONFIG_PATH.exists():
        try:
            defaults.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return defaults


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def build_storage(cfg: dict):
    """按界面配置造存储实例；未启用返回 None。配置不合法时抛 StorageError。"""
    if not cfg.get("cos_enabled"):
        return None
    return open_storage(StorageConfig(
        provider="cos",
        bucket=(cfg.get("cos_bucket") or "").strip(),
        region=(cfg.get("cos_region") or "").strip(),
        secret_id=(cfg.get("cos_secret_id") or "").strip(),
        secret_key=(cfg.get("cos_secret_key") or "").strip(),
        prefix=(cfg.get("cos_prefix") or "").strip(),
        domain=(cfg.get("cos_domain") or "").strip(),
    ))


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

class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self.cfg = dict(cfg)

        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.out_edit = QLineEdit(self.cfg.get("output_root", ""))
        out_btn = QPushButton("浏览…")
        out_btn.clicked.connect(self._pick_out)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(out_btn)
        form.addRow("输出根目录", out_row)

        self.manifest_edit = QLineEdit(self.cfg.get("manifest", ""))
        self.manifest_edit.setPlaceholderText("https://cdn.xxx.com/manifest.json 或本地 json 路径")
        man_btn = QPushButton("浏览…")
        man_btn.clicked.connect(self._pick_manifest)
        man_row = QHBoxLayout()
        man_row.addWidget(self.manifest_edit, 1)
        man_row.addWidget(man_btn)
        form.addRow("模板清单", man_row)

        self.key_edit = QLineEdit(self.cfg.get("api_key", "") or EMBEDDED_KEY)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("32 位 RunningHub Key（内置后这里可留空）")
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        self.key_btn = QPushButton("验证并查余额")
        self.key_btn.clicked.connect(self._check_key)
        key_row.addWidget(self.key_btn)
        form.addRow("API Key", key_row)

        self.key_label = QLabel("未验证")
        self.key_label.setStyleSheet("color:#777;")
        form.addRow("", self.key_label)

        # --- 对象存储（腾讯云 COS） ---
        self.cos_enabled = QCheckBox("启用 COS 归档：成品自动上传，本机磁盘和 24 小时链接都不再是唯一副本")
        self.cos_enabled.setChecked(bool(self.cfg.get("cos_enabled")))
        form.addRow("", self.cos_enabled)

        self.cos_bucket = QLineEdit(self.cfg.get("cos_bucket", ""))
        self.cos_bucket.setPlaceholderText("my-bucket-1250000000（必须带 APPID 后缀）")
        form.addRow("存储桶", self.cos_bucket)

        self.cos_region = QLineEdit(self.cfg.get("cos_region", "ap-guangzhou"))
        self.cos_region.setPlaceholderText("ap-guangzhou / ap-shanghai / ap-beijing")
        form.addRow("地域", self.cos_region)

        self.cos_id = QLineEdit(self.cfg.get("cos_secret_id", ""))
        self.cos_id.setPlaceholderText("子账号 SecretId（不要用主账号密钥）")
        form.addRow("SecretId", self.cos_id)

        self.cos_key = QLineEdit(self.cfg.get("cos_secret_key", ""))
        self.cos_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.cos_key.setPlaceholderText("子账号 SecretKey，只存在本机 config.json")
        form.addRow("SecretKey", self.cos_key)

        self.cos_prefix = QLineEdit(self.cfg.get("cos_prefix", ""))
        self.cos_prefix.setPlaceholderText("可选：对象前缀，例如 prod / test")
        form.addRow("对象前缀", self.cos_prefix)

        self.cos_domain = QLineEdit(self.cfg.get("cos_domain", ""))
        self.cos_domain.setPlaceholderText("可选：CDN 域名；留空用 COS 源站签名链接")
        form.addRow("CDN 域名", self.cos_domain)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_cos)
        self.test_label = QLabel("")
        self.test_label.setStyleSheet("color:#777;")
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_label, 1)
        form.addRow("", test_row)

        hint = QLabel("提示：模板清单建议放在对象存储 + CDN 上，链接需免登录可直连。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777;")
        form.addRow("", hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _pick_out(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出根目录", self.out_edit.text() or str(Path.home()))
        if path:
            self.out_edit.setText(path)

    def _pick_manifest(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择模板清单", "", "JSON (*.json);;所有文件 (*)")
        if path:
            self.manifest_edit.setText(path)

    # ---- 对象存储 ----
    def _cos_config(self) -> StorageConfig:
        return StorageConfig(
            provider="cos",
            bucket=self.cos_bucket.text().strip(),
            region=self.cos_region.text().strip(),
            secret_id=self.cos_id.text().strip(),
            secret_key=self.cos_key.text().strip(),
            prefix=self.cos_prefix.text().strip(),
            domain=self.cos_domain.text().strip(),
        )

    def _test_cos(self):
        """真写一个几十字节的探针对象，跑通 写→读→删 才算配置正确。"""
        self.test_btn.setEnabled(False)
        self.test_label.setStyleSheet("color:#777;")
        self.test_label.setText("连接中…")
        QApplication.processEvents()
        probe = "_healthcheck/ping.txt"
        try:
            storage = open_storage(self._cos_config())
            if storage is None:
                raise StorageError("未启用 COS")
            storage.put_bytes(b"ping", probe, content_type="text/plain")
            readable = storage.exists(probe)
            storage.delete(probe)
            self.test_label.setStyleSheet("color:#0a7d32;")
            self.test_label.setText("✓ 读写正常" if readable else "⚠ 写入成功但读不到")
        except Exception as exc:  # noqa: BLE001 - 界面只负责显示原因
            self.test_label.setStyleSheet("color:#b3261e;")
            self.test_label.setText(f"✗ {exc}"[:220])
        finally:
            self.test_btn.setEnabled(True)

    def _check_key(self):
        """校验 RunningHub Key 并显示余额——填错的 Key 一按就知道。"""
        key = self.key_edit.text().strip() or EMBEDDED_KEY
        if not key:
            self.key_label.setStyleSheet("color:#b3261e;")
            self.key_label.setText("请先填 Key")
            return
        self.key_btn.setEnabled(False)
        self.key_label.setStyleSheet("color:#777;")
        self.key_label.setText("验证中…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            info = check_account(key)
        finally:
            QApplication.restoreOverrideCursor()
            self.key_btn.setEnabled(True)
        if info.get("ok"):
            self.key_label.setStyleSheet("color:#0a7d32;")
            self.key_label.setText(
                f"✓ 有效 · 余额 ¥{info.get('balance')} · RH币 {info.get('coins')}"
                f" · 类型 {info.get('api_type') or '-'} · 运行中 {info.get('running')}"
            )
        else:
            self.key_label.setStyleSheet("color:#b3261e;")
            self.key_label.setText(f"✗ {info.get('message')}")

    def _save(self):
        self.cfg["output_root"] = self.out_edit.text().strip()
        self.cfg["manifest"] = self.manifest_edit.text().strip()
        self.cfg["api_key"] = self.key_edit.text().strip()
        self.cfg["cos_enabled"] = self.cos_enabled.isChecked()
        self.cfg["cos_bucket"] = self.cos_bucket.text().strip()
        self.cfg["cos_region"] = self.cos_region.text().strip()
        self.cfg["cos_secret_id"] = self.cos_id.text().strip()
        self.cfg["cos_secret_key"] = self.cos_key.text().strip()
        self.cfg["cos_prefix"] = self.cos_prefix.text().strip()
        self.cfg["cos_domain"] = self.cos_domain.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# 批次面板（小批量 / 大批量共用）
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

    def _open_folder(self):
        target = self._out_dir()
        if target and target.exists():
            QDesktopServices.openUrl(target.as_uri())

    def _out_dir(self) -> Path | None:
        if not self.id_edit.text().strip():
            return None
        root = Path(self.cfg.get("output_root") or (Path.home() / "换装输出"))
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
            QMessageBox.warning(self, "模板未加载", "模板清单还没有加载成功，去“设置”里检查地址。")
            return

        key = (self.cfg.get("api_key") or EMBEDDED_KEY).strip()
        if not key:
            QMessageBox.warning(self, "缺少 API Key", "去“设置”里填入 RunningHub Key，或直接内置到程序里。")
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

        root = Path(self.cfg.get("output_root") or (Path.home() / "换装输出"))
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
# 主窗口
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.busy = False
        self.templates: list[dict] = []
        self.manifest_worker: ManifestWorker | None = None
        self._log_path = Path(self.cfg["output_root"]) / "logs" / f"app-{time.strftime('%Y%m%d')}.log"

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
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(18, 12, 18, 12)
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        self.manifest_label = QLabel("模板清单：加载中…")
        self.manifest_label.setObjectName("manifest")
        settings_btn = QPushButton("设置")
        settings_btn.setObjectName("ghost")
        settings_btn.clicked.connect(self._open_settings)
        head_row.addWidget(title)
        head_row.addStretch(1)
        head_row.addWidget(self.manifest_label)
        head_row.addSpacing(12)
        head_row.addWidget(settings_btn)
        layout.addWidget(header)

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

    # ---- 清单 ----
    def _load_manifest_async(self):
        source = (self.cfg.get("manifest") or "").strip()
        if not source:
            self.manifest_label.setText("模板清单：未配置（点“设置”填写）")
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

    def _manifest_failed(self, msg: str):
        self.templates = []
        self.manifest_label.setText("模板清单：加载失败")
        self._log(f"模板清单加载失败：{msg}")

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

    def _open_settings(self):
        dialog = SettingsDialog(self.cfg, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cfg.update(dialog.cfg)
            save_config(self.cfg)
            for panel in (self.small_panel, self.big_panel):
                panel.cfg = self.cfg
            self._load_manifest_async()


STYLE = """
QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #1d1d1f; }
#header { background: #f5f5f7; border-bottom: 1px solid #e3e3e6; }
#title { font-size: 15px; font-weight: 600; }
#manifest { color: #6b6b70; }
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
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
