#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标签页 HTTP 接口测试：真起一个本地服务，不联网、不调 Wan/COS。

覆盖：
1. 普通用户（非 admin）能改标签、弃用、维护标签库；admin 专属接口仍只给 admin。
2. revision 乐观锁：旧值 409 且带最新 revision，换新值可继续。
3. 弃用 = 软删除：候选集 /api/templates 里消失，快照里仍可见且可恢复。
4. 标签库删除必须先看影响面（没有 confirm 就 400 并回带 impact）。
5. 页面能拿到 tag-view 与静态资源，CSP 仍然是 script-src 'self'。
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from http.server import ThreadingHTTPServer  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app.web.auth import make_password  # noqa: E402
from app.web.server import WebApplication, make_handler  # noqa: E402
from app.web.settings import load_settings  # noqa: E402
from tools.template_store import TemplateStore  # noqa: E402

TAGS = [("近景", "景别"), ("中远景", "景别"), ("适合秋冬", "适合季节"), ("酒店", "场景")]


def call(opener, base, path, method="GET", payload=None, headers=None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base + path, data=raw, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def body(raw):
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}



def post_raw(opener, base, path, data: bytes, headers):
    request = urllib.request.Request(base + path, data=data, method="POST", headers=headers)
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()

def write_workbook(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "视频标签清单"
    sheet.append(["模板ID", "相对路径", "文件名", "时长(秒)", "大小(MB)", "文件指纹", "标签", "状态"])
    sheet.append(["001", "0001.mp4", "0001.mp4", 7.5, 8.59, "aaaa1111", "近景;适合秋冬", "已上传"])
    sheet.append(["002", "0002.mp4", "0002.mp4", 5, 7.34, "bbbb2222", "中远景;适合秋冬", "待上传"])
    sheet.append(["003", "0003.mp4", "0003.mp4", 6, 9.0, "cccc3333", "近景", "待上传"])
    sheet.append(["004", "0004.mp4", "0004.mp4", 6, 3.0, "dddd4444", "", "待上传"])
    tags = workbook.create_sheet("标签字典")
    tags.append(["标签值", "分类"])
    for tag, category in TAGS:
        tags.append([tag, category])
    meta = workbook.create_sheet("项目")
    meta.append(["项目字段", "值"])
    meta.append(["项目ID", "testproject01"])
    meta.append(["本机视频文件夹", str(path.parent)])
    meta.append(["下一个编号", 5])
    meta.sheet_state = "hidden"
    workbook.save(path)
    workbook.close()
    return path


def build_env(tmp: Path):
    store_path = tmp / "web-data" / "template_tags.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = TemplateStore.load(store_path, create_empty=True)
    store.import_from_xlsx(write_workbook(tmp / "清单.xlsx"))

    manifest = tmp / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "count": 3, "templates": [
        {"id": "001", "name": "0001.mp4", "filename": "0001.mp4", "relative_path": "0001.mp4",
         "size_mb": 8.59, "url": "http://127.0.0.1:9/0001.mp4"},
        {"id": "002", "name": "0002.mp4", "filename": "0002.mp4", "relative_path": "0002.mp4",
         "size_mb": 7.34, "url": "http://127.0.0.1:9/0002.mp4"},
        {"id": "003", "name": "0003.mp4", "filename": "0003.mp4", "relative_path": "0003.mp4",
         "size_mb": 9.0, "url": "http://127.0.0.1:9/0003.mp4"},
    ]}), encoding="utf-8")
    api_config = tmp / "config.json"
    api_config.write_text(json.dumps({
        "wan": {"provider": "dashscope", "api_key": "mock-key", "model": "wan3.0-video"},
        "manifest_url": str(manifest),
        "cos": {"enabled": True, "provider": "local", "root": str(tmp / "cos-root"),
                "bucket": "local-000000"},
    }), encoding="utf-8")
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    web_config = tmp / "web.json"
    web_config.write_text(json.dumps({
        "session_secret": secret, "session_hours": 1, "data_root": str(tmp / "web-data"),
        "tag_store": str(store_path), "max_job_items": 3,
        "users": {
            "alice": {**make_password("alice-pass-123"), "role": "user", "enabled": True, "version": 1},
            "bob": {**make_password("bob-pass-12345"), "role": "user", "enabled": True, "version": 1},
            "admin": {**make_password("admin-pass-123"), "role": "admin", "enabled": True, "version": 1},
        },
    }), encoding="utf-8")
    settings = load_settings(base_dir=tmp, web_file=web_config, api_file=api_config, load_templates=True)
    assert settings.tag_source == "store", settings.tag_source
    return settings, store_path


