#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页密码与会话签名单元检查。"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web.auth import LoginLimiter, SessionSigner, make_password, verify_password  # noqa: E402


def main() -> int:
    password = make_password("local-pass-123")
    assert verify_password("local-pass-123", password)
    assert not verify_password("wrong-password", password)
    assert make_password("local-pass-123")["salt"] != password["salt"]
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    signer = SessionSigner(secret, hours=1)
    user = {"role": "admin", "version": 3, "enabled": True}
    token = signer.issue("admin", user)
    assert signer.verify(token, {"admin": user}) == {"username": "admin", "role": "admin"}
    assert signer.verify(token + "x", {"admin": user}) is None
    assert signer.verify(token, {"admin": {**user, "version": 4}}) is None
    limiter = LoginLimiter(limit=2, window=60)
    assert limiter.allowed("ip|user")
    limiter.failed("ip|user")
    limiter.failed("ip|user")
    assert not limiter.allowed("ip|user")
    limiter.succeeded("ip|user")
    assert limiter.allowed("ip|user")
    print("OK 网页认证检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
