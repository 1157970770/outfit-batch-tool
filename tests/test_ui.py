#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面集成测试：离屏启动窗口，用假服务完整跑一批（3 条），验证表格/CSV/日志。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import app.main as gui  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    port = free_port()
    mock = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_runninghub.py"), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_port(port):
        print("假服务没起来")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="rhui-"))
    app = QApplication([])

    # 测试里不需要弹窗，直接吞掉
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)

    from PIL import Image
    image = tmp / "outfit.png"
    Image.new("RGB", (720, 900), (228, 226, 222)).save(image)

    manifest = tmp / "manifest.json"
    manifest.write_text(json.dumps(
        [{"id": f"{i:03d}", "url": f"http://127.0.0.1:{port}/tpl/{i:03d}.mp4"} for i in range(1, 6)],
        ensure_ascii=False), encoding="utf-8")

    window = gui.MainWindow()
    window.cfg.update({
        "output_root": str(tmp / "out"),
        "manifest": str(manifest),
        "api_key": "demo-key-0123456789abcdef",
        "base_url": f"http://127.0.0.1:{port}/openapi/v2",
    })
    window.small_panel.cfg = window.cfg
    window.big_panel.cfg = window.cfg
    window.small_panel.apply_templates(gui.load_manifest(str(manifest)))
    window.show()

    panel = window.small_panel
    panel.image_edit.setText(str(image))
    panel.id_edit.setText("UI01")
    panel.prompt_edit.setPlainText("视频1中的人物，把服装换成图1这套")
    panel.start()

    deadline = time.time() + 60
    while window.busy and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)

    ok = True
    rows = panel.table.rowCount()
    print("表格行数:", rows)
    if rows != 3:
        ok = False
    for row in range(rows):
        status = panel.table.item(row, 1).text() if panel.table.item(row, 1) else ""
        if status != "完成":
            ok = False
            print("  异常行:", row, status)

    out_dir = tmp / "out" / "UI01"
    csv_path = out_dir / "report.csv"
    log_path = out_dir / "log.txt"
    print("batch.json:", (out_dir / "batch.json").exists())
    print("report.csv:", csv_path.exists())
    print("log.txt:", log_path.exists())
    print("mp4 数量:", len(list(out_dir.glob("*.mp4"))))
    if not (csv_path.exists() and log_path.exists() and len(list(out_dir.glob("*.mp4"))) == 3):
        ok = False

    # 重复批次号 → 自动加 -2
    second = gui.unique_batch_dir(tmp / "out", "UI01")
    print("重复批次号目录:", second.name)
    if second.name != "UI01-2":
        ok = False

    mock.terminate()
    try:
        mock.wait(timeout=5)
    except subprocess.TimeoutExpired:
        mock.kill()

    print("OK 界面集成通过" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