def main() -> int:
    tmp = tempdir("webtagapi-")
    settings, store_path = build_env(tmp)
    app = WebApplication(settings)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    alice = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    bob = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    try:
        # ---------- 页面与静态资源 ----------
        code, headers, raw = call(alice, base, "/")
        assert code == 200, code
        html = raw.decode("utf-8")
        for needle in ('data-view="tag-view"', "/static/tag.js", "/static/tag.css", ">标签<"):
            assert needle in html, f"页面缺少 {needle}"
        assert "标签" in html.split('data-view="admin-view"')[1].split("RunningHub")[0], "「标签」必须夹在管理与 RunningHub 之间"
        csp = headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp, csp

        # ---------- 未登录 ----------
        code, _, _ = call(alice, base, "/api/tags/snapshot")
        assert code == 401, code

        for opener, user, password in ((alice, "alice", "alice-pass-123"),
                                       (bob, "bob", "bob-pass-12345"),
                                       (admin, "admin", "admin-pass-123")):
            code, _, _ = call(opener, base, "/api/login", "POST", {"username": user, "password": password})
            assert code == 200, (user, code)

        # ---------- 快照 ----------
        code, _, raw = call(alice, base, "/api/tags/snapshot")
        assert code == 200, code
        snap = body(raw)
        assert snap["enabled"] is True and snap["source"] == "store"
        assert snap["revision"] >= 1
        assert {video["id"] for video in snap["videos"]} == {"001", "002", "003", "004"}
        assert snap["stats"]["untagged"] == 1 and snap["stats"]["ready"] == 3, snap["stats"]
        assert snap["library"]["tags"] and snap["library"]["usage"]["近景"] == 2
        revision = snap["revision"]

        code, _, raw = call(alice, base, f"/api/tags/snapshot?rev={revision}")
        assert code == 200 and body(raw).get("unchanged") is True, body(raw)

        # ---------- 普通用户改标签 ----------
        code, _, raw = call(alice, base, "/api/tags/videos/tags", "POST",
                            {"revision": revision, "ids": ["004"], "tags": ["近景"], "mode": "add"})
        assert code == 200, body(raw)
        result = body(raw)
        assert result["changed"] == 1 and result["revision"] == revision + 1
        assert result["updated"][0]["tags"] == ["近景"]
        assert result["updated"][0]["ready"] is False, "004 不在云端索引里"
        revision = result["revision"]

        # ---------- 409 乐观锁 ----------
        code, _, raw = call(bob, base, "/api/tags/videos/tags", "POST",
                            {"revision": revision - 1, "ids": ["004"], "tags": ["酒店"], "mode": "replace"})
        assert code == 409, (code, body(raw))
        conflict = body(raw)
        assert conflict.get("conflict") is True and conflict["revision"] == revision, conflict

        code, _, raw = call(bob, base, "/api/tags/videos/tags", "POST",
                            {"revision": conflict["revision"], "ids": ["004"], "tags": ["酒店"], "mode": "replace"})
        assert code == 200, body(raw)
        revision = body(raw)["revision"]

        # ---------- 弃用 = 软删除 ----------
        code, _, raw = call(alice, base, "/api/tags/videos/deprecate", "POST",
                            {"revision": revision, "ids": ["001"], "deprecated": True, "reason": "重复画面"})
        assert code == 200, body(raw)
        revision = body(raw)["revision"]

        code, _, raw = call(alice, base, "/api/templates")
        assert code == 200
        assert "001" not in {item["id"] for item in body(raw)["items"]}, "弃用后不能再进候选集"

        code, _, raw = call(alice, base, "/api/tags/snapshot")
        deprecated = {video["id"]: video for video in body(raw)["videos"]}["001"]
        assert deprecated["deprecated"] is True and deprecated["tags"] == ["近景", "适合秋冬"], deprecated

        code, _, raw = call(alice, base, "/api/tags/videos/deprecate", "POST",
                            {"revision": revision, "ids": ["001"], "deprecated": False})
        assert code == 200, body(raw)
        revision = body(raw)["revision"]
        code, _, raw = call(alice, base, "/api/templates")
        assert "001" in {item["id"] for item in body(raw)["items"]}, "恢复后必须回到候选集"

        # ---------- 影响预览只读 ----------
        before = body(call(alice, base, "/api/tags/snapshot")[2])["revision"]
        code, _, raw = call(alice, base, "/api/tags/impact", "POST", {"action": "delete", "name": "近景"})
        assert code == 200, body(raw)
        preview = body(raw)
        assert preview["affected"] == 2, preview  # 001 与 003 用「近景」
        assert body(call(alice, base, "/api/tags/snapshot")[2])["revision"] == before, "impact 不能改 revision"

        # ---------- 标签库删除：没 confirm 必须 400 ----------
        code, _, raw = call(alice, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "delete", "name": "近景"})
        assert code == 400, (code, body(raw))
        blocked = body(raw)
        assert blocked["confirm_required"] is True and blocked["impact"]["affected"] == 2, blocked

        code, _, raw = call(alice, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "delete", "name": "近景", "confirm": True})
        assert code == 200, body(raw)
        deleted = body(raw)
        assert deleted["becomes_untagged"] >= 1, deleted
        revision = deleted["revision"]
        assert "近景" not in deleted["library"]["tags"]

        # 003 只有「近景」一个标签 → 现在应该变未打标、退出候选
        snap = body(call(alice, base, "/api/tags/snapshot")[2])
        row = {video["id"]: video for video in snap["videos"]}["003"]
        assert row["tags"] == [], row
        assert snap["stats"]["untagged"] >= 1, snap["stats"]

        # ---------- 合并级联 ----------
        code, _, raw = call(bob, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "merge", "source": "酒店",
                             "target": "适合秋冬", "confirm": True})
        assert code == 200, body(raw)
        merged = body(raw)
        assert merged["affected"] >= 1 and "酒店" not in merged["library"]["tags"], merged
        revision = merged["revision"]

        # ---------- 新建 / 改分类 ----------
        code, _, raw = call(bob, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "create", "name": "特写", "category": "景别"})
        assert code == 200, body(raw)
        revision = body(raw)["revision"]
        code, _, raw = call(bob, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "set_category", "name": "特写", "category": "姿势"})
        assert code == 200, body(raw)
        assert body(raw)["library"]["categories"]["特写"] == "姿势"

        # ---------- 自定义大类 ----------
        revision = body(call(alice, base, "/api/tags/snapshot")[2])["revision"]
        code, _, raw = call(bob, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "category_create", "name": "天气"})
        assert code == 200, body(raw)
        assert "天气" in body(raw)["library"]["category_order"], body(raw)
        revision = body(raw)["revision"]

        code, _, raw = call(bob, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "create", "name": "下雨", "category": "天气"})
        assert code == 200, body(raw)
        revision = body(raw)["revision"]

        code, _, raw = call(bob, base, "/api/tags/videos/tags", "POST",
                            {"revision": revision, "ids": ["001"], "tags": ["下雨"], "mode": "add"})
        assert code == 200, body(raw)
        snap = body(call(alice, base, "/api/tags/snapshot")[2])
        assert "天气" in snap["categories"], snap["categories"]
        assert {v["id"]: v for v in snap["videos"]}["001"]["tag_categories"]["下雨"] == "天气"

        # 大类改名后，标签跟着走
        revision = snap["revision"]
        code, _, raw = call(bob, base, "/api/tags/library", "POST",
                            {"revision": revision, "action": "category_rename",
                             "name": "天气", "new_name": "天气状况"})
        assert code == 200, body(raw)
        assert body(raw)["library"]["categories"]["下雨"] == "天气状况", body(raw)

        # ---------- 权限：普通用户能写标签，但 admin 接口仍 403 ----------
        code, _, _ = call(bob, base, "/api/admin/template-health")
        assert code == 403, code

        # ---------- Origin 校验 ----------
        code, _, _ = call(bob, base, "/api/tags/videos/tags", "POST",
                          {"revision": revision, "ids": ["002"], "tags": ["酒店"], "mode": "replace"},
                          headers={"Origin": "http://evil.example"})
        assert code == 403, code

        # ---------- bootstrap 字段没退化 ----------
        boot = body(call(alice, base, "/api/bootstrap")[2])["service"]
        for field in ("templates_count", "source_ready_templates", "untagged_skipped"):
            assert isinstance(boot.get(field), int), (field, boot)

        # ---------- 桌面端共用同一份文件：外部改写后快照自动跟上 ----------
        external = TemplateStore.load(store_path)
        external.set_tags(["002"], ["中远景"], mode="replace", user="desktop")
        snap = body(call(alice, base, f"/api/tags/snapshot?rev={external.revision - 1}")[2])
        assert snap.get("unchanged") is not True, "桌面端写过之后必须返回新快照"
        assert snap["revision"] == external.revision
        assert {video["id"]: video for video in snap["videos"]}["002"]["tags"] == ["中远景"]

        # ---------- 版本：整库快照、手动打版、回滚 ----------
        code, _, raw = call(alice, base, "/api/tags/versions?limit=100")
        assert code == 200, body(raw)
        listed = body(raw)
        assert listed["versions"], "导入后就应该有自动版本"
        assert listed["versions"][0]["kind"] == "auto"
        revision = body(call(alice, base, "/api/tags/snapshot")[2])["revision"]

        code, _, raw = call(alice, base, "/api/tags/versions", "POST",
                            {"note": "接口测试版本"})
        assert code == 200, body(raw)
        manual = body(raw)["version"]
        assert manual["kind"] == "manual" and manual["note"] == "接口测试版本"

        code, _, raw = call(alice, base, "/api/tags/videos/tags", "POST",
                            {"revision": revision, "ids": ["002"], "tags": ["特写"], "mode": "replace"})
        assert code == 200, body(raw)
        assert {v["id"]: v for v in body(call(alice, base, "/api/tags/snapshot")[2])["videos"]}["002"]["tags"] == ["特写"]

        revision = body(call(alice, base, "/api/tags/snapshot")[2])["revision"]
        code, _, raw = call(alice, base, "/api/tags/versions/restore", "POST",
                            {"name": manual["name"], "revision": revision})
        assert code == 200, body(raw)
        restored = body(raw)
        assert restored["restored"] == 4 and restored["backup"], restored
        snap = body(call(alice, base, "/api/tags/snapshot")[2])
        assert {v["id"]: v for v in snap["videos"]}["002"]["tags"] == ["中远景"], "回滚后标签要回到版本里的值"

        names = {item["name"] for item in body(call(alice, base, "/api/tags/versions")[2])["versions"]}
        assert restored["backup"] in names and manual["name"] in names

        code, _, raw = call(alice, base, "/api/tags/versions/restore", "POST",
                            {"name": "v999999-1-auto-abcd.json.gz"})
        assert code == 404, (code, body(raw))
        code, _, raw = call(alice, base, "/api/tags/versions/restore", "POST",
                            {"name": "../../etc/passwd"})
        assert code == 400, (code, body(raw))
    finally:
        server.shutdown()
        server.server_close()

    print("OK 标签页接口：普通用户可写、revision 409、弃用可恢复、标签库确认、Origin 与权限不越界")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
