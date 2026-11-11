#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第五批回归测试：P1-5 / P1-19 / P1-20 / P1-21 / P1-22 / P1-23 关键面 + P2 抽查。

离线跑，不联网、不起 HTTP 服务、不调模型。覆盖：
1. 金额字段按角色裁剪：普通用户响应无 billable_seconds/estimated_cost/cost，管理员齐全。
2. WaitTimeout 落终态 `timeout`（不再卡 running），纳入重试白名单；unknown/running 不可重试。
3. 重试归属：新 job 用原 owner、透传 batch_ref（管理员重试他人任务不记账到自己头上）。
4. 失败口径恒等式：items = success + failed + unfinished（usage_for 与 work_for 一致）。
5. EXIF 方向：竖拍图按 EXIF 转正后尺寸校验、上传图不再带方向标签。
6. COS REST 临时凭证：header / 签名 URL 都带 x-cos-security-token，无 token 时不带。
7. 模板清单空表报 TemplateError（而不是 StopIteration 崩栈）。
8. 快速通道条数上限钳死 3。
9. 批次估算裁剪：普通用户无单价/金额，但保留生产档位 count。
10. 任务分类统计：服装换装 / 项链手串 / 未分类三个桶，恒等式与合计都要对得上。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmpdir import tempdir  # noqa: E402

from app.core import BatchRunner, BatchSpec, BatchState, status_counts  # noqa: E402
from app.web.jobs import _FAILED_RECORD_STATES, _RETRYABLE_RECORD_STATES, JobManager  # noqa: E402
from app.web.settings import WebSettings  # noqa: E402


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def make_manager(tmp: Path) -> JobManager:
    settings = SimpleNamespace(
        data_dir=tmp,
        max_active_jobs=2,
        max_active_small_jobs=1,
        max_active_per_user=2,
        max_items=500,
        small_lane_items=3,
        users={"alice": {"role": "user", "enabled": True},
               "bob": {"role": "user", "enabled": True},
               "root": {"role": "admin", "enabled": True}},
    )
    manager = JobManager(settings, {"p1": "把衣服换到模特身上"})
    manager.stop_event.set()
    with manager.changed:
        manager.changed.notify_all()
    manager.dispatcher.join(timeout=3)
    check(not manager.dispatcher.is_alive(), "后台派发线程没停下来，测试无法单步驱动")
    return manager


def base_job(job_id: str, owner: str = "alice", status: str = "failed") -> dict:
    return {
        "id": job_id, "status": status, "owner": owner, "lane": "big",
        "created_at": 1.0, "finished_at": 2.0, "batch_id": f"WEB-{owner}-{job_id}",
        "batch_ref": "batch-ref-9", "total": 3, "done": 3,
        "image_path": "", "image_name": "look.png", "prompt_id": "p1",
        "prompt": "把衣服换到模特身上", "resolution": "720P", "duration": 7,
        "records": [], "templates": [], "cancel_requested": False,
    }


# ---------------------------------------------------------------------------
# 1. 金额字段按角色裁剪（P1-5）
# ---------------------------------------------------------------------------

def test_public_money_redaction(tmp_path=None) -> None:
    tmp = Path(tempdir("batch5-redact-"))
    manager = make_manager(tmp)
    job = base_job("j-redact")
    job["records"] = [
        {"id": "T1", "status": "success", "billable_seconds": 8.5, "estimated_cost": 2.55},
        {"id": "T2", "status": "failed", "billable_seconds": 3.0, "estimated_cost": 0.9},
    ]
    job["summary"] = {"total": 2, "success": 1, "failed": 1, "billable_seconds": 11.5,
                      "estimated_cost": 3.45, "cost": 3.45, "out_dir": "/secret/out"}
    manager.jobs["j-redact"] = job

    mine = manager.public(job, admin=False)
    check("billable_seconds" not in mine["summary"], "普通用户 summary 不能带 billable_seconds")
    check("estimated_cost" not in mine["summary"] and "cost" not in mine["summary"],
          "普通用户 summary 不能带金额")
    for rec in mine["records"]:
        check("billable_seconds" not in rec and "estimated_cost" not in rec,
              f"普通用户 record 不能带金额字段：{rec}")
    check("out_dir" not in mine["summary"], "out_dir 仍然要裁掉")

    admin_view = manager.public(job, admin=True)
    check(admin_view["summary"]["billable_seconds"] == 11.5, "管理员能看到计费秒")
    check(admin_view["records"][0]["estimated_cost"] == 2.55, "管理员能看到单条金额")

    listed = manager.list_for("root", admin=True)
    check(any(j["id"] == "j-redact" for j in listed), "管理员 list_for 可见")
    check(listed[0]["summary"].get("estimated_cost") == 3.45, "list_for 也按 admin 放行金额")


