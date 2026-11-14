#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次永久档案：落盘、幂等、ZIP、删除保留与删除闸门的离线测试。

不联网、不起真实云端、不产生费用。直接构造上传图与批次，验证：
  1. 建批次即落档案（款号目录 / meta / prompt / 图片副本 / checksums）；
  2. 追加、删除细节图后归档同步，旧图保留并标 removed_at；
  3. 改提示词写 prompt-history，多轮不丢历史；
  4. 档案 ZIP 含 meta/prompt/图片/checksums，HTTP 路由可 probe 再下载；
  5. 删除批次只删批次 JSON，档案与图片永久保留（deleted.json 墓碑）；
  6. 档案写失败时删除被拒绝（批次不被删掉）。
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.web.auth import make_password  # noqa: E402
from app.web.batches import BatchError  # noqa: E402
from app.web.server import WebApplication, make_handler  # noqa: E402
from app.web.settings import load_settings  # noqa: E402


def make_settings(tmp: Path):
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    web_file = tmp / "web.json"
    web_file.write_text(json.dumps({
        "session_secret": secret, "session_hours": 2,
        "data_root": str(tmp / "web-data"),
        "max_active_jobs": 4, "max_active_per_user": 2, "max_job_items": 20,
        "users": {
            "alice": {**make_password("alice-pass-123"), "role": "user", "enabled": True, "version": 1},
            "bob": {**make_password("bob-pass-12345"), "role": "user", "enabled": True, "version": 1},
            "admin": {**make_password("admin-pass-123"), "role": "admin", "enabled": True, "version": 1},
        },
    }), encoding="utf-8")
    api_file = tmp / "config.json"
    api_file.write_text(json.dumps({
        "wan": {"provider": "dashscope", "api_key": "mock-api-key-123456789",
                "model": "wan3.0-video", "base_url": "http://127.0.0.1:9/api/v1",
                "default_mode": "edit", "poll_interval": 0.2, "poll_timeout": 30,
                "max_concurrency": 4, "query_qps": 50,
                "price_per_second": {"480P": None, "720P": None, "1080P": None}},
        "manifest_url": "", "cos": {"enabled": False},
    }), encoding="utf-8")
    return load_settings(base_dir=tmp, web_file=web_file, api_file=api_file, load_templates=False)


