#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动门禁：配了授权服务器就必须先拿到密码。

单独成文件，是为了让 `main.py` 只需要多一行调用，重构时不容易冲突。
"""

from __future__ import annotations

from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

from app.license import activate, status

MAX_ATTEMPTS = 3


def ensure_license(server: str) -> bool:
    """返回 True 表示可以进入主界面。server 为空 = 未启用授权。"""
    server = (server or "").strip()
    if not server:
        return True

    ok, message = status(server)
    if ok:
        return True

    for attempt in range(1, MAX_ATTEMPTS + 1):
        password, accepted = QInputDialog.getText(
            None,
            "需要授权",
            f"{message}\n\n请输入分配的授权密码（第 {attempt}/{MAX_ATTEMPTS} 次）：",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return False
        try:
            activate(server, password)
        except (ValueError, ConnectionError) as exc:
            QMessageBox.warning(None, "激活失败", f"{exc}")
            continue
        QMessageBox.information(None, "授权成功", "已激活，可以开始使用。")
        return True

    QMessageBox.critical(None, "无法启动", "连续 3 次激活失败，程序退出。请联系管理员获取授权密码。")
    return False