# ---------------------------------------------------------------------------
# 2. WaitTimeout → 终态 timeout，重试白名单（P1-19）
# ---------------------------------------------------------------------------

def test_timeout_is_retryable_terminal() -> None:
    check("timeout" in _RETRYABLE_RECORD_STATES, "等待超时必须可重试")
    check("unknown" not in _RETRYABLE_RECORD_STATES, "提交结果未知不可重试（防二次计费）")
    check("running" not in _RETRYABLE_RECORD_STATES, "进行中当然不可重试")
    counts = status_counts([{"status": "timeout"}, {"status": "running"}, {"status": "success"}])
    check(counts["timeout"] == 1 and counts["running"] == 1 and counts["success"] == 1,
          f"status_counts 必须分列 timeout：{counts}")


def test_wait_timeout_lands_timeout_status() -> None:
    from app.wan_api import WaitTimeout

    tmp = Path(tempdir("batch5-timeout-"))
    spec = BatchSpec(
        batch_id="timeout-1", image_path="", prompt="换装", mode="edit",
        out_dir=tmp / "out", templates=[{"id": "T1", "name": "A", "duration": 1.5}],
        api_key="sk-test", resolution="480P", duration=7,
        price_per_second={"480P": 0.3},
    )
    spec.out_dir.mkdir(parents=True, exist_ok=True)
    runner = object.__new__(BatchRunner)
    runner.spec = spec
    runner.on_event = lambda evt: None
    runner.log = lambda msg: None
    runner.pause_event = threading.Event()
    runner.pause_event.set()
    runner.stop_event = threading.Event()
    runner.state = BatchState(spec)
    runner.state.started = ""
    runner._image_url = "http://example.com/image.jpg"
    runner._upload_lock = threading.Lock()
    runner._save_lock = threading.Lock()
    runner._media_cache = {}
    runner._uploader = None

    class Stub:
        def build_body(self, task, defaults=None, *, default_mode=None,
                       probe=None, video_durations=None):
            return {}, [], []

        def wire_body(self, body):
            return body

        def submit(self, wire):
            from app.wan_api import SubmitResult
            return SubmitResult(task_id="task-1", status="PENDING", request_id="req-1")

        def wait_task(self, task_id, *, timeout=None, interval=None,
                      on_status=None, should_stop=None):
            raise WaitTimeout(task_id, 5)

    runner._client = Stub()
    rec = runner._run_one({"id": "T1", "name": "A", "duration": 1.5, "url": "http://example.com/v.mp4"})
    check(rec["status"] == "timeout", f"等待超时应落终态 timeout，实际：{rec['status']}")
    check(rec.get("task_id") == "task-1", "task_id 要保留，供控制台核对")
    check("timeout" in _RETRYABLE_RECORD_STATES, "超时记录必须能被重试")


# ---------------------------------------------------------------------------
# 3. 重试归属：原 owner + batch_ref 透传（P1-20）
# ---------------------------------------------------------------------------

def test_retry_uses_source_owner_and_batch_ref() -> None:
    tmp = Path(tempdir("batch5-retry-"))
    manager = make_manager(tmp)
    image = tmp / "look.jpg"
    image.write_bytes(b"fake")

    source = base_job("j-src", owner="alice", status="failed")
    source["image_path"] = str(image)
    source["tags"] = {"top": "AW26-001"}
    source["records"] = [{"id": "T1", "status": "failed", "error": "boom"},
                         {"id": "T2", "status": "success"}]
    source["templates"] = [{"id": "T1", "name": "A", "duration": 1.5, "url": "http://x/v.mp4"}]
    manager.jobs["j-src"] = source

    created = manager.retry_failed("j-src", "bob", admin=True)
    check(created["owner"] == "alice", f"重试必须记到原 owner 名下，实际：{created['owner']}")
    check(created["batch_ref"] == "batch-ref-9",
          f"batch_ref 必须透传（批次历史回查不断链），实际：{created.get('batch_ref')}")
    check(created["template_ids"] == ["T1"], "只重试失败项")


# ---------------------------------------------------------------------------
# 4. 失败口径恒等式（本轮新发现）
# ---------------------------------------------------------------------------

