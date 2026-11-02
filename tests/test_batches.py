#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务批次端到端测试：本地 mock 百炼，不产生真实费用。

覆盖（对应 batch-spec.md v1 + v2 修订）：
1. 创建批次 → 绑定定妆图 → 拉候选 → 锁定清单 → 分两批生产（20 + 30），
   同一批次内不出现重复模板。
2. 并发点两次 /produce：后到者被 active_job_id 拒绝（409）。
3. 未打标的模板不进候选；锁定里含未上云模板 → 400。
4. 历史按批次号分组：有 batch_ref 的按批次聚合，老/试跑 job 落 __ungrouped__。
5. 服务重启后批次状态能正确加载，并在首次读取时对账（producing 收敛）。
6. 删除批次：owner/admin 权限、404、运行中 409、locked 可删、只删批次 JSON
   （output/jobs 一个文件不动）、删除后 job 落回 __ungrouped__。
7. 锁定后修改提示词：空/超长 400、非 owner 403、owner 200；第二批生产
   的 job 带新文案；关闭后 409。
另附：并发配置不再被钳到 3、进程级限流为单例、单价 null 时预估价走兜底。
"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# 测试里 50 条任务要快速跑完：放开限速（限速本身的算法在下面单独断言）。
os.environ.setdefault("WAN_QUERY_RPM", "6000")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from http.server import ThreadingHTTPServer  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from PIL import Image  # noqa: E402

from app.wan_api import (WanClient, WanConfig, _RateLimiter, global_inflight, global_limiter,  # noqa: E402
                         global_query_limiter, global_small_inflight)
from app.web.auth import make_password  # noqa: E402
from app.web.jobs import JobManager  # noqa: E402
from app.web.server import WebApplication, make_handler  # noqa: E402
from app.web.settings import load_settings  # noqa: E402

