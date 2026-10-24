#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""授权链路测试：本地起服务，跑 激活/校验/换机器/停用/过期 五个场景。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lic-"))

# 必须在 import app.license 之前改，它按 APPDATA 决定缓存位置
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["LICENSE_SECRET"] = "test-secret-请换成随机串"
os.environ["LICENSE_PASSWORDS"] = str(TMP / "passwords.json")
os.environ["LICENSE_TTL_DAYS"] = "14"

sys.path.insert(0, str(ROOT))
from app.license import activate, clear_cache, load_cache, machine_id, status  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_passwords(data: dict) -> None:
    Path(os.environ["LICENSE_PASSWORDS"]).write_text(json.dumps({"passwords": data}, ensure_ascii=False), encoding="utf-8")


def wait_port(port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    write_passwords({
        "RH-OK": {"note": "正常用户", "expires": "2099-12-31", "revoked": False},
        "RH-OLD": {"note": "已到期", "expires": "2020-01-01", "revoked": False},
        "RH-BAN": {"note": "已停用", "expires": "2099-12-31", "revoked": True},
    })

    port = free_port()
    server = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, str(ROOT / "server" / "license_api.py"), "--serve", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ})
    if not wait_port(port):
        print("授权服务没起来")
        return 1

    results: list[tuple[str, bool]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond))
        print(f"{'✓' if cond else '✗'} {name}{(' — ' + detail) if detail else ''}")

    clear_cache()

    ok, msg = status(server)
    check("未激活时拒绝", not ok, msg)

    try:
        activate(server, "RH-WRONG")
        check("错误密码被拒", False, "居然通过了")
    except ValueError as exc:
        check("错误密码被拒", True, str(exc))

    try:
        activate(server, "RH-BAN")
        check("停用密码被拒", False)
    except ValueError as exc:
        check("停用密码被拒", True, str(exc))

    try:
        activate(server, "RH-OLD")
        check("到期密码被拒", False)
    except ValueError as exc:
        check("到期密码被拒", True, str(exc))

    activate(server, "RH-OK")
    ok, msg = status(server)
    check("正确密码激活后可用", ok, msg)
    check("token 已存本地", bool(load_cache().get("token")))
    check("缓存里不含明文密码", "password" not in json.dumps(load_cache()).lower())

    # 换机器：伪造一个别的 machine 去验签，必须失败
    import urllib.error
    import urllib.request

    body = json.dumps({"token": load_cache()["token"], "machine": "someone-else"}).encode()
    req = urllib.request.Request(f"{server}/verify", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            other = json.loads(resp.read())
    except urllib.error.HTTPError as exc:      # 403 是预期路径，body 里才有原因
        other = json.loads(exc.read().decode("utf-8", "replace"))
    check("token 绑定机器（换机器失败）", not other.get("ok"), other.get("error", ""))

    # 停用后立刻失效
    # 网关只挂了 /license/activate 时，客户端应自动补后缀重试
    import http.server
    import threading

    seen: list[str] = []

    class Gateway(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(self.path)
            if not self.path.endswith(("/activate", "/verify")):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw = json.dumps({"ok": True, "token": "t" * 24,
                              "expire_at": int(time.time()) + 3600}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    gw = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    clear_cache()
    try:
        activate(f"http://127.0.0.1:{gw.server_address[1]}/license", "RH-OK")
        check("网关只挂子路由时自动重试",
              len(seen) == 2 and seen[0].endswith("/license") and seen[1].endswith("/license/activate"),
              f"请求序列 {seen}")
    finally:
        gw.shutdown()

    # 换回真服务重新激活，供后面的停用测试用
    clear_cache()
    activate(server, "RH-OK")

    # 停用后立刻失效
    write_passwords({"RH-OK": {"note": "正常用户", "expires": "2099-12-31", "revoked": True}})
    # 服务端每次请求都重读密码表，不需要重启
    ok, msg = status(server)
    check("停用后立即拒绝", not ok, msg)
    check("停用后缓存被清空", not load_cache().get("token"))

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    failed = [name for name, cond in results if not cond]
    print()
    print("全部通过" if not failed else f"失败 {len(failed)} 项：{failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
