#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主机端网页用户管理命令；使用 getpass，不在命令行暴露密码。"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import secrets
import sys
from pathlib import Path

from app import config as appconfig
from app.web.auth import make_password


def _web_path() -> Path:
    return Path(os.getenv("OUTFIT_WEB_CONFIG") or (appconfig.app_dir() / "server" / "web.json"))


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _prompt_password() -> str:
    first = getpass.getpass("设置密码（至少 8 位）：")
    second = getpass.getpass("再输入一次：")
    if len(first) < 8:
        raise RuntimeError("密码至少需要 8 位")
    if first != second:
        raise RuntimeError("两次密码不一致")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="换装批量生产器局域网网页服务")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true", help="初始化网页服务并创建管理员")
    group.add_argument("--add-user", metavar="用户名", help="添加普通用户")
    group.add_argument("--reset-password", metavar="用户名", help="重置用户密码")
    group.add_argument("--list-users", action="store_true", help="列出用户")
    group.add_argument("--remove-user", metavar="用户名", help="停用用户")
    group.add_argument("--selftest", action="store_true", help="检查网页模块与本地图片依赖")
    parser.add_argument("--role", choices=("admin", "user"), default="user")
    parser.add_argument("--host", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, help="端口，默认 8765")
    args = parser.parse_args(argv)
    path = _web_path()
    try:
        if args.selftest:
            from app.web import WEB_VERSION
            from app.web.auth import verify_password
            from app.media import validate_image
            from PIL import Image
            import tempfile
            probe = make_password("selftest-pass")
            if not verify_password("selftest-pass", probe):
                raise RuntimeError("密码哈希自检失败")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
                probe_path = Path(temp.name)
            try:
                Image.new("RGB", (256, 256), (240, 230, 220)).save(probe_path)
                validate_image(probe_path)
            finally:
                probe_path.unlink(missing_ok=True)
            if not (Path(__file__).resolve().parent / "static" / "index.html").exists():
                raise RuntimeError("网页静态文件缺失")
            print(f"网页自检通过 v{WEB_VERSION}")
            return 0
        if args.init:
            if path.exists():
                raise RuntimeError(f"配置已存在：{path}（不会覆盖）")
            username = input("管理员用户名 [admin]：").strip() or "admin"
            if not re.fullmatch(r"[\w.-]{2,32}", username, flags=re.UNICODE):
                raise RuntimeError("账号请使用 2–32 位字母、数字、下划线、点或短横线")
            password = _prompt_password()
            web = {
                "host": "0.0.0.0", "port": 8765,
                "session_secret": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("="),
                "session_hours": 12, "max_active_jobs": 2, "max_active_per_user": 1,
                "max_job_items": 20, "confirm_over_items": 5, "max_upload_mb": 20,
                "data_root": "web-data", "retention_days": 0,
                "users": {username: {**make_password(password), "role": "admin", "enabled": True,
                                       "version": 1, "expires": ""}},
            }
            _save(path, web)
            print(f"已初始化：{path}\n管理员账号：{username}")
            print("下一步：运行 python3 run_web.py 启动服务。")
            return 0
        if not path.exists():
            raise RuntimeError("网页账号尚未初始化，请先运行：python3 run_web.py --init")
        web = json.loads(path.read_text(encoding="utf-8-sig"))
        users = web.setdefault("users", {})
        if args.list_users:
            for name, user in sorted(users.items()):
                status = "启用" if user.get("enabled", True) else "停用"
                print(f"{name}\t{user.get('role', 'user')}\t{status}")
            return 0
        if args.add_user:
            if not re.fullmatch(r"[\w.-]{2,32}", args.add_user, flags=re.UNICODE):
                raise RuntimeError("账号请使用 2–32 位字母、数字、下划线、点或短横线")
            if args.add_user in users:
                raise RuntimeError("用户名已存在")
            password = _prompt_password()
            users[args.add_user] = {**make_password(password), "role": args.role,
                                    "enabled": True, "version": 1, "expires": ""}
            _save(path, web)
            print(f"已添加用户：{args.add_user}（重启网页服务生效）")
            return 0
        if args.reset_password:
            if args.reset_password not in users:
                raise RuntimeError("用户不存在")
            password = _prompt_password()
            users[args.reset_password].update(make_password(password))
            users[args.reset_password]["version"] = int(users[args.reset_password].get("version", 1)) + 1
            _save(path, web)
            print(f"已重置：{args.reset_password}（重启网页服务生效）")
            return 0
        if args.remove_user:
            if args.remove_user not in users:
                raise RuntimeError("用户不存在")
            users[args.remove_user]["enabled"] = False
            users[args.remove_user]["version"] = int(users[args.remove_user].get("version", 1)) + 1
            _save(path, web)
            print(f"已停用：{args.remove_user}（重启网页服务生效）")
            return 0
        from app.web.server import serve
        return serve(host=args.host, port=args.port)
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
