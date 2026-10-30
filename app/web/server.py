#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机局域网 Web 服务：浏览器仅访问本机，生成与 COS 由主机发起。"""
from __future__ import annotations

import base64
import html
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie

from app import config as appconfig
from app.media import MediaError, validate_image
from app.prompts import PromptLibrary
from app.runtime import build_storages
from app.web.auth import LoginLimiter, SessionSigner, make_password, verify_password
from app.web.jobs import JobManager
from app.web.settings import WebSettings, load_settings
from app.web.templates import TemplateCatalog, TemplateError
from app.web import WEB_VERSION

MAX_BODY = 32 * 1024 * 1024
USERNAME_RE = re.compile(r"^[\w.-]{2,32}$", re.UNICODE)


def _local_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower()}
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            hosts.add(str(item[4][0]).split("%", 1)[0].lower())
    except OSError:
        pass
    try:
        import subprocess
        out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=3,
                             errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        hosts.update(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", out.stdout))
    except Exception:
        pass
    hosts.update(("0.0.0.0", "::"))
    return {h.lower() for h in hosts}


def _load_prompts() -> dict[str, dict]:
    from app.core import DEFAULT_PROMPT
    prompts = {"standard": {"title": "标准换装", "body": DEFAULT_PROMPT}}
    try:
        lib = PromptLibrary.load()
        # 服装定妆用于图片定妆，不是视频生成提示词，网页生产页不展示。
        prompts.update({p.id: {"title": p.title, "body": p.body} for p in lib.prompts
                        if p.id != "p01" and p.title.strip() != "服装定妆"})
    except Exception:
        pass
    return prompts


class WebApplication:
    def __init__(self, settings: WebSettings):
        self.settings = settings
        self.prompts = _load_prompts()
        self.catalog = TemplateCatalog(settings.templates)
        self.catalog.tag_workbook_info = settings.tag_workbook_info or {}
        self.signer = SessionSigner(settings.web["session_secret"], settings.web.get("session_hours", 12))
        self.limiter = LoginLimiter()
        self.jobs = JobManager(settings, {key: value["body"] for key, value in self.prompts.items()})
        self.started_at = time.time()
        self.uploads_dir = settings.data_dir / "uploads"
        self.upload_meta = self.uploads_dir / "meta"
        self.upload_meta.mkdir(parents=True, exist_ok=True)
        self._template_store = None
        self._template_store_lock = threading.Lock()

    def user_for_token(self, token: str) -> dict | None:
        return self.signer.verify(token, self.settings.users)

    def persist_web(self) -> None:
        path = self.settings.web_file
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.settings.web, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def template_store(self):
        with self._template_store_lock:
            if self._template_store is None:
                template_store, _ = build_storages(self.settings.config)
                self._template_store = template_store
            return self._template_store

    def reload_templates(self) -> None:
        from app.core import load_manifest
        manifest = str(self.settings.config.get("MANIFEST") or "")
        items: list[dict] = []
        warning = ""
        if manifest:
            try:
                items = load_manifest(manifest)
            except Exception as exc:
                warning = f"云端模板索引不可用（{exc}），本次仅按本地生产清单筛选"
        catalog = TemplateCatalog(items)
        tag_info = {}
        workbook = self.settings.tag_workbook_path
        if workbook is not None and Path(workbook).is_file():
            tag_info = catalog.attach_tag_workbook(workbook)
            if warning:
                tag_info["manifest_warning"] = warning
        elif not items:
            raise RuntimeError("既没有可用的标签清单，也没有云端模板索引")
        self.catalog = catalog
        self.catalog.tag_workbook_info = tag_info
        self.settings.templates = catalog.items
        self.settings.tag_workbook_info = tag_info
        self.settings.templates_error = ""


def _read_image_format(path: Path) -> str:
    from PIL import Image
    with Image.open(path) as image:
        fmt = (image.format or "").upper()
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp"}.get(fmt, "")


def _safe_user_name(username: str) -> bool:
    return bool(USERNAME_RE.fullmatch(username))


def make_handler(app: WebApplication):
    static_root = Path(__file__).resolve().parent / "static"
    allowed_hosts = _local_hosts()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OutfitLocal/1"
        sys_version = ""

        def log_message(self, fmt, *args):
            # 不记录请求体、Cookie、密码或签名 URL。
            try:
                line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {fmt % args}"
                log_dir = app.settings.data_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                with (log_dir / f"server-{time.strftime('%Y%m%d')}.log").open("a", encoding="utf-8") as fh:
                    fh.write(line[:1500] + "\n")
            except OSError:
                pass

        def _headers(self, content_type="application/json; charset=utf-8", length=0, status=200,
                     extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: blob:; media-src 'self'; "
                             "connect-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self'; frame-ancestors 'none'")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()

        def _json(self, obj, status=200, extra=None):
            raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._headers(length=len(raw), status=status, extra=extra)
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _error(self, message, status=400):
            self._json({"ok": False, "error": str(message)[:600]}, status)

        def _body(self, limit=MAX_BODY):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("请求长度无效") from None
            if length < 0 or length > limit:
                raise OverflowError("请求过大，请缩小图片后重试")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求未完整接收，请重试")
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("请求格式不正确") from None
            if not isinstance(data, dict):
                raise ValueError("请求格式不正确")
            return data

        def _host_ok(self):
            host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
            return host in allowed_hosts

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            if not origin:
                return True
            try:
                parsed = urllib.parse.urlsplit(origin)
                origin_host = (parsed.hostname or "").lower()
                host_header = self.headers.get("Host", "").lower()
                return parsed.scheme in ("http", "https") and origin_host in allowed_hosts and parsed.netloc.lower() == host_header
            except ValueError:
                return False

        def _token(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                return cookie.get("outfit_sid").value if cookie.get("outfit_sid") else ""
            except Exception:
                return ""

        def _user(self, admin=False):
            user = app.user_for_token(self._token())
            if not user:
                self._error("登录已过期，请重新登录。", 401)
                return None
            if admin and user.get("role") != "admin":
                self._error("此操作需要管理员权限。", 403)
                return None
            return user

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._error("访问地址无效。", 421)
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path == "/healthz":
                return self._json({"ok": True, "version": WEB_VERSION})
            if path == "/":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            if path == "/api/me":
                user = self._user()
                return self._json({"ok": True, "user": user}) if user else None
            if path == "/api/bootstrap":
                user = self._user()
                if not user:
                    return
                cfg = app.settings.config
                return self._json({"ok": True, "user": user,
                                   "service": {"provider": cfg.get("WAN_PROVIDER"), "model": cfg.get("WAN_MODEL"),
                                               "cos_enabled": appconfig.truthy(cfg.get("COS_ENABLED")),
                                               "templates_count": len(app.catalog.items),
                                               "templates_error": app.settings.templates_error,
                                               "max_items": app.settings.max_items,
                                                "tag_filter_enabled": any(app.catalog.tag_values.values()),
                                                "tagged_templates_count": len(app.catalog.items) if app.catalog.tag_workbook_info else 0,
                                                "source_ready_templates": int((app.catalog.tag_workbook_info or {}).get("source_ready_templates", 0)),
                                                "unlisted_files": (app.catalog.tag_workbook_info or {}).get("unlisted_files"),
                                                "manifest_warning": (app.catalog.tag_workbook_info or {}).get("manifest_warning", ""),
                                               "notice": cfg.get("NOTICE") or ""},
                                   "prompts": [{"id": k, "title": v["title"], "body": v["body"]} for k, v in app.prompts.items()]})
            if path == "/api/templates":
                if not self._user():
                    return
                if app.settings.templates_error:
                    return self._error("视频场景暂不可用，请联系管理员。", 503)
                return self._json({"ok": True, "items": app.catalog.public(),
                                   "categories": app.catalog.tag_categories,
                                   "tag_values": app.catalog.tag_values,
                                   "tag_filter_enabled": any(app.catalog.tag_values.values())})
            if path.startswith("/api/templates/") and path.endswith("/preview"):
                if not self._user():
                    return
                template_id = path.split("/")[-2]
                return self._preview(template_id)
            if path.startswith("/api/jobs/"):
                user = self._user()
                if not user:
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "log":
                    return self._job_log(parts[2], user)
                if len(parts) == 4 and parts[3] == "report.csv":
                    return self._job_report(parts[2], user)
                if len(parts) == 5 and parts[3] == "download":
                    return self._job_download(parts[2], parts[4], user)
                if len(parts) == 4 and parts[3] == "files.zip":
                    return self._job_zip(parts[2], user)
                if len(parts) == 3:
                    return self._job_detail(parts[2], user)
            if path == "/api/jobs":
                user = self._user()
                if not user:
                    return
                query = urllib.parse.parse_qs(parsed.query)
                all_jobs = query.get("scope", ["mine"])[0] == "all"
                if all_jobs and user.get("role") != "admin":
                    return self._error("此操作需要管理员权限。", 403)
                return self._json({"ok": True, "jobs": app.jobs.list_for(user["username"], all_jobs)})
            if path == "/api/admin/status":
                if not self._user(admin=True):
                    return
                usage = shutil.disk_usage(app.settings.data_dir)
                with app.jobs.lock:
                    queued_count = sum(j["status"] == "queued" for j in app.jobs.jobs.values())
                    active_count = len(app.jobs.active)
                return self._json({"ok": True, "version": WEB_VERSION,
                                   "templates": len(app.catalog.items), "queued": queued_count,
                                   "active": active_count, "free_gb": round(usage.free / (1024**3), 1),
                                   "users": [{"username": n, "role": u.get("role", "user"), "enabled": u.get("enabled", True)}
                                             for n, u in app.settings.users.items()]})
            if path == "/api/admin/users":
                if not self._user(admin=True):
                    return
                return self._json({"ok": True, "users": [{"username": n, "role": u.get("role", "user"),
                                   "enabled": u.get("enabled", True)} for n, u in app.settings.users.items()]})
            return self._error("页面或接口不存在。", 404)

        def do_POST(self):
            if not self._host_ok() or not self._origin_ok():
                return self._error("请求来源无效，请刷新页面后重试。", 403)
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/logout":
                extra = {"Set-Cookie": "outfit_sid=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"}
                return self._json({"ok": True}, extra=extra)
            if path == "/api/login":
                try:
                    data = self._body(16 * 1024)
                except (OverflowError, ValueError) as exc:
                    return self._error(exc, 413 if isinstance(exc, OverflowError) else 400)
                return self._login(data)
            user = self._user()
            if not user:
                return
            try:
                data = self._body()
            except OverflowError as exc:
                return self._error(exc, 413)
            except ValueError as exc:
                return self._error(exc, 400)
            if path == "/api/uploads":
                return self._upload(data, user)
            if path == "/api/jobs":
                return self._create_job(data, user)
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.strip("/").split("/")[2]
                return self._cancel(job_id, user)
            if path.startswith("/api/jobs/") and path.endswith("/retry"):
                job_id = path.strip("/").split("/")[2]
                source = self._authorized_job(job_id, user)
                if not source:
                    return self._error("找不到该任务。", 404)
                try:
                    retry = app.jobs.retry_failed(job_id, user["username"])
                    return self._json({"ok": True, "job": retry}, 201)
                except ValueError as exc:
                    return self._error(exc, 409)
            if path == "/api/admin/reload-templates":
                if user.get("role") != "admin":
                    return self._error("此操作需要管理员权限。", 403)
                try:
                    app.reload_templates()
                    return self._json({"ok": True, "count": len(app.catalog.items)})
                except Exception as exc:
                    return self._error(f"重载失败：{exc}", 503)
            if path == "/api/admin/users":
                if user.get("role") != "admin":
                    return self._error("此操作需要管理员权限。", 403)
                return self._admin_add_user(data)
            if path.startswith("/api/admin/users/"):
                if user.get("role") != "admin":
                    return self._error("此操作需要管理员权限。", 403)
                parts = path.strip("/").split("/")
                username = urllib.parse.unquote(parts[3]) if len(parts) >= 4 else ""
                if len(parts) == 5 and parts[4] == "reset":
                    return self._admin_reset_user(username, data)
                if len(parts) == 5 and parts[4] == "disable":
                    return self._admin_disable_user(username, user)
                if len(parts) == 5 and parts[4] == "enable":
                    return self._admin_enable_user(username)
            return self._error("页面或接口不存在。", 404)

        def _login(self, data):
            username = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            key = self.client_address[0] + "|" + username
            if not app.limiter.allowed(key):
                return self._error("尝试次数过多，请 10 分钟后再试。", 429)
            record = app.settings.users.get(username)
            ok = bool(record and record.get("enabled", True) and verify_password(password, record))
            if record and record.get("expires") and str(record["expires"]) < time.strftime("%Y-%m-%d"):
                ok = False
            if not ok:
                app.limiter.failed(key)
                # 未知账号也执行一次 PBKDF2，降低账号枚举时序差异。
                if not record:
                    import hashlib
                    from app.web.auth import PBKDF2_ROUNDS
                    hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"unknown-user-salt", PBKDF2_ROUNDS)
                return self._error("账号或密码不正确。", 401)
            app.limiter.succeeded(key)
            token = app.signer.issue(username, record)
            extra = {"Set-Cookie": f"outfit_sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={app.signer.hours * 3600}"}
            return self._json({"ok": True, "user": {"username": username, "role": record.get("role", "user")}}, extra=extra)

        def _upload(self, data, user):
            filename = Path(str(data.get("filename") or "outfit.jpg")).name
            ext = Path(filename).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
                return self._error("请上传 JPG、PNG 或 WebP 图片；iPhone HEIC 请先另存为 JPG。")
            encoded = str(data.get("data") or "")
            if "," in encoded and encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[1]
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception:
                return self._error("图片上传失败，请重新选择图片。")
            max_bytes = int(float(app.settings.web.get("max_upload_mb", 20)) * 1024 * 1024)
            if not raw or len(raw) > max_bytes:
                return self._error(f"图片为空或超过 {max_bytes // (1024 * 1024)}MB。", 413)
            upload_id = uuid.uuid4().hex
            folder = app.uploads_dir / upload_id[:2]
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{upload_id}{ext}"
            path.write_bytes(raw)
            try:
                info = validate_image(path)
                actual_ext = _read_image_format(path)
                if not actual_ext:
                    raise MediaError("图片格式无法识别")
                if actual_ext != ext:
                    corrected = path.with_suffix(actual_ext)
                    os.replace(path, corrected)
                    path = corrected
            except Exception as exc:
                path.unlink(missing_ok=True)
                return self._error(str(exc))
            meta = {"id": upload_id, "owner": user["username"], "path": str(path),
                    "name": filename[:120], "created_at": time.time(), "info": info}
            (app.upload_meta / f"{upload_id}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            return self._json({"ok": True, "upload_id": upload_id, "name": filename,
                               "width": info.get("width"), "height": info.get("height"),
                               "size_mb": info.get("size_mb")}, 201)

        def _create_job(self, data, user):
            upload_id = str(data.get("upload_id") or "")
            if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
                return self._error("请先上传服装图片。")
            meta_path = app.upload_meta / f"{upload_id}.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self._error("图片已失效，请重新上传。", 404)
            if meta.get("owner") != user["username"] or not Path(meta.get("path", "")).is_file():
                return self._error("找不到这张图片，请重新上传。", 404)
            if app.settings.templates_error:
                return self._error("视频场景暂不可用，请联系管理员。", 503)
            try:
                mode = str(data.get("selection") or "random")
                count = int(data.get("count") or 1)
                templates = app.catalog.choose(mode, count, str(data.get("template_id") or ""),
                                               app.settings.max_items, data.get("tag_filters"))
                pending = [str(t.get("id")) for t in templates if not (t.get("url") or t.get("key"))]
                if pending:
                    raise TemplateError(
                        f"所选视频尚未上传到云端模板库（例如 {pending[0]}），当前只支持标签筛选与预览；"
                        "完成批量上传后即可生成。")
                prompt_id = str(data.get("prompt_id") or next(iter(app.prompts)))
                if count > int(app.settings.web.get("confirm_over_items", 5)) and not data.get("confirmed"):
                    return self._error("批量数量较多，请确认后再提交。", 409)
                job = app.jobs.create(user["username"], Path(meta["path"]), meta["name"], templates,
                                      prompt_id, str(data.get("resolution") or "480P"),
                                      int(data.get("duration") or 5), str(data.get("prompt") or ""))
                return self._json({"ok": True, "job": job}, 201)
            except (ValueError, TemplateError) as exc:
                return self._error(exc)

        def _cancel(self, job_id, user):
            job = app.jobs.get(job_id)
            if not job:
                return self._error("找不到该任务。", 404)
            if user.get("role") != "admin" and job.get("owner") != user.get("username"):
                return self._error("无权操作该任务。", 403)
            if not app.jobs.cancel(job_id):
                return self._error("该任务目前不能取消。", 409)
            return self._json({"ok": True})

        def _admin_add_user(self, data):
            username = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            role = str(data.get("role") or "user")
            if not _safe_user_name(username):
                return self._error("账号请使用 2–32 位字母、数字、下划线、点或短横线。")
            if username in app.settings.users:
                return self._error("账号已存在。", 409)
            if len(password) < 8:
                return self._error("密码至少需要 8 位。")
            if role not in ("user", "admin"):
                return self._error("用户角色无效。")
            app.settings.users[username] = {**make_password(password), "role": role,
                                            "enabled": True, "version": 1, "expires": ""}
            app.persist_web()
            return self._json({"ok": True, "username": username}, 201)

        def _admin_reset_user(self, username, data):
            user = app.settings.users.get(username)
            password = str(data.get("password") or "")
            if not user:
                return self._error("账号不存在。", 404)
            if len(password) < 8:
                return self._error("新密码至少需要 8 位。")
            user.update(make_password(password))
            user["version"] = int(user.get("version", 1)) + 1
            app.persist_web()
            return self._json({"ok": True})

        def _admin_disable_user(self, username, actor):
            user = app.settings.users.get(username)
            if not user:
                return self._error("账号不存在。", 404)
            if username == actor.get("username"):
                return self._error("不能停用当前登录的管理员账号。", 409)
            if user.get("role") == "admin" and user.get("enabled", True):
                admins = [u for u in app.settings.users.values() if u.get("role") == "admin" and u.get("enabled", True)]
                if len(admins) <= 1:
                    return self._error("至少保留一个可用管理员。", 409)
            user["enabled"] = False
            user["version"] = int(user.get("version", 1)) + 1
            app.persist_web()
            return self._json({"ok": True})

        def _admin_enable_user(self, username):
            user = app.settings.users.get(username)
            if not user:
                return self._error("账号不存在。", 404)
            user["enabled"] = True
            user["version"] = int(user.get("version", 1)) + 1
            app.persist_web()
            return self._json({"ok": True})

        def _job_detail(self, job_id, user):
            job = app.jobs.get(job_id)
            if not job:
                return self._error("找不到该任务。", 404)
            if user.get("role") != "admin" and job.get("owner") != user.get("username"):
                return self._error("无权查看该任务。", 403)
            return self._json({"ok": True, "job": job})

        def _authorized_job(self, job_id, user):
            job = app.jobs.get(job_id)
            if not job:
                return None
            if user.get("role") != "admin" and job.get("owner") != user.get("username"):
                return None
            return job

        def _job_log(self, job_id, user):
            job = self._authorized_job(job_id, user)
            if not job:
                return self._error("找不到该任务。", 404)
            return self._json({"ok": True, "lines": job.get("log_tail", [])})

        def _job_report(self, job_id, user):
            job = self._authorized_job(job_id, user)
            out = app.jobs.output_path(job_id)
            path = out / "report.csv" if out else None
            if not job or not path or not path.is_file():
                return self._error("报告暂不可下载。", 404)
            return self._send_file(path, "text/csv; charset=utf-8", f"{job_id}-report.csv")

        def _job_download(self, job_id, template_id, user):
            job = self._authorized_job(job_id, user)
            if not job:
                return self._error("找不到该任务。", 404)
            if template_id not in job.get("template_ids", []):
                return self._error("找不到该视频。", 404)
            rec = next((r for r in job.get("records", []) if str(r.get("id")) == template_id), None)
            out = app.jobs.output_path(job_id)
            path = out / f"{template_id}.mp4" if out else None
            if not rec or rec.get("status") != "success" or not path or not path.is_file():
                return self._error("视频还没有生成完成。", 404)
            return self._send_file(path, "video/mp4", f"{job_id}-{template_id}.mp4", inline=self.headers.get("X-Playback") == "1")

        def _job_zip(self, job_id, user):
            job = self._authorized_job(job_id, user)
            out = app.jobs.output_path(job_id)
            if not job or not out or not out.is_dir():
                return self._error("暂时没有可下载的视频。", 404)
            zip_path = app.settings.data_dir / "tmp" / f"{job_id}.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for tid in job.get("template_ids", []):
                        video = out / f"{tid}.mp4"
                        if video.is_file():
                            zf.write(video, f"{tid}.mp4")
                    report = out / "report.csv"
                    if report.is_file():
                        zf.write(report, "report.csv")
            except OSError as exc:
                return self._error(f"打包失败：{exc}", 500)
            if zip_path.stat().st_size == 0:
                return self._error("暂时没有可下载的视频。", 404)
            return self._send_file(zip_path, "application/zip", f"{job_id}.zip")

        def _preview(self, template_id):
            try:
                item = app.catalog.preview(template_id)
                url = str(item.get("url") or "")
                if not url and not item.get("key"):
                    return self._error("该视频尚未上传到云端，暂无预览。", 404)
                if not url and item.get("key"):
                    store = app.template_store()
                    if store is None:
                        return self._error("模板预览暂不可用。", 503)
                    url = store.sign_url(item["key"], 3600)
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    return self._error("模板预览地址无效。", 502)
                headers = {"User-Agent": "OutfitLocal/1"}
                if self.headers.get("Range"):
                    headers["Range"] = self.headers["Range"]
                req = urllib.request.Request(url, headers=headers)
                try:
                    response = urllib.request.urlopen(req, timeout=30)
                except urllib.error.HTTPError as exc:
                    response = exc
                status = getattr(response, "status", 200)
                self.send_response(status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "video/mp4"))
                for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
                    value = response.headers.get(header)
                    if value:
                        self.send_header(header, value)
                self.send_header("Cache-Control", "private, max-age=300")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    shutil.copyfileobj(response, self.wfile, length=64 * 1024)
                response.close()
            except (TemplateError, OSError, urllib.error.URLError, RuntimeError) as exc:
                return self._error(f"模板预览失败：{exc}", 502)

        def _static(self, relative):
            target = (static_root / relative).resolve()
            try:
                target.relative_to(static_root.resolve())
            except ValueError:
                return self._error("页面不存在。", 404)
            if not target.is_file():
                return self._error("页面不存在。", 404)
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if mime.startswith("text/") or mime in ("application/javascript", "image/svg+xml"):
                mime += "; charset=utf-8"
            return self._send_file(target, mime, target.name, attachment=False)

        def _send_file(self, path: Path, mime: str, filename: str, inline=False, attachment=True):
            size = path.stat().st_size
            start, end, status = 0, max(0, size - 1), 200
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if not match or size == 0:
                    self._headers("text/plain", 0, 416, {"Content-Range": f"bytes */{size}"})
                    return
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    suffix = int(last or 0)
                    start = max(0, size - suffix)
                    end = size - 1
                if start >= size or end < start:
                    self._headers("text/plain", 0, 416, {"Content-Range": f"bytes */{size}"})
                    return
                end = min(end, size - 1)
                status = 206
            length = max(0, end - start + 1)
            disposition = "inline" if inline else "attachment" if attachment else "inline"
            quoted = urllib.parse.quote(filename)
            extra = {"Accept-Ranges": "bytes", "Content-Disposition": f"{disposition}; filename*=UTF-8''{quoted}"}
            if status == 206:
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            self._headers(mime, length, status, extra)
            if self.command == "HEAD":
                return
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    return Handler


