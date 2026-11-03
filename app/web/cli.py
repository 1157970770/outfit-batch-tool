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
import time
from pathlib import Path

from app import config as appconfig
from app.web.auth import make_password
from app.web.settings import resolve_tag_store_path


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


class _PendingStore:
    """把 `load_xlsx` 的项目包装成 TemplateStore 的只读形态，用于导入前对账（不写盘）。"""

    revision = 0

    def __init__(self, project: dict, path: Path):
        self._project = project
        self.path = str(path)
        self.rows_data = [dict(row) for row in project.get("videos") or []]

    def categories(self) -> dict:
        return dict(self._project.get("tag_categories") or {})

    def rows(self, *, include_deprecated: bool = True) -> list[dict]:
        return [dict(row) for row in self.rows_data
                if include_deprecated or not row.get("deprecated")]


def _manifest_items(base: Path, data_dir: Path) -> tuple[list[dict], str]:
    api_path = Path(os.getenv("OUTFIT_CONFIG") or base / "server" / "config.json")
    try:
        remote = json.loads(api_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"配置读取失败：{exc}"
    cfg = appconfig.from_remote(remote, data_dir / "output")
    source = str(cfg.get("MANIFEST") or "")
    if not source:
        return [], "服务端没有配置模板清单"
    try:
        from app.core import load_manifest
        return load_manifest(source), ""
    except Exception as exc:  # noqa: BLE001 - 对账允许在离线时降级
        return [], str(exc)


def _tag_store_command(args, web: dict) -> int:
    """--import-tags / --export-tags：主机端迁移与导出，不进入 serve。"""
    from tools.template_catalog import load_xlsx
    from tools.template_store import StoreError, TemplateStore
    from app.web.templates import TemplateCatalog

    base = appconfig.app_dir()
    data_dir = Path(web.get("data_root") or "web-data")
    if not data_dir.is_absolute():
        data_dir = base / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    store_path = resolve_tag_store_path(web, base, data_dir)
    assert store_path is not None

    try:
        if args.export_tags is not None:
            store = TemplateStore.load(store_path)
            target = (Path(args.export_tags) if args.export_tags
                      else data_dir / "exports" / f"新标签清单-{time.strftime('%Y%m%d-%H%M')}.xlsx")
            store.export_to_xlsx(target)
            print(f"已导出：{target}（revision {store.revision}，{len(store.rows())} 行）")
            return 0

        source = args.from_xlsx or os.getenv("OUTFIT_TAG_WORKBOOK") or web.get("tag_workbook")
        if not source:
            raise RuntimeError("没有可导入的标签清单：用 --from-xlsx 指定，或在 web.json 配置 tag_workbook")
        workbook = Path(str(source)).expanduser()
        if not workbook.is_absolute():
            workbook = base / workbook
        if not workbook.is_file():
            raise RuntimeError(f"标签清单不存在：{workbook}")

        store = TemplateStore.load(store_path, create_empty=True)
        if store.rows() and not (args.dry_run or args.force):
            raise RuntimeError(
                f"真源已存在（{len(store.rows())} 行，revision {store.revision}）：{store_path}\n"
                "确认覆盖请加 --force；只想看对账请加 --dry-run")

        report = store.import_from_xlsx(workbook, dry_run=True)
        manifest, warning = _manifest_items(base, data_dir)
        project = load_xlsx(workbook)
        old_info = TemplateCatalog(manifest).attach_tag_workbook(workbook)
        new_info = TemplateCatalog(manifest).attach_tag_store(_PendingStore(project, store_path))

        print(f"源清单：{workbook}")
        print(f"行数：{report['rows']}（未打标 {report['untagged']} / 显式弃用 {report['deprecated']} / "
              f"缺指纹 {report['no_fingerprint']} / 缺大小 {report['no_size']}）")
        print(f"标签字典：{report['tags']} 个")
        print(f"可生成（已匹配云端）：{new_info['source_ready_templates']} 条"
              f"（旧 xlsx 口径 {old_info['source_ready_templates']} 条）")
        print(f"未匹配云端：{new_info['manifest_unmatched']} 条 / 云端索引 {new_info['manifest_entries']} 条")
        print(f"真源：{store_path}（当前 revision {store.revision}）")
        if warning:
            print(f"提示：云端模板索引不可用（{warning}），本次不校验 ready 数")

        mismatch = []
        if old_info["source_ready_templates"] != new_info["source_ready_templates"]:
            mismatch.append("可生成条数与旧 xlsx 口径不一致")
        if old_info["untagged_skipped"] != new_info["untagged_skipped"]:
            mismatch.append("未打标条数与旧 xlsx 口径不一致")
        if report["deprecated"] and old_info["untagged_skipped"] == 0:
            mismatch.append("清单里出现显式弃用行，但旧口径没有未打标行")

        if args.dry_run:
            print("dry-run：未写入任何文件。")
            if mismatch and not args.force:
                print("对账差异：" + "；".join(mismatch) + "（确认无误可加 --force）", file=sys.stderr)
                return 2
            return 0

        if mismatch and not args.force:
            print("对账差异：" + "；".join(mismatch) + "（确认无误可加 --force）", file=sys.stderr)
            return 2

        result = store.import_from_xlsx(workbook)
        print(f"已导入：{result['rows']} 行，revision {result['revision']} → {store_path}")
        print("下一步：重启网页服务，进「标签」页核对。回滚：把该文件改名即可退回 xlsx 模式。")
        return 0
    except (StoreError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="换装批量生产器局域网网页服务")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true", help="初始化网页服务并创建管理员")
    group.add_argument("--add-user", metavar="用户名", help="添加普通用户")
    group.add_argument("--reset-password", metavar="用户名", help="重置用户密码")
    group.add_argument("--list-users", action="store_true", help="列出用户")
    group.add_argument("--remove-user", metavar="用户名", help="停用用户")
    group.add_argument("--selftest", action="store_true", help="检查网页模块与本地图片依赖")
    group.add_argument("--import-tags", action="store_true", help="把标签 xlsx 一次性导入服务端 JSON 真源")
    group.add_argument("--export-tags", metavar="PATH", nargs="?", const="",
                       help="把 JSON 真源导出为 xlsx（默认写到 web-data/exports/）")
    parser.add_argument("--role", choices=("admin", "user"), default="user")
    parser.add_argument("--host", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, help="端口，默认 8765")
    parser.add_argument("--from-xlsx", metavar="PATH", help="导入源；默认取 web.json 的 tag_workbook")
    parser.add_argument("--dry-run", action="store_true", help="只对账，不写盘")
    parser.add_argument("--force", action="store_true", help="对账有差异或真源已存在时仍继续")
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
                "max_job_items": 3, "confirm_over_items": 5, "max_upload_mb": 20,
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
        if args.import_tags or args.export_tags is not None:
            return _tag_store_command(args, web)
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
