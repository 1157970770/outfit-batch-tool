#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多图（1 定妆 + 9 细节）端到端：HTTP 契约、引擎 wire、批次追加/删除、兼容回退。"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from _tmpdir import tempdir  # noqa: E402
from PIL import Image  # noqa: E402
from test_batches import (MOCK, body, build_manifest, build_rows, build_settings,  # noqa: E402
                          call, free_port, start_server, wait_job, write_workbook)
from app import media  # noqa: E402
from app.web.server import WebApplication  # noqa: E402


def png_bytes(size=(256, 320), color=(230, 220, 210)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def upload(opener, base, name="look.png", color=(230, 220, 210)) -> str:
    raw = png_bytes(color=color)
    code, _, raw_resp = call(opener, base, "/api/uploads", "POST", {
        "filename": name,
        "data": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    })
    assert code == 201, (code, raw_resp)
    return body(raw_resp)["upload_id"]


PROMPT_ID = "p01"


def job_payload(ids, **extra):
    # 固定挑 001：random 模式下 catalog.choose() 会连同未上云的 051 一起参与抽样，
    # 抽中 051 就会在建任务前 400——那是“该报错”，但会让多图测试变成 1/51 概率失败。
    payload = {"upload_ids": ids, "upload_id": ids[0] if ids else "",
               "selection": "selected", "template_id": "001", "count": 1, "confirmed": True,
               "prompt_id": PROMPT_ID, "prompt": "编辑视频1：把服装换成图1这套，细节参考图2。",
               "resolution": "720P", "duration": 7, "tags": {"top": "TEST-TOP"}}
    payload.update(extra)
    return payload


def batch_payload(ids, batch_no="AW26-MULTI", **extra):
    payload = {"batch_no": batch_no, "prompt_id": PROMPT_ID, "prompt": "图1定妆，图2细节。",
               "resolution": "720P", "duration": 7, "tags": {"top": "TEST-TOP"},
               "upload_ids": ids, "upload_id": ids[0] if ids else ""}
    payload.update(extra)
    return payload


def stats(mock_base) -> dict:
    with urllib.request.urlopen(f"{mock_base}/stats", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def media_images(mock_base) -> list[str]:
    wire = stats(mock_base).get("last_body") or {}
    return [item.get("url") for item in (wire.get("input") or {}).get("media") or []
            if item.get("type") == "reference_image"]


def main() -> int:
    global PROMPT_ID
    tmp = tempdir("multiimage-")
    mock_port = free_port()
    mock_base = f"http://127.0.0.1:{mock_port}"
    env = os.environ.copy()
    env["MOCK_RUNNING_POLLS"] = "2"
    mock = subprocess.Popen([sys.executable, str(MOCK), str(mock_port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    server = app = None
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", mock_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        workbook = tmp / "标签清单.xlsx"
        write_workbook(workbook, build_rows())
        manifest = build_manifest(tmp, mock_base)
        settings = build_settings(tmp, manifest, workbook, mock_base)
        assert not settings.templates_error, settings.templates_error
        app = WebApplication(settings)
        server, base = start_server(app)
        alice = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        bob = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        for opener, name, password in ((alice, "alice", "alice-pass-123"),
                                       (bob, "bob", "bob-pass-12345")):
            code, _, raw = call(opener, base, "/api/login", "POST", {"username": name, "password": password})
            assert code == 200, (name, code, raw)
        code, _, boot = call(alice, base, "/api/bootstrap")
        assert code == 200 and body(boot)["service"]["max_images"] == 10, body(boot)["service"]
        PROMPT_ID = body(boot)["prompts"][0]["id"]

        # 1. 10 张全链路：顺序 = 槽位 = imageUrls
        ids = [upload(alice, base, f"look{i}.png", (200 + i, 210, 220)) for i in range(10)]
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload(ids))
        assert code == 201, (code, raw)
        job = body(raw)["job"]
        assert job["image_urls"] == [f"/api/uploads/{uid}" for uid in ids], job["image_urls"]
        assert len(job["image_names"]) == 10, job
        for uid in ids:
            code, _, _ = call(alice, base, f"/api/uploads/{uid}")
            assert code == 200, uid
        job_file = Path(settings.data_dir) / "jobs" / f'{job["id"]}.json'
        stored = json.loads(job_file.read_text(encoding="utf-8"))
        assert len(stored["images"]) == 10 and stored["image_path"] == stored["images"][0]["path"], stored
        assert len(app.jobs.public(app.jobs.jobs[job["id"]])["image_urls"]) == 10, "重启回读口径"
        wait_job(alice, base, job["id"])
        assert len(media_images(mock_base)) == 10, "10 张应展开 10 条 reference_image"
        print("  ✓ 10 张全链路：落盘 / public / 回读 / wire 顺序一致")

        # 2. 张数与归属校验
        too_many = ids + [upload(alice, base, "eleventh.png")]
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload(too_many))
        assert code == 400 and "最多 10 张" in body(raw)["error"], (code, raw)
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload([]))
        assert code == 400, (code, raw)
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload([ids[0], ids[0]]))
        assert code == 400 and "重复" in body(raw)["error"], (code, raw)
        foreign = upload(bob, base, "bob.png")
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload([ids[0], foreign]))
        assert code == 404 and "第 2 张" in body(raw)["error"], (code, raw)
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload(["zzzz"]))
        assert code == 404, (code, raw)
        print("  ✓ 第 11 张 400 / 0 张 400 / 重复 400 / 他人图 404 / 非法 id 404")

        # 3. 单张超 20MB（运行时把上限调到 0.001MB，等价触发 413）
        app.settings.web["max_upload_mb"] = 0.0001
        code, _, raw = call(alice, base, "/api/uploads", "POST", {
            "filename": "big.png",
            "data": "data:image/png;base64," + base64.b64encode(png_bytes()).decode("ascii"),
        })
        assert code == 413, (code, raw)
        app.settings.web["max_upload_mb"] = 20
        print("  ✓ 单张超限 413（服务端口径不变）")

        # 4. 空槽不进 imageUrls：只传 2 张 → wire 恰好 2 张参考图
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload(ids[:2]))
        assert code == 201, (code, raw)
        wait_job(alice, base, body(raw)["job"]["id"])
        urls = media_images(mock_base)
        assert len(urls) == 2 and urls[0] != urls[1], urls
        print("  ✓ 空槽不占位：2 张图 → 2 条 reference_image")

        # 5. Base64 总量闸（COS 关闭时，把阈值临时调小即可覆盖同一条分支）
        original_budget = media.MAX_BASE64_TOTAL_MB
        media.MAX_BASE64_TOTAL_MB = 0.001
        try:
            code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload(ids[:3]))
            assert code == 400 and "COS" in body(raw)["error"], (code, raw)
        finally:
            media.MAX_BASE64_TOTAL_MB = original_budget
        print("  ✓ 未启用 COS 时多图总量超预算 → 建任务前 400")

        # 6. 重试保留全部图
        code, _, raw = call(alice, base, "/api/jobs", "POST", job_payload(ids[:3]))
        assert code == 201, (code, raw)
        source = body(raw)["job"]
        wait_job(alice, base, source["id"])
        stored_job = app.jobs.jobs[source["id"]]
        stored_job["status"] = "partial"
        for rec in stored_job["records"]:
            rec["status"] = "failed"
        code, _, retry_raw = call(alice, base, f'/api/jobs/{source["id"]}/retry', "POST", {})
        assert code == 201, (code, retry_raw)
        retried = body(retry_raw)["job"]
        assert retried["image_urls"] == source["image_urls"], (retried["image_urls"], source["image_urls"])
        wait_job(alice, base, retried["id"])       # 排空，免得它异步提交干扰后面的计数断言
        print("  ✓ 重试：3 张图全量保留、顺序不变")

        # 7. 老 job（只有 image_path）零迁移回退
        legacy = {"id": "legacy0000001", "owner": "alice", "status": "failed",
                  "image_path": str(Path(settings.data_dir) / "uploads" / ids[0][:2] / f"{ids[0]}.png"),
                  "image_name": "old.png", "records": []}
        public = app.jobs.public(legacy)
        assert public["image_urls"] == [f"/api/uploads/{ids[0]}"], public["image_urls"]
        print("  ✓ 老 job 无 images 字段 → 反解 upload_id，public 仍给 1 张主图")

        # 8. 批次多图 + 锁定后追加/删除
        code, _, raw = call(alice, base, "/api/batches", "POST", batch_payload(ids[:3]))
        assert code == 201, (code, raw)
        batch = body(raw)["batch"]
        assert len(batch["image_urls"]) == 3 and batch["image_url"] == batch["image_urls"][0], batch
        code, _, cands = call(alice, base, f'/api/batches/{batch["id"]}/candidates', "POST",
                              {"tag_filters": {}, "limit": 1000})
        assert code == 200, (code, cands)
        ready = [item["id"] for item in body(cands)["items"] if item["ready"]][:3]
        assert len(ready) == 3
        code, _, raw = call(alice, base, f'/api/batches/{batch["id"]}/lock', "POST",
                            {"template_ids": ready, "confirmed": True})
        assert code == 200, (code, raw)
        code, _, raw = call(alice, base, f'/api/batches/{batch["id"]}/images', "POST",
                            {"op": "add", "upload_ids": ids[3:6]})
        assert code == 200 and len(body(raw)["batch"]["image_urls"]) == 6, (code, raw)
        code, _, raw = call(alice, base, f'/api/batches/{batch["id"]}/images', "POST",
                            {"op": "add", "upload_ids": ids[6:10] + [too_many[10]]})
        assert code == 400 and "最多" in body(raw)["error"], (code, raw)
        code, _, raw = call(alice, base, f'/api/batches/{batch["id"]}/images', "POST",
                            {"op": "remove", "index": 1})
        assert code == 400 and "定妆图" in body(raw)["error"], (code, raw)
        code, _, raw = call(alice, base, f'/api/batches/{batch["id"]}/images', "POST",
                            {"op": "remove", "index": 3})
        assert code == 200, (code, raw)
        after = body(raw)["batch"]["image_urls"]
        assert after == [f"/api/uploads/{uid}" for uid in (ids[0], ids[1], ids[3], ids[4], ids[5])], after
        print("  ✓ 批次多图：锁定后追加/越界 400/主图不可删/删除后左移顺序正确")

        # 9. 生产时逐张校验；删掉第 3 张 → 404 且一条都不提交
        submitted_before = (stats(mock_base).get("submitted") or 0)
        third = json.loads((Path(settings.data_dir) / "batches" / f'{batch["id"]}.json')
                           .read_text(encoding="utf-8"))["images"][2]["path"]
        os.remove(third)
        code, _, raw = call(alice, base, f'/api/batches/{batch["id"]}/produce', "POST", {"count": 5})
        assert code == 404 and "第 3 张" in body(raw)["error"], (code, raw)
        assert (stats(mock_base).get("submitted") or 0) == submitted_before, "缺图时不能提交任何任务"
        print("  ✓ 第 3 张图丢失 → 404，整批不提交（不产生费用）")

        print("OK 多图：契约 / 引擎 / 重试 / 老数据回退 / 批次增删 / 缺图整批拒绝 全部通过")
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if app is not None:
            app.jobs.shutdown()
        if mock.poll() is None:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()


if __name__ == "__main__":
    raise SystemExit(main())