def test_failure_accounting_identity() -> None:
    tmp = Path(tempdir("batch5-accounting-"))
    manager = make_manager(tmp)
    job = base_job("j-acc", owner="alice", status="partial")
    job["records"] = [
        {"id": "T1", "status": "success"},
        {"id": "T2", "status": "failed"},
        {"id": "T3", "status": "skipped"},
        {"id": "T4", "status": "unknown", "billable_seconds": 5.0, "estimated_cost": 1.5},
        {"id": "T5", "status": "timeout"},
        {"id": "T6", "status": "running"},
    ]
    manager.jobs["j-acc"] = job
    # _record_cost 兜底路径要读单价；测试里给个静态映射，金额直接来自记录的 estimated_cost
    manager.settings.unit_price = lambda resolution: (0.3, "test")

    usage = manager.usage_for()
    row = usage["accounts"][0] if isinstance(usage.get("accounts"), list) else None
    if row is None:  # 兼容返回结构：找到 alice 那行
        rows = usage.get("rows") or usage.get("items") or []
        row = next(r for r in rows if r.get("owner") == "alice")
    check(row["items"] == row["success"] + row["failed"] + row["unfinished"],
          f"恒等式 items = success + failed + unfinished 必须成立：{row}")
    check(row["failed"] == 4, f"failed/unknown/timeout/skipped 都算失败侧，实际：{row['failed']}")
    check(row["unfinished"] == 1, f"只有 running 算进行中，实际：{row['unfinished']}")
    check(float(row["estimated_cost"]) == 1.5, "unknown 的已计费金额不能从统计里消失")

    work = manager.work_for("alice")
    check(work["total"] == 6 and work["success"] == 1 and work["failed"] == 4,
          f"work_for 口径要与 usage_for 一致：{work}")
    check(work["total"] == work["success"] + work["failed"] + work["unfinished"],
          f"work_for 也要满足恒等式：{work}")


# ---------------------------------------------------------------------------
# 4b. 按任务分类统计（服装换装 / 项链手串 / 未分类）
# ---------------------------------------------------------------------------

def test_usage_categories() -> None:
    tmp = Path(tempdir("batch5-categories-"))
    manager = make_manager(tmp)
    manager.settings.unit_price = lambda resolution: (0.3, "test")

    apparel = base_job("j-apparel", owner="alice")
    apparel["category"] = "服装换装"
    apparel["records"] = [{"id": "T1", "status": "success", "billable_seconds": 7.0,
                           "estimated_cost": 2.1},
                          {"id": "T2", "status": "failed"}]
    necklace = base_job("j-necklace", owner="bob")
    necklace["category"] = "项链手串"
    necklace["records"] = [{"id": "T1", "status": "success", "billable_seconds": 3.0,
                            "estimated_cost": 0.9}]
    legacy = base_job("j-legacy", owner="alice")      # 老 job：没有 category 字段
    legacy["records"] = [{"id": "T1", "status": "success", "billable_seconds": 7.0,
                          "estimated_cost": 2.1}]
    for job in (apparel, necklace, legacy):
        manager.jobs[job["id"]] = job

    usage = manager.usage_for()
    cats = {row["category"]: row for row in usage["categories"]}
    check(set(cats) == {"服装换装", "项链手串", "未分类"},
          f"空分类必须归「未分类」，不能混进业务分类：{cats}")
    check(cats["服装换装"]["items"] == 2 and cats["服装换装"]["failed"] == 1, cats)
    check(cats["项链手串"]["items"] == 1 and cats["项链手串"]["success"] == 1, cats)
    check(float(cats["服装换装"]["estimated_cost"]) == 2.1, cats["服装换装"])
    check(float(cats["项链手串"]["estimated_cost"]) == 0.9, cats["项链手串"])
    check(float(cats["未分类"]["estimated_cost"]) == 2.1, "老任务的钱不能从分类统计里消失")
    for row in usage["categories"]:
        check(row["items"] == row["success"] + row["failed"] + row["unfinished"],
              f"分类行也要满足恒等式：{row}")
        check(abs(float(row["success_rate"]) - (row["success"] / row["items"] if row["items"] else 0)) < 1e-4,
              f"分类成功率口径与账户一致：{row}")
    # 分类维度是账户维度的另一个切面：合计必须对得上
    check(usage["totals"]["items"] == sum(row["items"] for row in usage["categories"]), usage["totals"])
    check(abs(usage["totals"]["estimated_cost"]
              - sum(row["estimated_cost"] for row in usage["categories"])) < 1e-9, usage["totals"])

    work = manager.work_for("alice")
    check({item["category"] for item in work["items"]} == {"服装换装", ""},
          f"work_for 要带原始分类（空串交给接口层折成「未分类」）：{work['items']}")


# ---------------------------------------------------------------------------
# 5. EXIF 方向（P1-21）
# ---------------------------------------------------------------------------