def serve(host: str | None = None, port: int | None = None) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"启动失败：{exc}")
        return 2
    app = WebApplication(settings)
    bind_host = host or str(settings.web.get("host") or "0.0.0.0")
    bind_port = int(port or settings.web.get("port") or 8765)
    try:
        server = ThreadingHTTPServer((bind_host, bind_port), make_handler(app))
    except OSError as exc:
        print(f"无法监听 {bind_host}:{bind_port}：{exc}")
        app.jobs.shutdown()
        return 2
    server.daemon_threads = True
    server.allow_reuse_address = True
    addresses = sorted(a for a in _local_hosts() if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", a)
                       and not a.startswith(("127.", "169.254.")))
    print(f"换装批量生产器 网页版 v{WEB_VERSION}")
    print(f"本机访问：http://127.0.0.1:{bind_port}")
    for address in addresses:
        print(f"局域网访问：http://{address}:{bind_port}")
    print("客户端只需浏览器；按 Ctrl+C 停止服务。")
    address_file = settings.data_dir / "访问地址.txt"
    address_file.write_text("\n".join([f"http://127.0.0.1:{bind_port}"] + [f"http://{a}:{bind_port}" for a in addresses]), encoding="utf-8")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止网页服务…")
    finally:
        server.shutdown()
        server.server_close()
        app.jobs.shutdown()
    return 0