def add_upload(settings, owner: str, upload_id: str, name: str, color) -> Path:
    uploads = Path(settings.data_dir) / "uploads"
    folder = uploads / upload_id[:2]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{upload_id}.png"
    Image.new("RGB", (48, 48), color).save(path)
    meta_dir = uploads / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {"id": upload_id, "owner": owner, "path": str(path), "name": name,
            "created_at": time.time(), "info": {"width": 48, "height": 48}}
    (meta_dir / f"{upload_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return path


def call(opener, base, path, method="GET", payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def expect_error(fn, status, what):
    try:
        fn()
    except BatchError as exc:
        assert exc.status == status, (what, exc.status, exc)
    else:
        raise AssertionError(f"{what}：应当抛出 {status}")


def main() -> int:
    tmp = tempdir("batcharchive-")
    settings = make_settings(tmp)
    app = WebApplication(settings)
    batches = app.batches
    data_dir = Path(settings.data_dir)
    alice = {"username": "alice", "role": "user"}
    bob = {"username": "bob", "role": "user"}

    # 测试里只需要 p01 这一条提示词模板；load_templates=False 时默认库是空的。
    app.jobs.prompts["p01"] = "第一版提示词"

    first_id = "a" * 32
    second_id = "b" * 32
    first_path = add_upload(settings, "alice", first_id, "定妆图.png", (210, 30, 30))
    add_upload(settings, "alice", second_id, "细节图.png", (30, 30, 210))

    try:
        batch = batches.create(alice, {
            "batch_no": "ARCH-001", "title": "档案测试",
            "prompt_id": "p01", "prompt": "第一版提示词",
            "resolution": "720P", "duration": -1,
            "tags": {"top": "LL-ARCH-1"},
            "upload_ids": [first_id],
        })
        batch_id = batch["id"]
        folder = data_dir / "archive" / "LL-ARCH-1" / f"ARCH-001__{batch_id}"
        assert folder.is_dir(), folder
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        assert meta["style"] == "LL-ARCH-1", meta
        assert meta["batch_id"] == batch_id and meta["batch_no"] == "ARCH-001", meta
        assert (folder / "prompt.txt").read_text(encoding="utf-8").strip() == "第一版提示词"
        assert (folder / "images" / f"{first_id}.png").read_bytes() == first_path.read_bytes()
        assert (folder / "checksums.sha256").is_file()
        assert (folder / "archive说明.txt").is_file()
        print("  ✓ 建批次即落档：款号目录 / meta / prompt / 图片副本 / checksums 齐全")

        batches.add_images(batch_id, alice, {"upload_ids": [second_id]})
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        assert len(meta["current_images"]) == 2, meta["current_images"]
        batches.remove_images(batch_id, alice, {"index": 2})
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        assert len(meta["current_images"]) == 1, meta["current_images"]
        removed = [item for item in meta["all_images"] if item.get("removed_at")]
        assert len(removed) == 1 and removed[0]["upload_id"] == second_id, removed
        assert (folder / "images" / f"{second_id}.png").is_file(), "删掉的图也要留在档案里"
        print("  ✓ 图片变更：当前图收敛，删掉的旧图保留并标 removed_at")

        batches.update_prompt(batch_id, alice, {"prompt": "第二版提示词"})
        prompt_lines = (folder / "prompt-history.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        assert len(prompt_lines) == 2, prompt_lines
        assert "第二版提示词" in (folder / "prompt.txt").read_text(encoding="utf-8")
        print("  ✓ 提示词历史：两版都留在 prompt-history.jsonl，prompt.txt 为当前版")

        info = batches.archive_info(batch_id, alice)
        assert info["ready"] is True and info["image_count"] == 1, info
        assert info["size_bytes"] > 0, info

        result = batches.archive_zip(batch_id, alice)
        try:
            with zipfile.ZipFile(result["path"]) as zf:
                names = zf.namelist()
                assert any(name.endswith("/meta.json") for name in names), names
                assert any(name.endswith("/prompt.txt") for name in names), names
                assert any(name.endswith("/checksums.sha256") for name in names), names
                assert any(name.endswith(f"/images/{first_id}.png") for name in names), names
                assert any(name.endswith("/runs/") is False for name in names)
        finally:
            result["path"].unlink(missing_ok=True)
        print("  ✓ 档案 ZIP：meta / prompt / 图片 / checksums 都在包内")

        # HTTP：probe 只回报数量，真实 GET 返回 zip。
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            assert call(opener, base, "/api/login", "POST",
                        {"username": "alice", "password": "alice-pass-123"})[0] == 200
            code, headers, raw = call(
                opener, base, f"/api/batches/{batch_id}/archive.zip?probe=1")
            probe = json.loads(raw.decode("utf-8"))
            assert code == 200 and probe["count"] == 1, (code, probe)
            assert probe["download_url"].endswith(f"/api/batches/{batch_id}/archive.zip")
            assert "json" in headers.get("Content-Type", ""), headers
            code, headers, raw = call(opener, base, f"/api/batches/{batch_id}/archive.zip")
            assert code == 200 and raw[:2] == b"PK", (code, raw[:20])
            assert "zip" in headers.get("Content-Type", ""), headers
            bob_op = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            assert call(bob_op, base, "/api/login", "POST",
                        {"username": "bob", "password": "bob-pass-12345"})[0] == 200
            code, _, _ = call(bob_op, base, f"/api/batches/{batch_id}/archive.zip?probe=1")
            assert code == 403, code
            print("  ✓ HTTP：probe 数量正确，GET 返回 zip，跨账户 403")
        finally:
            server.shutdown()
            server.server_close()

        # 删除：只删批次 JSON，档案与图片不动，墓碑留下。
        batches.delete(batch_id, alice)
        assert not (data_dir / "batches" / f"{batch_id}.json").exists()
        assert folder.is_dir(), "档案目录不能随批次删除"
        assert (folder / "deleted.json").is_file()
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        assert meta["deleted"] is True and meta["deleted_by"] == "alice", meta
        assert (folder / "images" / f"{first_id}.png").is_file(), "档案图片必须保留"
        print("  ✓ 删除批次：批次 JSON 消失，档案、图片与 deleted.json 永久保留")

        # 档案写失败时删除必须被拒绝：批次仍在，避免「批次没了、档案也没写全」。
        batch2 = batches.create(alice, {
            "batch_no": "ARCH-002", "prompt_id": "p01", "prompt": "待删除",
            "resolution": "720P", "duration": -1, "tags": {"top": "LL-ARCH-2"},
            "upload_ids": [first_id],
        })
        original = batches.archive.ensure

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        batches.archive.ensure = boom
        try:
            expect_error(lambda: batches.delete(batch2["id"], alice), 500, "档案写失败时删除")
        finally:
            batches.archive.ensure = original
        assert batch2["id"] in batches.batches, "删除失败后批次必须还在"
        assert (data_dir / "batches" / f"{batch2['id']}.json").is_file()
        print("  ✓ 删除闸门：档案写失败返回 500，批次不丢")

        # 老批次没有档案时 probe 仍按已绑定图报数，下载时才补档。
        old_id = "c" * 32
        add_upload(settings, "alice", old_id, "老图.png", (90, 90, 90))
        old_folder = "oldbatch"
        batches.batches[old_folder] = {
            "id": old_folder, "batch_no": "ARCH-OLD", "owner": "alice", "state": "locked",
            "tags": {"top": "LL-ARCH-OLD"}, "style_no": "", "prompt_id": "p01",
            "prompt": "老提示词", "resolution": "720P", "duration": -1,
            "images": [{"upload_id": old_id, "name": "老图.png",
                        "path": str(Path(settings.data_dir) / "uploads" / old_id[:2] / f"{old_id}.png")}],
            "image_upload_id": old_id, "image_name": "老图.png",
            "image_path": str(Path(settings.data_dir) / "uploads" / old_id[:2] / f"{old_id}.png"),
            "items": [], "runs": [], "active_job_id": "",
            "created_at": time.time(), "updated_at": time.time(),
        }
        batches._save(batches.batches[old_folder])
        old_info = batches.archive_info(old_folder, alice)
        assert old_info["ready"] is False and old_info["image_count"] == 1, old_info
        zip_info = batches.archive_zip(old_folder, alice)
        try:
            assert zip_info["count"] >= 4, zip_info["count"]
        finally:
            zip_info["path"].unlink(missing_ok=True)
        print("  ✓ 老批次：probe 按已绑定图报数，首次下载自动补档")

        print("\n批次永久档案测试全部通过")
        return 0
    finally:
        app.jobs.stop_event.set()


if __name__ == "__main__":
    raise SystemExit(main())
