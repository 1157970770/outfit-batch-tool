#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""授权服务（单文件，零依赖）。

两种跑法：
  1) 本地 / 自有服务器：  python3 server/license_api.py --serve 8099
  2) 腾讯云函数 SCF：     入口填 main_handler，走 API 网关触发

接口（POST，JSON）：
  /activate  {password, machine}  → {ok, token, expire_at, note}
  /verify    {token, machine}     → {ok, expire_at, note}

设计取舍（刻意保持"最简单"）：
  · 服务端**不存状态**，token 是 HMAC 签名，天然适合无状态的云函数；
  · 密码表就是一份 JSON，改完重新部署即可，不需要数据库；
  · 换机器 = machine 不匹配 → 验签失败，等于绑定机器；
  · 想吊销：把 revoked 改成 true 并重新部署，最多 TTL_DAYS 天后旧 token 全部失效。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

SECRET = os.environ.get("LICENSE_SECRET", "")
TTL_DAYS = int(os.environ.get("LICENSE_TTL_DAYS", "14"))
PASSWORDS_PATH = Path(os.environ.get("LICENSE_PASSWORDS", "") or Path(__file__).with_name("passwords.json"))


class LicenseError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


def issue_token(password: str, machine: str) -> dict:
    exp = int(time.time()) + TTL_DAYS * 86400
    payload = {"p": password, "m": machine, "e": exp}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    token = f"{_b64e(raw)}.{_sign(raw)}"
    return {"token": token, "expire_at": exp, "days": TTL_DAYS}


def verify_token(token: str, machine: str) -> dict:
    try:
        body, sig = token.split(".", 1)
        raw = _b64d(body)
    except (ValueError, TypeError):
        raise LicenseError("token 格式不对") from None
    if not hmac.compare_digest(sig, _sign(raw)):
        raise LicenseError("token 签名不对（可能是别的服务器签的）")
    data = json.loads(raw)
    if int(data.get("e", 0)) < time.time():
        raise LicenseError("token 已过期，请重新输入密码")
    if data.get("m") != machine:
        raise LicenseError("token 与这台机器不匹配")
    entry = load_passwords().get(str(data.get("p")))
    if not entry:
        raise LicenseError("密码已被删除")
    if entry.get("revoked"):
        raise LicenseError("密码已被停用")
    return {"expire_at": data.get("e"), "note": entry.get("note", "")}


# ---------------------------------------------------------------------------
# 密码表
# ---------------------------------------------------------------------------

def load_passwords() -> dict:
    if not PASSWORDS_PATH.exists():
        return {}
    try:
        data = json.loads(PASSWORDS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise LicenseError(f"密码表不是合法 JSON：{exc}", status=500) from None
    return data.get("passwords", data)


def check_password(password: str, machine: str) -> dict:
    entry = load_passwords().get(password)
    if not entry:
        raise LicenseError("密码不存在", status=401)
    if entry.get("revoked"):
        raise LicenseError("密码已被停用")
    expires = entry.get("expires")
    if expires:
        # expires 支持 2026-12-31 或 2026-12-31 23:59
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                deadline = time.mktime(time.strptime(str(expires), fmt))
                break
            except ValueError:
                continue
        else:
            raise LicenseError(f"密码表里的 expires 格式看不懂：{expires}", status=500)
        if deadline < time.time():
            raise LicenseError("密码已到期")
    bound = entry.get("machine")
    if bound and bound != machine:
        raise LicenseError("该密码已在另一台机器上激活")
    return entry


# ---------------------------------------------------------------------------
# 请求处理（HTTP 风格，两种入口共用）
# ---------------------------------------------------------------------------

def handle(path: str, body: dict) -> tuple[int, dict]:
    try:
        if not SECRET:
            raise LicenseError("服务端没配 LICENSE_SECRET", status=500)

        action = path.rstrip("/").split("/")[-1]
        if action == "health":
            return 200, {"ok": True, "service": "outfit-license", "ttl_days": TTL_DAYS}

        machine = str(body.get("machine") or "").strip()
        if not machine:
            raise LicenseError("缺少 machine", status=400)

        if action == "activate":
            password = str(body.get("password") or "").strip()
            if not password:
                raise LicenseError("缺少密码", status=400)
            entry = check_password(password, machine)
            result = issue_token(password, machine)
            return 200, {"ok": True, **result, "note": entry.get("note", "")}

        if action == "verify":
            token = str(body.get("token") or "").strip()
            if not token:
                raise LicenseError("缺少 token", status=400)
            info = verify_token(token, machine)
            return 200, {"ok": True, **info}

        raise LicenseError("未知接口", status=404)
    except LicenseError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 入口 1：腾讯云函数 SCF（API 网关触发）
# ---------------------------------------------------------------------------

def main_handler(event, context):  # noqa: N802, ANN001
    path = event.get("path") or (event.get("queryString") or "")
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": json.dumps({"ok": False, "error": "body 不是 JSON"})}
    status, payload = handle(path, body or {})
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# 入口 2：本地 / 自有服务器
# ---------------------------------------------------------------------------

def serve(port: int) -> None:
    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静一点
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def do_GET(self):  # 健康检查
            status, payload = handle(self.path, {})
            self._reply(status, payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
            try:
                body = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return self._reply(400, {"ok": False, "error": "body 不是 JSON"})
            status, payload = handle(self.path, body)
            self._reply(status, payload)

        def _reply(self, status: int, payload: dict):
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as httpd:
        print(f"授权服务已启动：http://127.0.0.1:{port}  （密码表：{PASSWORDS_PATH}）")
        httpd.serve_forever()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        idx = sys.argv.index("--serve")
        serve(int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 8099)
    else:
        print(__doc__)