MOCK = ROOT / "tests" / "mock_dashscope.py"
TAG_DICT = [("近景", "景别"), ("中远景", "景别"), ("适合秋冬", "适合季节"), ("适合春夏", "适合季节")]
READY_COUNT = 50           # 已上传云端、可生产的模板数
TEMPLATE_SECONDS = 1.5     # 工作簿里的模板时长（秒）→ 单条计费 = 1.5 + 7
UNIT_PRICE = 0.6           # config 里 price_per_second 为 null 时的 720P 兜底单价


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(opener, base, path, method="GET", payload=None, headers=None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=raw, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def body(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def wait_batch(opener, base, batch_id, predicate, timeout=120.0, label=""):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        code, _, raw = call(opener, base, f"/api/batches/{batch_id}")
        assert code == 200, (code, raw)
        last = body(raw)["batch"]
        if predicate(last):
            return last
        time.sleep(0.2)
    raise AssertionError(f"等待批次状态超时：{label} {last}")


def wait_job(opener, base, job_id, timeout=120.0):
    deadline = time.time() + timeout
    job = {}
    while time.time() < deadline:
        code, _, raw = call(opener, base, f"/api/jobs/{job_id}")
        assert code == 200, (code, raw)
        job = body(raw)["job"]
        if job.get("status") in {"succeeded", "partial", "failed", "canceled"}:
            return job
        time.sleep(0.2)
    raise AssertionError(f"等待任务结束超时：{job}")


# ---------------------------------------------------------------- 测试夹具
def write_workbook(path: Path, rows) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "视频标签清单"
    ws.append(["模板ID", "相对路径", "文件名", "时长(秒)", "大小(MB)", "文件指纹", "标签", "状态"])
    for row in rows:
        ws.append(row)
    tags = wb.create_sheet("标签字典")
    tags.append(["标签值", "分类"])
    for tag, category in TAG_DICT:
        tags.append([tag, category])
    wb.save(path)
    wb.close()


def build_rows() -> list[list]:
    rows = []
    for index in range(1, READY_COUNT + 2):          # 001–051，其中 051 未上传云端
        template_id = f"{index:03d}"
        name = f"{index:04d}.mp4"
        rows.append([template_id, f"input/tpl/{name}", name, TEMPLATE_SECONDS, 12.0,
                     f"fp{template_id}", "近景;适合秋冬", "已上传" if index <= READY_COUNT else "待上传"])
    # 没打标签 = 废弃素材：不进候选集
    rows.append(["052", "input/tpl/0052.mp4", "0052.mp4", 1.5, 12.0, "fp052", "", "待上传"])
    rows.append(["053", "input/tpl/0053.mp4", "0053.mp4", 1.5, 12.0, "fp053", "", "待上传"])
    return rows


def build_manifest(tmp: Path, manifest_base: str) -> Path:
    entries = []
    for index in range(1, READY_COUNT + 1):
        name = f"{index:04d}.mp4"
        entries.append({"id": f"{index:03d}", "filename": name, "relative_path": f"input/tpl/{name}",
                        "size_mb": 12.0, "duration": TEMPLATE_SECONDS,
                        "url": f"{manifest_base}/file.mp4"})
    path = tmp / "manifest.json"
    path.write_text(json.dumps({"version": 1, "count": len(entries), "templates": entries}), encoding="utf-8")
    return path


def build_settings(tmp: Path, manifest: Path, workbook: Path, mock_base: str):
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    web_file = tmp / "web.json"
    web_file.write_text(json.dumps({
        "session_secret": secret, "session_hours": 2, "data_root": str(tmp / "web-data"),
        "max_active_jobs": 2, "max_active_per_user": 1, "max_job_items": 20,
        "max_batch_items": 500, "max_batch_run_items": 100, "confirm_over_items": 5,
        "tag_workbook": str(workbook),
        "users": {
            "alice": {**make_password("alice-pass-123"), "role": "user", "enabled": True, "version": 1},
            "bob": {**make_password("bob-pass-12345"), "role": "user", "enabled": True, "version": 1},
            "admin": {**make_password("admin-pass-123"), "role": "admin", "enabled": True, "version": 1},
        },
    }), encoding="utf-8")
    api_file = tmp / "config.json"
    api_file.write_text(json.dumps({
        "wan": {"provider": "dashscope", "api_key": "mock-api-key-123456789", "model": "wan3.0-video",
                "base_url": f"{mock_base}/api/v1", "default_mode": "edit",
                "poll_interval": 1, "poll_timeout": 60, "max_concurrency": 20, "query_qps": 10,
                # 单价留 null：验证代码里的兜底，而不是硬编码在请求处理里
                "price_per_second": {"480P": None, "720P": None, "1080P": None}},
        "manifest_url": str(manifest), "cos": {"enabled": False},
    }), encoding="utf-8")
    return load_settings(base_dir=tmp, web_file=web_file, api_file=api_file, load_templates=True)


def reload_settings(tmp: Path):
    """模拟重启：重新从磁盘读配置与清单。"""
    return load_settings(base_dir=tmp, web_file=tmp / "web.json",
                         api_file=tmp / "config.json", load_templates=True)


def start_server(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> int:
    # ---------------- 0. 限流/在飞单例（并发改造的核心事实）
    limiter = _RateLimiter(rpm=250)
    assert abs(limiter.interval - 60.0 / 250) < 1e-9, limiter.interval
    assert global_limiter() is global_limiter(), "请求速率桶必须是进程级单例"
    assert global_inflight() is global_inflight(), "在飞额度必须是进程级单例"
    cfg = WanConfig(api_key="mock-key-123456", base_url="http://127.0.0.1:1/api/v1", model="wan3.0-video")
    assert WanClient(cfg).limiter is WanClient(cfg).limiter, "所有 job 共用同一个提交桶"
    assert WanClient(cfg).query_limiter is WanClient(cfg).query_limiter, "所有 job 共用同一个轮询桶"
    assert WanClient(cfg).query_limiter is not WanClient(cfg).limiter, "提交与轮询不得共桶"
    assert WanClient(cfg).inflight is WanClient(cfg).inflight, "所有 job 共用同一份在飞额度"
    assert global_small_inflight() is global_small_inflight(), "快速通道在飞池必须稳定复用"
    assert global_small_inflight() is not global_inflight(), "快速通道必须与大批量池隔离"
    # 创建任务与轮询是两个额度：RPM 桶只管提交，RPS 桶只管查询
    assert global_query_limiter() is global_query_limiter(), "轮询桶必须稳定复用"
    assert global_query_limiter() is not global_limiter(), "轮询桶不得与创建任务共桶"
    assert abs(global_query_limiter().interval - 1 / 15.0) < 1e-9, global_query_limiter().interval
    print("  ✓ 创建任务与轮询分桶；快速通道另有独立在飞池")

    # 快速通道的两个独立计数：小批量不占大批量的 job 槽位，也不占每用户大批量额度
    assert JobManager._lane_allowed("small", "alice", {"alice": 1}, {}, 1, 0, 4, 4, 1) is True
    assert JobManager._lane_allowed("small", "alice", {}, {"alice": 1}, 0, 1, 4, 4, 1) is False
    assert JobManager._lane_allowed("small", "bob", {}, {}, 4, 4, 4, 4, 1) is False
    assert JobManager._lane_allowed("big", "bob", {}, {}, 4, 0, 4, 4, 1) is False
    assert JobManager._lane_allowed("big", "bob", {}, {}, 0, 4, 4, 4, 1) is True
    print("  ✓ 快速通道计数独立：小批量可与本人/他人的大批量并行")

    tmp = tempdir("batchtest-")
    mock_port = free_port()
    mock_base = f"http://127.0.0.1:{mock_port}"
    env = os.environ.copy()
    env["MOCK_RUNNING_POLLS"] = "5"      # 留出「在跑」窗口，用于并发点两次 produce
    mock = subprocess.Popen([sys.executable, str(MOCK), str(mock_port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    # 第二个 mock 永远返回 FAILED：用于验证 retry-failed 的真实失败路径
    failed_base = f"http://127.0.0.1:{free_port()}"
    env_fail = os.environ.copy()
    env_fail["MOCK_TASK_MODE"] = "failed"
    env_fail["MOCK_RUNNING_POLLS"] = "0"
    mock_fail = subprocess.Popen([sys.executable, str(MOCK), failed_base.rsplit(":", 1)[1]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_fail)
    server = app = None
    server2 = app2 = None
    try:
        for base_url in (mock_base, failed_base):
            port = int(base_url.rsplit(":", 1)[1])
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError(f"本地 mock 百炼服务未启动：{base_url}")

        workbook = tmp / "标签清单.xlsx"
        write_workbook(workbook, build_rows())
        manifest = build_manifest(tmp, mock_base)
        settings = build_settings(tmp, manifest, workbook, mock_base)
        assert not settings.templates_error, settings.templates_error
        info = settings.tag_workbook_info
        assert info["workbook_rows"] == READY_COUNT + 1, info        # 052/053 未打标被排除
        assert info["source_ready_templates"] == READY_COUNT, info
        assert info["untagged_skipped"] == 2, info
        # 并发配置不再被钳到 3
        assert settings.max_concurrency == 20, settings.max_concurrency
        assert settings.small_lane_items == 3, settings.small_lane_items
        assert settings.max_active_small_jobs == 4 and settings.small_lane_inflight == 12
        assert settings.max_batch_items == 500 and settings.max_batch_run_items == 100
        # 单价兜底三档齐全（config 全 null 时用公开价目表兜底）
        assert settings.unit_price("480P") == (0.3, "default"), settings.unit_price("480P")
        assert settings.unit_price("720P") == (UNIT_PRICE, "default"), settings.unit_price("720P")
        assert settings.unit_price("1080P") == (1.2, "default"), settings.unit_price("1080P")
        print("  ✓ 单 job 并发读配置到 20（不再 min(3, …)），废弃行排除、上云 50 条")

        app = WebApplication(settings)
        server, base = start_server(app)
        alice = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        bob = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        for opener, name, password in ((alice, "alice", "alice-pass-123"),
                                       (bob, "bob", "bob-pass-12345"),
                                       (admin, "admin", "admin-pass-123")):
            code, _, _ = call(opener, base, "/api/login", "POST", {"username": name, "password": password})
            assert code == 200, (name, code)
        code, _, boot = call(alice, base, "/api/bootstrap")
        assert code == 200, code
        prompt = body(boot)["prompts"][0]
        prompt_id, prompt_text = prompt["id"], prompt["body"]

        image = io.BytesIO()
        Image.new("RGB", (256, 320), (230, 220, 210)).save(image, format="PNG")
        code, _, upload = call(alice, base, "/api/uploads", "POST", {
            "filename": "look.png",
            "data": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")})
        assert code == 201, (code, upload)
        upload_id = body(upload)["upload_id"]

        # ---------------- 1. 创建 / 绑定图 / 候选 / 锁定
        code, _, created = call(alice, base, "/api/batches", "POST", {
            "batch_no": "AW26-001", "prompt_id": prompt_id, "prompt": prompt_text,
            "resolution": "720P", "duration": 7, "title": "秋冬打底"})
        assert code == 201, (code, created)
        batch = body(created)["batch"]
        batch_id = batch["id"]
        assert re.fullmatch(r"[a-f0-9]{12}", batch_id), batch_id
        assert batch["state"] == "draft" and batch["total_items"] == 0, batch
        assert batch["duration"] == 7, batch["duration"]     # 输出固定 7 秒
        code, _, dup = call(alice, base, "/api/batches", "POST", {
            "batch_no": "aw26-001", "prompt_id": prompt_id, "prompt": prompt_text,
            "resolution": "720P", "duration": 7})
        assert code == 409, (code, dup)                       # 批次号忽略大小写唯一
        code, _, _ = call(alice, base, "/api/batches", "POST", {
            "batch_no": "AW26-002", "prompt_id": "nope", "prompt": prompt_text,
            "resolution": "720P", "duration": 7})
        assert code == 400, "无效提示词模板必须 400"
        for bad_duration in (1, 31, 0):
            code, _, _ = call(alice, base, "/api/batches", "POST", {
                "batch_no": f"AW26-BAD{bad_duration}", "prompt_id": prompt_id,
                "prompt": prompt_text, "resolution": "720P", "duration": bad_duration})
            assert code == 400, f"duration={bad_duration} 必须拒绝（只接受 -1 或 2–30）"
        code, _, listed = call(alice, base, "/api/batches")
        assert code == 200 and len(body(listed)["batches"]) == 1, listed
        assert "items" not in body(listed)["batches"][0], "列表不返回全量条目"

        code, _, bound = call(alice, base, f"/api/batches/{batch_id}/image", "POST", {"upload_id": upload_id})
        assert code == 200 and body(bound)["image_name"] == "look.png", (code, bound)
        assert body(bound)["batch"]["image_url"] == f"/api/uploads/{upload_id}"
        code, _, img = call(alice, base, f"/api/uploads/{upload_id}")
        assert code == 200 and img[:4] == b"\x89PNG", "批次定妆图需要能取回缩略图"

        code, _, cands = call(alice, base, f"/api/batches/{batch_id}/candidates", "POST",
                              {"tag_filters": {}, "limit": 1000})
        assert code == 200, (code, cands)
        payload = body(cands)
        ids = [item["id"] for item in payload["items"]]
        assert "052" not in ids and "053" not in ids, "未打标的废弃模板不能进候选"
        assert "051" in ids, ids
        assert payload["filtered_total"] == READY_COUNT + 1, payload["filtered_total"]
        assert payload["ready_count"] == READY_COUNT, payload["ready_count"]
        assert not next(item for item in payload["items"] if item["id"] == "051")["ready"]
        estimate = payload["cost_estimate"]
        assert estimate["unit_price"] == UNIT_PRICE and estimate["price_source"] == "default", estimate
        assert [option["count"] for option in estimate["options"]] == [20, 50, 100], estimate
        assert abs(estimate["amount"] - 51 * (TEMPLATE_SECONDS + 7) * UNIT_PRICE) < 0.05, estimate
        code, _, bad = call(alice, base, f"/api/batches/{batch_id}/candidates", "POST",
                            {"tag_filters": {"景别": ["不存在的标签"]}})
        assert code == 400, (code, bad)
        print("  ✓ 候选只含已打标模板，响应带预估金额（单价 null 走兜底 0.6 元/秒）")

        ready_ids = [item["id"] for item in payload["items"] if item["ready"]]
        assert len(ready_ids) == READY_COUNT
        code, _, locked = call(alice, base, f"/api/batches/{batch_id}/lock", "POST",
                               {"template_ids": ready_ids + ["051"], "confirmed": True})
        assert code == 400, (code, locked)                    # 含未上云模板必须拦下
        assert "云端" in body(locked)["error"], locked
        code, _, locked = call(alice, base, f"/api/batches/{batch_id}/lock", "POST",
                               {"template_ids": ready_ids + ready_ids[:3], "confirmed": True})
        assert code == 200, (code, locked)
        batch = body(locked)["batch"]
        assert batch["state"] == "locked" and batch["total_items"] == READY_COUNT, batch
        assert batch["pending"] == READY_COUNT, batch
        code, _, again = call(alice, base, f"/api/batches/{batch_id}/lock", "POST",
                              {"template_ids": ready_ids, "confirmed": True})
        assert code == 409, (code, again)
        code, _, locked_detail = call(alice, base, f"/api/batches/{batch_id}")
        locked_estimate = body(locked_detail)["batch"]["cost_estimate"]
        assert locked_estimate["count"] == READY_COUNT, locked_estimate
        assert abs(locked_estimate["amount"]
                   - READY_COUNT * (TEMPLATE_SECONDS + 7) * UNIT_PRICE) < 0.05, locked_estimate
        code, _, swap = call(alice, base, f"/api/batches/{batch_id}/image", "POST", {"upload_id": upload_id})
        assert code == 409 and "新开批次" in body(swap)["error"], swap
        code, _, noconfirm = call(alice, base, f"/api/batches/{batch_id}/lock", "POST",
                                  {"template_ids": ready_ids})
        assert code in (400, 409), "缺 confirmed 不能锁定"
        print("  ✓ 锁定校验：未上云 400、重复锁定 409、锁定后换图 409、去重后 50 条")

        # 锁定后仍可改提示词（#14）：生产时才读 batch["prompt"]，改完只影响之后的分批生产
        new_prompt = prompt_text + " 画面里人物的圆领服装换成黑色高领毛衣，其余保持不变。"
        code, _, empty = call(alice, base, f"/api/batches/{batch_id}/prompt", "POST", {"prompt": "   "})
        assert code == 400, (code, empty)
        code, _, long = call(alice, base, f"/api/batches/{batch_id}/prompt", "POST", {"prompt": "长" * 4001})
        assert code == 400, (code, long)
        code, _, forbidden = call(bob, base, f"/api/batches/{batch_id}/prompt", "POST", {"prompt": new_prompt})
        assert code == 403, (code, forbidden)
        code, _, edited = call(alice, base, f"/api/batches/{batch_id}/prompt", "POST", {"prompt": new_prompt})
        assert code == 200 and body(edited)["batch"]["prompt"] == new_prompt, (code, edited)
        print("  ✓ 锁定后改提示词：空/超长 400、非 owner 403、owner 200 且详情返回新文案")

        # 热重载模板不应打断已锁定的批次（#13 / #14）
        code, _, reloaded = call(admin, base, "/api/admin/reload-templates", "POST", {})
        assert code == 200, (code, reloaded)
        reloaded_body = body(reloaded)
        assert reloaded_body["count"] == READY_COUNT + 1, reloaded_body
        assert reloaded_body["source_ready_templates"] == READY_COUNT, reloaded_body
        assert reloaded_body["untagged_skipped"] == 2, reloaded_body
        assert "manifest_unmatched" in reloaded_body and "manifest_warning" in reloaded_body
        code, _, health = call(admin, base, "/api/admin/template-health")
        assert code == 200, (code, health)
        health_body = body(health)
        assert health_body["workbook_rows"] == READY_COUNT + 1, health_body
        assert health_body["source_ready"] == READY_COUNT and health_body["untagged"] == 2, health_body
        assert abs(health_body["ready_ratio"] - READY_COUNT / (READY_COUNT + 1)) < 1e-4, health_body
        print("  ✓ reload-templates/template-health 返回上云条数与未打标数")

        # ---------------- 2. 并发点两次生产：后到者 409
        results: dict[str, tuple] = {}

        def click(tag):
            results[tag] = call(alice, base, f"/api/batches/{batch_id}/produce", "POST", {"count": 20})

        threads = [threading.Thread(target=click, args=(tag,)) for tag in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        codes = sorted(result[0] for result in results.values())
        assert codes == [201, 409], {tag: value[0] for tag, value in results.items()}
        winner = next(value for value in results.values() if value[0] == 201)
        produced = body(winner[2])
        assert produced["taken"] == 20, produced["taken"]
        assert produced["batch"]["active_job_id"] == produced["job"]["id"]
        assert produced["job"]["batch_ref"] == batch_id, produced["job"]
        assert produced["job"]["duration"] == 7, produced["job"]["duration"]   # 输出固定 7 秒
        cost = produced["cost_estimate"]
        assert abs(cost["amount"] - 20 * (TEMPLATE_SECONDS + 7) * UNIT_PRICE) < 0.05, cost
        job_ids = [produced["job"]["id"]]
        print(f"  ✓ 并发两次 /produce：一次 201（取 20 条，duration=7，预估 {cost['amount']} 元），一次 409")

        run1_job = wait_job(alice, base, job_ids[0])
        assert run1_job["status"] == "succeeded", run1_job
        with urllib.request.urlopen(f"{mock_base}/stats", timeout=5) as response:
            mock_state = json.loads(response.read().decode("utf-8"))
        assert mock_state["last_body"]["parameters"]["duration"] == 7, mock_state["last_body"]["parameters"]
        batch = wait_batch(alice, base, batch_id,
                           lambda b: b["active_job_id"] == "" and b["producing"] == 0,
                           label="第一批对账")
        assert batch["done"] == 20 and batch["pending"] == 30 and batch["failed"] == 0, batch
        print("  ✓ 第一批 20 条全部 done，active_job_id 由对账清空")

        # 锁定后改的提示词要真的被后续生产用上：第二批的 job 应带新文案
        code, _, edited2 = call(alice, base, f"/api/batches/{batch_id}/prompt", "POST",
                                {"prompt": new_prompt})
        assert code == 200, (code, edited2)
        code, _, segundo = call(alice, base, f"/api/batches/{batch_id}/produce", "POST", {"count": 50})
        assert code == 201, (code, segundo)
        produced = body(segundo)
        assert produced["taken"] == 30, produced["taken"]     # 剩余不足 50 取全部
        # job 的 public 字段不含 prompt（防止列表泄漏长文案），直接看内部记录
        assert app.jobs.jobs[produced["job"]["id"]]["prompt"] == new_prompt, \
            "第二批生产必须使用改过之后的提示词"
        job_ids.append(produced["job"]["id"])
        run2_job = wait_job(alice, base, job_ids[1])
        assert run2_job["status"] == "succeeded", run2_job
        batch = wait_batch(alice, base, batch_id,
                           lambda b: b["active_job_id"] == "" and b["producing"] == 0,
                           label="第二批对账")
        assert batch["done"] == READY_COUNT and batch["pending"] == 0, batch
        code, _, detail = call(alice, base, f"/api/batches/{batch_id}")
        items = body(detail)["items"]
        template_ids = [item["template_id"] for item in items]
        assert len(template_ids) == len(set(template_ids)) == READY_COUNT, "同批次内不能出现重复模板"
        assert all(item["state"] == "done" for item in items), items
        assigned = [item["job_id"] for item in items]
        assert len([value for value in assigned if value == job_ids[0]]) == 20, assigned
        assert len([value for value in assigned if value == job_ids[1]]) == 30, assigned
        code, _, filtered = call(alice, base, f"/api/batches/{batch_id}?item_state=done")
        assert code == 200 and len(body(filtered)["items"]) == READY_COUNT
        assert all(item["download_url"] for item in body(filtered)["items"]), "done 条目要带 download_url"
        assert all(isinstance(item["verify_warn"], str) for item in body(filtered)["items"]), \
            "每条 done 条目都要有 verify_warn（可能为空串）"
        # 注入一条质量提示，证明 verify.warn 的取数链路真的通（mock 产不出时长异常）
        warn_tid = items[0]["template_id"]
        for rec in app.jobs.jobs[items[0]["job_id"]]["records"]:
            if str(rec.get("id")) == warn_tid:
                rec["verify"] = {"size_kb": 120, "warn": "时长 9.0s 与预期 7s 不符"}
        code, _, patched = call(alice, base, f"/api/batches/{batch_id}")
        patched_items = {row["template_id"]: row for row in body(patched)["items"]}
        assert patched_items[warn_tid]["verify_warn"] == "时长 9.0s 与预期 7s 不符", patched_items[warn_tid]
        assert patched_items[warn_tid]["state"] == "done", "质量提示不能改变条目状态"
        # 详情要能直接给「重开已锁定批次」的金额，不必再拉候选
        detail_estimate = body(filtered)["batch"]["cost_estimate"]
        assert detail_estimate["unit_price"] == UNIT_PRICE, detail_estimate
        assert detail_estimate["count"] == 0, detail_estimate          # 已无待生产
        assert [option["count"] for option in detail_estimate["options"]] == [20, 50, 100]
        assert detail_estimate["options"][0]["amount"] is not None, detail_estimate
        code, _, filtered = call(alice, base, f"/api/batches/{batch_id}?item_state=pending")
        assert code == 200 and body(filtered)["items"] == []
        code, _, invalid = call(alice, base, f"/api/batches/{batch_id}/produce", "POST", {"count": 30})
        assert code == 400, (code, invalid)                   # v2：只接受 20/50/100
        print("  ✓ 两批 20+30 全部 done，50 条模板 ID 无重复")

        # 打包下载
        code, headers, zipped = call(alice, base, f"/api/batches/{batch_id}/files.zip")
        assert code == 200 and headers.get("Content-Type", "").startswith("application/zip"), code
        names = zipfile.ZipFile(io.BytesIO(zipped)).namelist()
        assert len(names) == READY_COUNT, len(names)
        print(f"  ✓ 批次产物打包 {len(names)} 个 mp4")

        # ---------------- 3. 越权
        code, _, _ = call(bob, base, f"/api/batches/{batch_id}")
        assert code == 403, code
        code, _, _ = call(bob, base, f"/api/batches/{batch_id}/produce", "POST", {"count": 20})
        assert code == 403, code
        code, _, _ = call(bob, base, "/api/batches?scope=all")
        assert code == 403, code
        code, _, admin_list = call(admin, base, "/api/batches?scope=all")
        assert code == 200 and len(body(admin_list)["batches"]) == 1
        print("  ✓ 非 owner 403，admin scope=all 可看")

        # ---------------- 4. 历史分组
        code, _, trial = call(alice, base, "/api/jobs", "POST", {
            "upload_id": upload_id, "selection": "selected", "template_id": "001", "count": 1,
            "prompt_id": prompt_id, "prompt": prompt_text, "resolution": "720P", "duration": 5})
        assert code == 201, (code, trial)
        trial_job = body(trial)["job"]
        assert trial_job["batch_ref"] == "", trial_job           # 试跑页没有批次归属
        wait_job(alice, base, trial_job["id"])

        code, _, groups = call(alice, base, "/api/history/groups")
        assert code == 200, (code, groups)
        rows = body(groups)["groups"]
        keyed = {row["key"]: row for row in rows}
        assert batch_id in keyed, rows
        assert keyed[batch_id]["batch_no"] == "AW26-001", keyed[batch_id]
        assert keyed[batch_id]["job_count"] == 2 and keyed[batch_id]["total"] == READY_COUNT, keyed
        assert keyed[batch_id]["success"] == READY_COUNT and keyed[batch_id]["failed"] == 0, keyed
        assert "__ungrouped__" in keyed, rows
        assert keyed["__ungrouped__"]["is_ungrouped"] is True, keyed["__ungrouped__"]
        assert keyed["__ungrouped__"]["job_count"] == 1, keyed["__ungrouped__"]
        assert rows[-1]["key"] == "__ungrouped__", "未分组固定排最后"
        code, _, hist = call(alice, base, f"/api/history/groups/{batch_id}?offset=0&limit=200")
        assert code == 200, (code, hist)
        hist_body = body(hist)
        assert hist_body["total"] == READY_COUNT, hist_body["total"]
        first = hist_body["items"][0]
        assert first["template_name"].endswith(".mp4") and first["download_url"], first
        hist_warn = {row["template_id"]: row["verify_warn"] for row in hist_body["items"]}
        assert hist_warn[warn_tid] == "时长 9.0s 与预期 7s 不符", "历史分组行也要带同口径的 verify_warn"
        code, _, hist = call(alice, base, "/api/history/groups/__ungrouped__")
        assert code == 200 and body(hist)["total"] == 1, hist
        code, _, missing = call(alice, base, "/api/history/groups/ffffffffffff")
        assert code == 404, (code, missing)
        print("  ✓ 历史分组：批次聚 2 个 job/50 条，试跑落 __ungrouped__")

        # ---------------- 4.5 retry-failed：真失败 → 条目落 failed → 重跑成功
        code, _, created2 = call(alice, base, "/api/batches", "POST", {
            "batch_no": "AW26-002", "prompt_id": prompt_id, "prompt": prompt_text,
            "resolution": "720P", "duration": -1, "upload_id": upload_id})
        assert code == 201, (code, created2)
        batch2 = body(created2)["batch"]
        assert batch2["image_name"] == "look.png", batch2      # 创建时可直接带图
        code, _, locked2 = call(alice, base, f"/api/batches/{batch2['id']}/lock", "POST",
                                {"template_ids": ready_ids[:5], "confirmed": True})
        assert code == 200, (code, locked2)
        settings.config["WAN_BASE_URL"] = f"{failed_base}/api/v1"     # 让云端任务必然失败
        try:
            code, _, broken = call(alice, base, f"/api/batches/{batch2['id']}/produce", "POST", {"count": 20})
            assert code == 201, (code, broken)
            assert body(broken)["taken"] == 5, body(broken)
            assert body(broken)["job"]["duration"] == -1, body(broken)["job"]   # 老口径 -1 仍可用
            wait_job(alice, base, body(broken)["job"]["id"])
            batch2 = wait_batch(alice, base, batch2["id"],
                                lambda b: b["active_job_id"] == "" and b["producing"] == 0,
                                label="失败批次对账")
            assert batch2["failed"] == 5 and batch2["done"] == 0, batch2
            code, _, detail2 = call(alice, base, f"/api/batches/{batch2['id']}?item_state=failed")
            assert code == 200 and len(body(detail2)["items"]) == 5, detail2
            assert all(item["error"] for item in body(detail2)["items"]), "失败条目必须带原因"
        finally:
            settings.config["WAN_BASE_URL"] = f"{mock_base}/api/v1"
        code, _, retried = call(alice, base, f"/api/batches/{batch2['id']}/retry-failed", "POST", {"count": 10})
        assert code == 201, (code, retried)
        assert body(retried)["taken"] == 5, body(retried)
        wait_job(alice, base, body(retried)["job"]["id"])
        batch2 = wait_batch(alice, base, batch2["id"],
                            lambda b: b["active_job_id"] == "" and b["done"] == 5,
                            label="重试完成")
        assert batch2["failed"] == 0 and batch2["done"] == 5, batch2
        print("  ✓ retry-failed：5 条失败带原因，重跑后全部 done")

        # ---------------- 5. 重启后加载 + 读时对账
        state_path = Path(settings.data_dir) / "batches" / f"{batch_id}.json"
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
        assert snapshot["active_job_id"] == "" and snapshot["done"] == READY_COUNT

        # 先停服务，再把清单改成「重启时崩溃」的样子
        server.shutdown()
        server.server_close()
        server = None
        app.jobs.shutdown()
        app = None
        time.sleep(0.2)

        killed = snapshot["items"][0]                       # 属于第一个 job 的成功条目
        killed.update({"state": "producing", "job_id": job_ids[0]})
        orphan = snapshot["items"][-1]                      # job 里查不到 → 必须回 pending
        orphan.update({"state": "producing", "job_id": job_ids[0]})
        snapshot["active_job_id"] = job_ids[0]
        snapshot["done"] = READY_COUNT - 2
        snapshot["pending"] = 0
        snapshot["producing"] = 2
        state_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

        settings2 = reload_settings(tmp)
        app2 = WebApplication(settings2)
        reloaded = app2.batches.batches[batch_id]
        assert reloaded["active_job_id"] == "", reloaded["active_job_id"]
        assert reloaded["producing"] == 0, reloaded
        assert reloaded["items"][0]["state"] == "done", reloaded["items"][0]
        assert reloaded["items"][-1]["state"] == "pending", reloaded["items"][-1]
        assert reloaded["done"] == READY_COUNT - 1 and reloaded["pending"] == 1, reloaded
        server2, base2 = start_server(app2)
        code, _, listed = call(alice, base2, "/api/batches")
        assert code == 200 and batch_id in {row["id"] for row in body(listed)["batches"]}, listed
        print("  ✓ 重启后批次状态正确加载：成功条目收敛 done、查不到的 producing 回 pending")

        code, _, resumed = call(alice, base2, f"/api/batches/{batch_id}/produce", "POST", {"count": 20})
        assert code == 201, (code, resumed)
        assert body(resumed)["taken"] == 1, body(resumed)      # 只剩 1 条可生产
        wait_job(alice, base2, body(resumed)["job"]["id"])
        batch = wait_batch(alice, base2, batch_id,
                           lambda b: b["active_job_id"] == "" and b["done"] == READY_COUNT,
                           label="重启后补生产")
        print("  ✓ 重启后剩余 1 条可继续生产")

        code, _, closed = call(alice, base2, f"/api/batches/{batch_id}/close", "POST", {})
        assert code == 200 and body(closed)["batch"]["state"] == "closed", closed
        code, _, denied = call(alice, base2, f"/api/batches/{batch_id}/produce", "POST", {"count": 20})
        assert code == 409, (code, denied)
        code, _, retry = call(alice, base2, f"/api/batches/{batch_id}/retry-failed", "POST", {"count": 10})
        assert code == 409, (code, retry)                     # 没有失败项
        code, _, frozen = call(alice, base2, f"/api/batches/{batch_id}/prompt", "POST", {"prompt": "关闭后不能再改"})
        assert code == 409, (code, frozen)                    # 批次已关闭，提示词一并冻结
        print("  ✓ 关闭后 409（produce / retry-failed / prompt），retry-failed 无失败项也 409")

        # ---------------- 6. 删除批次：DELETE /api/batches/{id}
        state_dir = Path(settings2.data_dir)
        batches_dir = state_dir / "batches"
        jobs_dir = state_dir / "jobs"
        output_dir = state_dir / "output"

        def snapshot_products() -> tuple:
            """jobs/ 与 output/ 下已落盘文件的名字+大小；删批次绝不能动它们。

            原子写留下的 *.tmp（jobs.py/_save、jobs.py:download 的临时文件）是瞬时文件，
            不纳入快照，否则生产中被误判成「删了文件」。
            """
            rows = []
            for folder in (jobs_dir, output_dir):
                for path in sorted(folder.rglob("*")):
                    if not path.is_file() or path.suffix in {".tmp", ".part", ".download"}:
                        continue
                    rows.append((str(path.relative_to(state_dir)).replace("\\", "/"),
                                 path.stat().st_size))
            return tuple(rows)

        def make_batch(batch_no, **extra):
            code, _, raw = call(alice, base2, "/api/batches", "POST", {
                "batch_no": batch_no, "prompt_id": prompt_id, "prompt": prompt_text,
                "resolution": "720P", "duration": 7, **extra})
            assert code == 201, (batch_no, code, raw)
            return body(raw)["batch"]["id"]

        assert snapshot_products(), "产物快照不能为空，否则『没动文件』的断言无意义"

        # 6.1 owner 删 draft → 200 + {ok,id}，文件消失、列表消失、详情与重复删 404
        draft_id = make_batch("AW26-DEL1")
        draft_path = batches_dir / f"{draft_id}.json"
        assert draft_path.is_file(), draft_path
        code, _, raw = call(alice, base2, f"/api/batches/{draft_id}", "DELETE")
        assert code == 200, (code, raw)
        assert body(raw) == {"ok": True, "id": draft_id}, body(raw)
        assert not draft_path.exists(), "批次 JSON 必须被删掉"
        assert draft_id not in {row["id"] for row in body(call(alice, base2, "/api/batches")[2])["batches"]}
        assert draft_id not in app2.batches.batches, "内存索引也要清掉"
        code, _, raw = call(alice, base2, f"/api/batches/{draft_id}", "DELETE")
        assert code == 404 and body(raw)["ok"] is False, (code, raw)
        code, _, raw = call(alice, base2, f"/api/batches/{draft_id}")
        assert code == 404, (code, raw)                    # 删除后详情 404
        print("  ✓ owner 删 draft：200＋{ok,id}，文件/列表条目消失，详情与重复删 404")

        # 6.2 非 owner → 403（文件仍在）；admin scope=all 删他人批次 → 200
        other_id = make_batch("AW26-DEL2")
        other_path = batches_dir / f"{other_id}.json"
        code, _, raw = call(bob, base2, f"/api/batches/{other_id}", "DELETE")
        assert code == 403 and body(raw)["ok"] is False, (code, raw)
        assert other_path.is_file(), "403 不能把文件删掉"
        assert other_id in app2.batches.batches
        assert other_id in {row["id"] for row in body(call(admin, base2, "/api/batches?scope=all")[2])["batches"]}
        code, _, raw = call(admin, base2, f"/api/batches/{other_id}", "DELETE")
        assert code == 200 and body(raw)["id"] == other_id, (code, raw)
        assert not other_path.exists()
        print("  ✓ 非 owner 删 403（文件仍在、批次还在）；admin scope=all 可删他人批次 200")

        # 6.3 不存在的 id → 404
        code, _, raw = call(alice, base2, "/api/batches/ffffffffffff", "DELETE")
        assert code == 404 and body(raw)["ok"] is False, (code, raw)
        print("  ✓ 不存在的 id 删除 404（未登录另行 401，见 server 的 _user）")

        # 6.4 生产中的批次 → 409；停稳后 locked 批次 → 200，且产物文件一个没少
        live_id = make_batch("AW26-DEL3", upload_id=upload_id)
        live_path = batches_dir / f"{live_id}.json"
        code, _, raw = call(alice, base2, f"/api/batches/{live_id}/lock", "POST",
                            {"template_ids": ready_ids[:3], "confirmed": True})
        assert code == 200, (code, raw)
        code, _, raw = call(alice, base2, f"/api/batches/{live_id}/produce", "POST", {"count": 20})
        assert code == 201, (code, raw)
        running_job = body(raw)["job"]["id"]
        assert body(raw)["taken"] == 3 and body(raw)["batch"]["active_job_id"] == running_job, body(raw)
        assert app2.jobs.jobs[running_job]["status"] not in {
            "succeeded", "partial", "failed", "canceled"}, "这条断言要求 job 此刻真的在跑"
        stop_check = {name for name, _size in snapshot_products()}
        code, _, raw = call(alice, base2, f"/api/batches/{live_id}", "DELETE")
        assert code == 409, (code, raw)
        assert body(raw)["error"] == "批次正在生产中，请等待生产结束或取消任务后再删除", body(raw)
        assert live_path.is_file(), "409 不能删掉文件"
        assert live_id in app2.batches.batches, "409 后批次必须还在"
        # job 在跑会新增产物，所以这里只断言「没有被删掉的文件」，不比整个快照
        assert stop_check - {name for name, _size in snapshot_products()} == set(), \
            "409 不能删掉 jobs/output 下任何已有文件"
        wait_job(alice, base2, running_job)
        stopped = wait_batch(alice, base2, live_id, lambda b: b["active_job_id"] == "", label="删除前排空")
        assert stopped["state"] == "locked", stopped        # 生产不改批次状态，仍是 locked
        products_before = snapshot_products()               # job 已终态，此刻不再有人写产物
        code, _, raw = call(alice, base2, f"/api/batches/{live_id}", "DELETE")
        assert code == 200 and body(raw)["id"] == live_id, (code, raw)
        assert not live_path.exists(), "locked 批次也要允许删除"
        assert snapshot_products() == products_before, "删除批次绝不能动 jobs/output 下的产物"
        print("  ✓ 生产中 409（文案与契约一致、文件仍在）；停稳后 locked 批次删 200，产物文件一个没少")

        # 6.5 删掉已关闭批次 → 200；历史分组：批次分组消失、job 落回 __ungrouped__
        closed_path = batches_dir / f"{batch_id}.json"
        assert closed_path.is_file()
        # 该批次名下的 job（含重启后补生产的那次，共 3 个）——删批次后它们都要落回未分组
        moved_ids = {str(job.get("id")) for job in app2.jobs.jobs.values()
                     if str(job.get("batch_ref") or "") == batch_id}
        moved_records = sum(len(app2.jobs.jobs[job_id].get("records") or []) for job_id in moved_ids)
        assert len(moved_ids) == 3 and moved_records == READY_COUNT + 1, (len(moved_ids), moved_records)
        groups_before = {row["key"]: row for row in body(call(alice, base2, "/api/history/groups")[2])["groups"]}
        assert batch_id in groups_before, groups_before
        ungrouped_before = groups_before["__ungrouped__"]
        products_before = snapshot_products()
        code, _, raw = call(alice, base2, f"/api/batches/{batch_id}", "DELETE")
        assert code == 200 and body(raw)["id"] == batch_id, (code, raw)
        assert not closed_path.exists()
        assert snapshot_products() == products_before, "删除批次不能碰 web-data/output/ 与 web-data/jobs/"
        for job_id in sorted(moved_ids):
            assert (jobs_dir / f"{job_id}.json").is_file(), f"job 文件不该被删：{job_id}"
            assert app2.jobs.jobs[job_id]["batch_ref"] == batch_id, "batch_ref 必须保持原值（历史不回写）"
        rows_after = body(call(alice, base2, "/api/history/groups")[2])["groups"]
        keyed_after = {row["key"]: row for row in rows_after}
        assert batch_id not in keyed_after, "删除后历史分组不能再出现该批次"
        assert rows_after[-1]["key"] == "__ungrouped__", rows_after
        ungrouped_after = keyed_after["__ungrouped__"]
        assert ungrouped_after["job_count"] == ungrouped_before["job_count"] + len(moved_ids), ungrouped_after
        assert ungrouped_after["total"] == ungrouped_before["total"] + moved_records, ungrouped_after
        code, _, raw = call(alice, base2, "/api/history/groups/__ungrouped__?offset=0&limit=500")
        assert code == 200, (code, raw)
        moved = [row for row in body(raw)["items"] if row["job_id"] in moved_ids]
        assert len(moved) == moved_records, len(moved)
        assert all(row["download_url"] for row in moved), "落回未分组的成功记录仍要能下载"
        code, _, raw = call(alice, base2, f"/api/history/groups/{batch_id}")
        assert code == 404, (code, raw)                    # 旧 key 已失效，不留点不开的空分组
        print(f"  ✓ 删除后历史分组：批次分组消失、{len(moved_ids)} 个 job/{moved_records} 条落回 __ungrouped__，"
              "batch_ref 不回写、产物没少")

        # 6.6 批次号索引一并释放：同号可重新创建
        again_id = make_batch("AW26-001")
        assert again_id != batch_id and again_id in app2.batches.batches
        print("  ✓ 删除批次同时释放批次号索引，同号可重新创建")

        print("OK 批次创建 → 候选 → 锁定 → 分批生产 → 历史分组 → 重启恢复 → 删除批次 全部通过")
        return 0
    finally:
        for item in (server, server2):
            if item is not None:
                item.shutdown()
                item.server_close()
        for instance in (app, app2):
            if instance is not None:
                instance.jobs.shutdown()
        if mock.poll() is None:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()
        if mock_fail.poll() is None:
            mock_fail.terminate()
            try:
                mock_fail.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock_fail.kill()


if __name__ == "__main__":
    raise SystemExit(main())
