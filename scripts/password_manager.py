# -*- coding: utf-8 -*-
"""密码管理器（图形界面）—— 管理 server/passwords.json，并可一键同步到云函数。

这个界面替代下面这些命令：
    python scripts/license_passwords.py list
    python scripts/license_passwords.py add --note "张三 / 设计部"
    python scripts/license_passwords.py expire CODE 2028-12-31
    python scripts/license_passwords.py revoke / unrevoke / remove CODE
    python scripts/license_deploy.py deploy

启动：双击 scripts/密码管理器.bat，或 python scripts/密码管理器.py
注意：这是**内部管理工具**，不要跟着软件一起发给客户。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from PySide6.QtCore import QThread, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QGuiApplication
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
        QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
        QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    )
except ImportError as exc:  # 双击 .py、但那个解释器没装 PySide6 时，说人话再退
    print("启动失败：这个 Python 里没有 PySide6。")
    print(f"  实际解释器：{sys.executable}")
    print("  解决：pip install PySide6")
    print("  更省事：双击 scripts\\密码管理器.bat（它会自动挑带 PySide6 的 Python）")
    print(f"\n原始错误：{exc}")
    input("\n按回车关闭…")
    raise SystemExit(1) from None

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import license_passwords as LP  # noqa: E402

DEPLOY_SCRIPT = Path(LP.DEPLOY_SCRIPT)   # 指向项目里的真脚本（打包后也能找到）
DATA = Path(LP.DEFAULT_FILE)             # 密码表真身，跟随项目根

COLUMNS = ["密码", "状态", "到期时间", "剩余", "备注"]
GREEN, AMBER, RED, GRAY = QColor("#1D9E75"), QColor("#BA7517"), QColor("#A32D2D"), QColor("#5F5E5A")


def human_left(expires) -> tuple[str, int | None]:
    """返回 (剩余描述, 剩余天数)。"""
    dl = LP._deadline(expires)
    if dl is None:
        return "永久", None
    left = (dl - time.time()) / 86400
    if left < 0:
        return "已过期", int(left)
    if left < 1:
        return "今天到期", 0
    return f"{int(left)} 天", int(left)


class EditDialog(QDialog):
    """新增 / 续期共用：备注 + 有效天数 + 永久。"""

    def __init__(self, parent, title: str, note: str = "", days: int = LP.DEFAULT_DAYS,
                 forever: bool = False, show_note: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        form = QFormLayout(self)

        self.note = QLineEdit(note)
        if show_note:
            form.addRow("备注", self.note)

        self.days = QSpinBox()
        self.days.setRange(1, 3650)
        self.days.setValue(days)
        self.days.setSuffix(" 天")
        form.addRow("有效期", self.days)

        self.forever = QCheckBox("永久有效（不限制到期）")
        self.forever.setChecked(forever)
        form.addRow("", self.forever)

        self.preview = QLabel()
        self.preview.setStyleSheet("color:#5F5E5A")
        form.addRow("", self.preview)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("确定")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

        self.days.valueChanged.connect(self._refresh)
        self.forever.toggled.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        on = self.forever.isChecked()
        self.days.setEnabled(not on)
        self.preview.setText("到期时间：不限（永久）" if on
                             else f"到期时间：{LP.default_expires(self.days.value())}")

    def values(self) -> dict:
        return {
            "note": self.note.text().strip(),
            "expires": "" if self.forever.isChecked() else LP.default_expires(self.days.value()),
        }


class SyncWorker(QThread):
    """后台执行 deploy（+ 可选 check），避免界面卡住。"""

    done = Signal(bool, str)

    def __init__(self, code: str | None, expect: str):
        super().__init__()
        self.code, self.expect = code, expect

    def run(self):
        try:
            py = LP.python_exe()          # 打包后 sys.executable 是 exe 自己，不能用
            r = subprocess.run([py, str(DEPLOY_SCRIPT), "deploy"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            out = (r.stdout or "").strip().splitlines()
            if r.returncode != 0:
                tail = "\n".join((out + (r.stderr or "").strip().splitlines())[-6:])
                self.done.emit(False, f"同步失败：\n{tail}")
                return
            if not self.code:
                self.done.emit(True, "已同步到云函数")
                return
            c = subprocess.run([py, str(DEPLOY_SCRIPT), "check",
                                self.code, "--expect", self.expect],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            line = (c.stdout or "").strip().splitlines()
            self.done.emit(c.returncode == 0,
                           "已同步并验证通过" + (f"：{line[-1].strip()}" if line else ""))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, f"同步异常：{type(exc).__name__}: {exc}")


class Manager(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("密码管理器 · 换装批量生产器")
        self.resize(880, 520)
        self.doc: dict = {}
        self.worker: SyncWorker | None = None

        root = QVBoxLayout(self)

        self.stat = QLabel()
        self.stat.setStyleSheet("color:#444441")
        root.addWidget(self.stat)

        bar = QHBoxLayout()
        self.btn_add = QPushButton("新增密码")
        self.btn_renew = QPushButton("续期 / 改到期")
        self.btn_toggle = QPushButton("停用 / 恢复")
        self.btn_del = QPushButton("删除")
        self.btn_copy = QPushButton("复制密码")
        self.btn_sync = QPushButton("立即同步到云")
        for b in (self.btn_add, self.btn_renew, self.btn_toggle, self.btn_del,
                  self.btn_copy, self.btn_sync):
            bar.addWidget(b)
        bar.addStretch(1)
        self.auto = QCheckBox("改完自动同步到云")
        self.auto.setChecked(True)
        bar.addWidget(self.auto)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        h = self.table.horizontalHeader()
        for i, w in enumerate((220, 90, 170, 90, 260)):
            self.table.setColumnWidth(i, w)
        h.setStretchLastSection(True)
        root.addWidget(self.table, 1)

        self.status = QLabel("就绪")
        self.status.setStyleSheet("color:#5F5E5A")
        root.addWidget(self.status)

        self.btn_add.clicked.connect(self.on_add)
        self.btn_renew.clicked.connect(self.on_renew)
        self.btn_toggle.clicked.connect(self.on_toggle)
        self.btn_del.clicked.connect(self.on_delete)
        self.btn_copy.clicked.connect(self.on_copy)
        self.btn_sync.clicked.connect(lambda: self.sync(None, "ok"))
        self.table.itemDoubleClicked.connect(lambda *_: self.on_renew())

        self.reload()

    # ---------------------------------------------------------------- 数据
    def reload(self):
        self.doc = LP.load(str(DATA))
        self.render()

    def render(self):
        rows = self.doc.get("passwords", {})
        ordered = sorted(rows.items(),
                         key=lambda kv: (bool(kv[1].get("revoked")),
                                         LP._deadline(kv[1].get("expires")) or 9e12))
        self.table.setRowCount(len(ordered))
        usable = expired = revoked = soon = 0
        for r, (code, info) in enumerate(ordered):
            left_text, left_days = human_left(info.get("expires"))
            if info.get("revoked"):
                state, color = "已停用", GRAY
                revoked += 1
            elif left_days is not None and left_days < 0:
                state, color = "已到期", RED
                expired += 1
            elif left_days is not None and left_days <= 7:
                state, color = "即将到期", AMBER
                soon += 1
                usable += 1
            else:
                state, color = "可用", GREEN
                usable += 1
            cells = [code, state, info.get("expires") or "永久", left_text, info.get("note") or ""]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if c == 0:
                    item.setData(Qt.UserRole, code)
                if c == 1:
                    item.setForeground(color)
                self.table.setItem(r, c, item)
        self.stat.setText(
            f"共 {len(ordered)} 条   ·   可用 {usable}（其中 7 天内到期 {soon}）"
            f"   ·   已到期 {expired}   ·   已停用 {revoked}"
            f"    密码表：{DATA}")

    def current_code(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _pick(self, title="提示") -> str | None:
        code = self.current_code()
        if not code:
            QMessageBox.information(self, title, "请先在表格里选中一行。")
        return code

    def _save_and_sync(self, code: str | None, expect: str, msg: str):
        LP.save(str(DATA), self.doc)
        self.reload()
        self.status.setText(msg)
        if self.auto.isChecked():
            self.sync(code, expect)

    # ---------------------------------------------------------------- 操作
    def on_add(self):
        dlg = EditDialog(self, "新增密码", days=LP.DEFAULT_DAYS)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        code = LP.next_code(self.doc)
        self.doc["passwords"][code] = {
            "note": v["note"], "machine": "", "expires": v["expires"], "revoked": False,
        }
        QGuiApplication.clipboard().setText(code)
        self._save_and_sync(code, "ok",
                            f"已新增 {code}（已复制到剪贴板） 到期：{v['expires'] or '永久'}")

    def on_renew(self):
        code = self._pick("续期")
        if not code:
            return
        info = self.doc["passwords"][code]
        dlg = EditDialog(self, f"续期 / 改到期 · {code}", note=info.get("note") or "",
                         forever=not info.get("expires"), show_note=True)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        info["note"] = v["note"]
        info["expires"] = v["expires"]
        expect = "reject" if (v["expires"] and (LP._deadline(v["expires"]) or 0) < time.time()) else "ok"
        self._save_and_sync(code, expect,
                            f"已更新 {code} 到期时间：{v['expires'] or '永久'}")

    def on_toggle(self):
        code = self._pick("停用 / 恢复")
        if not code:
            return
        info = self.doc["passwords"][code]
        info["revoked"] = not info.get("revoked")
        action = "已停用" if info["revoked"] else "已恢复"
        self._save_and_sync(code, "reject" if info["revoked"] else "ok", f"{action} {code}")

    def on_delete(self):
        code = self._pick("删除")
        if not code:
            return
        r = QMessageBox.question(
            self, "确认删除",
            f"删除密码 {code}？\n\n该用户下次启动就会被拒，且密码无法找回。\n"
            f"（如果只是想临时停用，请用「停用 / 恢复」）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        del self.doc["passwords"][code]
        self._save_and_sync(code, "reject", f"已删除 {code}")

    def on_copy(self):
        code = self._pick("复制密码")
        if not code:
            return
        QGuiApplication.clipboard().setText(code)
        self.status.setText(f"已复制：{code}")

    # ---------------------------------------------------------------- 同步
    def sync(self, code: str | None, expect: str):
        if self.worker and self.worker.isRunning():
            self.status.setText("上一次同步还没结束，请稍等…")
            return
        self._busy(True, "正在同步到云函数（约 5~15 秒）…")
        self.worker = SyncWorker(code, expect)
        self.worker.done.connect(self.on_sync_done)
        self.worker.start()

    def on_sync_done(self, ok: bool, message: str):
        self._busy(False, message)
        if not ok:
            QMessageBox.warning(self, "同步没成功", message + "\n\n本地表已保存，网络恢复后点「立即同步到云」即可。")

    def _busy(self, busy: bool, message: str):
        for b in (self.btn_add, self.btn_renew, self.btn_toggle, self.btn_del,
                  self.btn_copy, self.btn_sync):
            b.setEnabled(not busy)
        self.status.setText(message)


LOG_PATH = Path(os.getenv("TEMP") or os.getenv("TMP") or ".") / "密码管理器_error.log"


def _install_crash_log() -> None:
    """未捕获异常写到 %TEMP%\\密码管理器_error.log —— 闪退时靠它查原因。"""

    def hook(t, v, tb):  # noqa: ANN001
        try:
            import traceback
            LOG_PATH.write_text("".join(traceback.format_exception(t, v, tb)), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        sys.__excepthook__(t, v, tb)

    sys.excepthook = hook


def main() -> int:
    _install_crash_log()
    try:
        app = QApplication(sys.argv)
    except Exception:  # noqa: BLE001
        import traceback
        LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        raise
    app.setApplicationName("密码管理器")
    try:
        w = Manager()
    except Exception:  # noqa: BLE001
        import traceback
        LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        raise
    w.show()
    if "--smoke" in sys.argv:
        QTimer.singleShot(1500, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