def test_exif_transpose() -> None:
    from PIL import Image
    import app.media as media

    tmp = Path(tempdir("batch5-exif-"))
    img = Image.new("RGB", (300, 600), (200, 30, 30))
    exif = Image.Exif()
    exif[274] = 6                     # Orientation: 旋转 90°（手机竖拍最常见）
    portrait = tmp / "portrait.jpg"
    img.save(portrait, exif=exif)

    info = media.inspect_image(portrait)
    check(info.get("orientation") == 6, f"要读出 EXIF 方向：{info}")
    check((info["width"], info["height"]) == (600, 300),
          f"校验尺寸必须是转正后的：{info['width']}x{info['height']}")

    out, ctype = media.prepare_image(portrait)
    check(ctype == "image/jpeg", f"带 EXIF 的图要走转码：{ctype}")
    with Image.open(out) as up:
        check((up.width, up.height) == (600, 300), "上传图必须是转正后的")
        check(up.getexif().get(274) in (1, None), "上传图不应再带方向标签")
    Path(out).unlink(missing_ok=True)

    # 无 EXIF 的正图不受影响：直接透传
    plain = tmp / "plain.jpg"
    Image.new("RGB", (400, 300), (10, 10, 10)).save(plain)
    out2, ctype2 = media.prepare_image(plain)
    check(ctype2 == "image/jpeg", "普通 JPEG 仍直接透传")
    check(Path(out2) == plain, "无 EXIF 的 RGB JPEG 不应转码")


# ---------------------------------------------------------------------------
# 6. COS REST 临时凭证（P1-22）
# ---------------------------------------------------------------------------

def test_cos_rest_token() -> None:
    from app.cos_rest import CosRestClient

    client = CosRestClient("bucket", "ap-guangzhou", "id", "key", token="STS-token")
    headers = client._headers("put", "a/b.jpg")
    check(headers.get("x-cos-security-token") == "STS-token", "PUT 头必须带安全令牌")
    check("x-cos-security-token" in client.sign_url("a/b.jpg", 600), "签名 URL 必须带安全令牌")
    url = client.sign_url("a/b.jpg", 600)
    check("q-signature" in url and "x-cos-security-token" in url, f"签名 URL 结构异常：{url[:120]}")

    plain = CosRestClient("bucket", "ap-guangzhou", "id", "key")
    check("x-cos-security-token" not in plain._headers("put", "a.jpg"),
          "长期凭证不该带令牌头")


# ---------------------------------------------------------------------------
# 7. 模板清单空表（P2）
# ---------------------------------------------------------------------------

def test_empty_sheet_raises_template_error() -> None:
    from openpyxl import Workbook
    from app.web.templates import TemplateCatalog, TemplateError

    wb = Workbook()
    ws = wb.active
    ws.append([])                     # 空表：连表头都没有
    try:
        TemplateCatalog._read_tag_dictionary(ws)
    except StopIteration:
        raise AssertionError("空表不能裸抛 StopIteration（会 500 / 崩栈）")
    except TemplateError:
        pass                          # 正确：友好报错
    else:
        raise AssertionError("空表必须报 TemplateError")


# ---------------------------------------------------------------------------
# 8. 快速通道钳位（P2）
# ---------------------------------------------------------------------------

def test_small_lane_clamped_to_3() -> None:
    settings = WebSettings.__new__(WebSettings)
    settings.web = {"small_lane_items": 20}
    check(settings.small_lane_items == 3, f"配置 20 也必须钳到 3：{settings.small_lane_items}")
    settings.web = {"small_lane_items": 2}
    check(settings.small_lane_items == 2, "合理配置不动")
    settings.web = {}
    check(settings.small_lane_items == 3, "缺省 3")


# ---------------------------------------------------------------------------
# 9. 批次估算裁剪（P1-5 批次侧）
# ---------------------------------------------------------------------------

def test_batch_estimate_money_view() -> None:
    from app.web.batches import BatchManager

    estimate = {"count": 2, "seconds": 17.0, "unit_price": 0.6, "price_source": "default",
                "amount": 10.2, "resolution": "720P",
                "options": [{"count": 20, "amount": 20.4}], "currency": "CNY"}
    admin_view = BatchManager._money_view({"role": "admin"}, estimate)
    check(admin_view["unit_price"] == 0.6 and admin_view["amount"] == 10.2, "管理员看全量")

    user_view = BatchManager._money_view({"role": "user"}, estimate)
    check("unit_price" not in user_view and "amount" not in user_view
          and "price_source" not in user_view, f"普通用户不能带单价/金额：{user_view}")
    check(user_view["options"] == [{"count": 20}], f"档位 count 保留、amount 剥掉：{user_view}")
    check(user_view["count"] == 2 and user_view["seconds"] == 17.0, "时长与条数保留")


def main() -> int:
    tests = [
        test_public_money_redaction,
        test_timeout_is_retryable_terminal,
        test_wait_timeout_lands_timeout_status,
        test_retry_uses_source_owner_and_batch_ref,
        test_failure_accounting_identity,
        test_usage_categories,
        test_exif_transpose,
        test_cos_rest_token,
        test_empty_sheet_raises_template_error,
        test_small_lane_clamped_to_3,
        test_batch_estimate_money_view,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {test.__name__}: {exc}")
            import traceback
            traceback.print_exc()
            return 1
        print(f"  ✓ {test.__name__}")
    print(f"通过 {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
