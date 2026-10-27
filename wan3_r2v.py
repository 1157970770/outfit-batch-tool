#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""万相 3.0 · 阿里云百炼官方 API 批量客户端（参考生视频 / 视频编辑）。

默认走官方 DashScope 协议；保留 `--provider runninghub` 作为旧链路回滚。

用法:
  python3 wan3_r2v.py validate --tasks tasks.example.json
  python3 wan3_r2v.py build    --tasks tasks.example.json --index 0
  python3 wan3_r2v.py run      --tasks tasks.example.json [--limit N] [--concurrency 3]
  python3 wan3_r2v.py upload   ./assets/role.mp4
  python3 wan3_r2v.py query    <task_id>
  python3 wan3_r2v.py emit     --tasks tasks.example.json --out batch/requests.jsonl

规格与字段见 docs/wan-3.0-reference-to-video.md。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:  # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.wan_api import (  # noqa: E402
    DASHSCOPE_DEFAULT_BASE, PROVIDER_DASHSCOPE, PROVIDER_RUNNINGHUB,
    RUNNINGHUB_DEFAULT_BASE, STATUS_CANCELED, STATUS_FAILED, STATUS_SUCCEEDED,
    STATUS_UNKNOWN, SUBMIT_SUFFIX, TaskCanceled, TaskExpired, WaitTimeout,
    WanClient, WanConfig, WanError, estimate_cost, to_wire_body, validate_task,
)
from app.media import (  # noqa: E402
    MediaError, describe_media_source, resolve_audio, resolve_body,
    resolve_image, resolve_video, uploader_from_env,
)

PROVIDER = (os.environ.get("WAN_PROVIDER") or PROVIDER_DASHSCOPE).strip().lower()
MODEL = (os.environ.get("WAN_MODEL") or "wan3.0-video").strip()
BASE = (
    os.environ.get("WAN_BASE_URL")
    or (os.environ.get("RH_BASE_URL") if PROVIDER == PROVIDER_RUNNINGHUB else "")
    or (RUNNINGHUB_DEFAULT_BASE if PROVIDER == PROVIDER_RUNNINGHUB else DASHSCOPE_DEFAULT_BASE)
).rstrip("/")


class ApiError(RuntimeError):
    pass


class WaitTimeoutError(ApiError):
    """轮询超时：任务可能还在跑，保留 task_id 便于续查，不要重复提交。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _env_key(provider: str) -> str:
    for name in ("WAN_API_KEY", "DASHSCOPE_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if provider == PROVIDER_RUNNINGHUB:
        return os.environ.get("RUNNINGHUB_API_KEY", "").strip()
    return ""


def api_key(explicit: str | None, provider: str = PROVIDER) -> str:
    key = (explicit or _env_key(provider)).strip()
    if not key:
        names = "WAN_API_KEY / DASHSCOPE_API_KEY" if provider != PROVIDER_RUNNINGHUB else "RUNNINGHUB_API_KEY"
        raise SystemExit(f"缺少 API Key：设置环境变量 {names}，或传 --api-key。")
    return key


def ffprobe_duration(src: str) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            capture_output=True, text=True, timeout=25,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    try:
        value = float(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


def _price_map() -> dict:
    raw = os.environ.get("WAN_PRICE_PER_SECOND", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        try:
            out[str(key).upper()] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def make_config(args, *, require_key: bool = False) -> WanConfig:
    provider = (getattr(args, "provider", None) or PROVIDER).strip().lower()
    base = (
        getattr(args, "base_url", None)
        or os.environ.get("WAN_BASE_URL")
        or (RUNNINGHUB_DEFAULT_BASE if provider == PROVIDER_RUNNINGHUB else DASHSCOPE_DEFAULT_BASE)
    ).rstrip("/")
    cfg = WanConfig(
        provider=provider,
        api_key=api_key(getattr(args, "api_key", None), provider) if require_key else (
            getattr(args, "api_key", None) or _env_key(provider)
        ),
        base_url=base,
        model=(getattr(args, "model", None) or MODEL),
        default_mode=(getattr(args, "mode", None) or "reference"),
        poll_interval=float(getattr(args, "interval", None) or 15),
        poll_timeout=int(getattr(args, "timeout", None) or 1800),
        max_concurrency=int(getattr(args, "concurrency", None) or 3),
        query_qps=float(os.environ.get("WAN_QUERY_QPS") or 10),
        price_per_second=_price_map(),
    )
    return cfg


# ---------------------------------------------------------------------------
# 构造 + 校验（make_batch.py 也复用这个入口）
# ---------------------------------------------------------------------------

def build_body(task: dict, defaults: dict, probe: bool = True,
               default_mode: str = "reference") -> tuple[dict, list[str], list[str]]:
    """返回 (内部请求体, errors, warnings)。errors 非空时不要提交。"""
    return validate_task(
        task, defaults,
        provider=PROVIDER,
        default_mode=default_mode,
        probe=ffprobe_duration if probe else None,
    )


# ---------------------------------------------------------------------------
# 提交 / 轮询 / 下载
# ---------------------------------------------------------------------------

_warned: set[str] = set()
_warn_lock = threading.Lock()


def note_warning(tid: str, msg: str) -> None:
    """同一类提示在整批里只打印一次，避免 100 条任务刷屏。"""
    with _warn_lock:
        if msg in _warned:
            return
        _warned.add(msg)
    print(f"[{tid}] 提示: {msg}", file=sys.stderr)


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "batch-video-client/1.0"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)
    return dest


def wait(client: WanClient, task_id: str, timeout: int, interval: float):
    return client.wait_task(task_id, timeout=timeout, interval=interval,
                            on_status=lambda status: print(f"  · {status}", flush=True))


def run_one(task: dict, defaults: dict, client: WanClient, args, state: dict,
            lock: threading.Lock, cache: dict, cache_lock: threading.Lock) -> dict:
    tid = str(task.get("id") or f"task{len(state) + 1}")
    record: dict = {
        "id": tid, "status": "pending", "provider": client.cfg.provider,
        "model": client.cfg.model, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    previous = state.get(tid) or {}
    previous_task_id = previous.get("task_id") or previous.get("taskId")  # 兼容旧状态
    if previous_task_id and previous.get("status") == "running":
        expires = int(previous.get("task_expires_at") or 0)
        # 旧状态没有 provider 字段 = RunningHub 时代，不能拿它的 taskId 问官方接口
        same_provider = previous.get("provider") == client.cfg.provider
        if same_provider and (not expires or expires > time.time()):
            record["task_id"] = previous_task_id
        else:
            record["status"] = "legacy" if not same_provider else "expired"
            record["error"] = (
                "旧接口 taskId，无法用官方接口续查，请重跑"
                if not same_provider else "task_id 已超过 24h，请重跑"
            )
            return record

    body, errors, warnings = client.build_body(
        task, defaults, default_mode=(args.mode or "reference"),
        probe=(None if args.no_probe else ffprobe_duration),
    )
    for msg in warnings:
        note_warning(tid, msg)
    if errors:
        record["status"] = "invalid"
        record["error"] = "; ".join(errors)
        return record

    try:
        with cache_lock:
            body = resolve_body(body, getattr(args, "uploader", None), cache=cache)
        wire = client.wire_body(body)
        if not record.get("task_id"):
            resp = client.submit(wire)
            record["task_id"] = resp.task_id
            record["request_id"] = resp.request_id
            record["status"] = "running"
            record["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            record["task_expires_at"] = int(time.time()) + 24 * 3600
            with lock:  # 立即落盘：进程中途挂掉后重启只续查，不重复扣费
                state[tid] = dict(record)
                save_state(args.state_file, state)
            print(f"[{tid}] 已提交 taskId={resp.task_id} status={resp.status}", flush=True)
        else:
            print(f"[{tid}] 复用已有 taskId={record['task_id']}，继续等待结果", flush=True)

        final = wait(client, record["task_id"], args.timeout, args.interval)
        if final.status == STATUS_UNKNOWN:
            raise TaskExpired(record["task_id"])
        if final.status == STATUS_CANCELED:
            raise TaskCanceled(record["task_id"])
        if final.status == STATUS_FAILED:
            raise ApiError(
                f"任务失败 [{final.code or 'UNKNOWN'}] {final.message or '无错误信息'}"
                + (f" request_id={final.request_id}" if final.request_id else "")
            )
        if final.status != STATUS_SUCCEEDED or not final.video_url:
            raise ApiError("SUCCESS 但没有结果地址")

        out_name = task.get("output") or f"out/{tid}.mp4"
        dest = Path(out_name).with_suffix(".mp4")
        download(final.video_url, dest)

        usage = final.usage or {}
        billable, estimated = estimate_cost(usage, body.get("resolution", "720P"),
                                           client.cfg.price_per_second)
        record.update({
            "status": "success",
            "request_id": final.request_id or record.get("request_id", ""),
            "file": str(dest.resolve()),
            "usage": usage,
            "billable_seconds": billable,
            "estimated_cost": estimated,
            "cost": None,
            "costTime": usage.get("task_cost_time") or usage.get("taskCostTime"),
            "expiresNote": "官方结果链接 24 小时失效，已下载到本地",
        })
    except WaitTimeout as exc:
        record["status"] = "running"
        record["error"] = str(exc)
    except TaskExpired as exc:
        record["status"] = "expired"
        record["error"] = str(exc)
    except TaskCanceled as exc:
        record["status"] = "canceled"
        record["error"] = str(exc)
    except WanError as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        if exc.request_id:
            record["request_id"] = exc.request_id
    except (ApiError, MediaError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
    except Exception as exc:  # 单个任务炸掉不影响整批
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"

    with lock:
        state[tid] = record
        save_state(args.state_file, state)
    return record


def save_state(path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def load_tasks(path: Path) -> tuple[dict, list[dict]]:
    # utf-8-sig：兼容记事本 / Windows PowerShell 写出的带 BOM 的 JSON
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    defaults = data.get("defaults") or {}
    tasks = data.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit(f"{path} 里没有 tasks 数组")
    return defaults, tasks


def _provider_of(args) -> str:
    return (getattr(args, "provider", None) or PROVIDER).strip().lower()


def cmd_validate(args) -> int:
    defaults, tasks = load_tasks(Path(args.tasks))
    provider = _provider_of(args)
    failures = 0
    warn_counts: dict[str, int] = {}
    quiet = len(tasks) > 10  # 大批量只汇总，不刷屏
    for idx, task in enumerate(tasks):
        tid = task.get("id") or f"#{idx}"
        _, errors, warnings = validate_task(
            task, defaults, provider=provider,
            default_mode=(args.mode or "reference"),
            probe=(None if args.no_probe else ffprobe_duration),
        )
        for msg in warnings:
            warn_counts[msg] = warn_counts.get(msg, 0) + 1
            if not quiet:
                print(f"[{tid}] 提示: {msg}")
        if errors:
            failures += 1
            for msg in errors:
                print(f"[{tid}] 错误: {msg}")
        elif not quiet:
            print(f"[{tid}] OK")
    for msg, count in warn_counts.items():
        suffix = f"（{count} 个任务相同）" if count > 1 else ""
        print(f"提示: {msg}{suffix}")
    print(f"\n共 {len(tasks)} 个任务，{failures} 个有问题")
    return 2 if failures else 0


def cmd_build(args) -> int:
    defaults, tasks = load_tasks(Path(args.tasks))
    idx = args.index if args.index is not None else 0
    if not 0 <= idx < len(tasks):
        raise SystemExit(f"--index 超出范围 0..{len(tasks) - 1}")
    provider = _provider_of(args)
    body, errors, warnings = validate_task(
        tasks[idx], defaults, provider=provider,
        default_mode=(args.mode or "reference"),
        probe=(None if args.no_probe else ffprobe_duration),
    )
    for msg in warnings:
        print(f"提示: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"错误: {msg}", file=sys.stderr)
    wire = to_wire_body(body, provider, args.model or MODEL)
    print(json.dumps(wire, ensure_ascii=False, indent=2))
    if not getattr(args, "no_endpoint", False):
        base = (args.base_url or BASE).rstrip("/")
        path = f"{base}{SUBMIT_SUFFIX}" if provider != PROVIDER_RUNNINGHUB else f"{base}/alibaba/wan-3.0/reference-to-video"
        print(f"\n# endpoint: {path}", file=sys.stderr)
        if provider != PROVIDER_RUNNINGHUB:
            print("# header:   X-DashScope-Async: enable", file=sys.stderr)
    return 2 if errors else 0


def cmd_run(args) -> int:
    defaults, tasks = load_tasks(Path(args.tasks))
    _warned.clear()
    config = make_config(args, require_key=True)
    client = WanClient(config)
    args.uploader = uploader_from_env(log=lambda msg: print(f"  {msg}", flush=True))
    if args.uploader is None:
        print("  提示: 没有 COS_* 环境变量，本地图片将走 Base64；本地视频/音频会直接报错。",
              file=sys.stderr)

    if args.limit:
        tasks = tasks[: args.limit]

    state_file = Path(args.state_file)
    state = load_state(state_file)
    if args.fresh:
        state = {}

    todo = []
    for idx, task in enumerate(tasks):
        tid = str(task.get("id") or f"task{idx + 1}")
        prev = state.get(tid)
        if prev and prev.get("status") == "success" and not args.force:
            file_path = prev.get("file")
            if file_path and Path(file_path).exists():
                print(f"[{tid}] 已完成，跳过（--force 重跑）")
                continue
            print(f"[{tid}] 记录为成功但本地文件不见了，重新跑")
        todo.append(task)

    if not todo:
        print("没有待执行任务。")
        return 0

    print(f"接口 {config.provider} · 模型 {config.model} · 待执行 {len(todo)} 条，"
          f"并发 {args.concurrency}，轮询 {args.interval:g}s / 上限 {args.timeout}s\n")
    lock = threading.Lock()
    cache: dict = {}
    cache_lock = threading.Lock()
    records = []
    if args.concurrency <= 1:
        for task in todo:
            rec = run_one(task, defaults, client, args, state, lock, cache, cache_lock)
            records.append(rec)
            _print_record(rec)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(run_one, t, defaults, client, args, state, lock, cache, cache_lock)
                       for t in todo]
            for fut in as_completed(futures):
                rec = fut.result()
                records.append(rec)
                _print_record(rec)

    ok = sum(1 for r in records if r["status"] == "success")
    failed = sum(1 for r in records if r["status"] == "failed")
    running = sum(1 for r in records if r["status"] == "running")
    invalid = sum(1 for r in records if r["status"] == "invalid")
    expired = sum(1 for r in records if r["status"] in ("expired", "legacy"))
    canceled = sum(1 for r in records if r["status"] == "canceled")
    billable = sum(float(r.get("billable_seconds") or 0) for r in records)
    estimated = sum(float(r.get("estimated_cost") or 0) for r in records if r.get("estimated_cost") is not None)
    has_estimate = any(r.get("estimated_cost") is not None for r in records)

    print(f"\n完成 {ok} / 失败 {failed} / 超时待续 {running} / 校验不通过 {invalid} "
          f"/ 过期 {expired} / 取消 {canceled}")
    print(f"计费秒数 {billable:g}s" + (f" · 预估 ¥{estimated:.2f}（以阿里云账单为准）" if has_estimate else "（官方未返回金额）"))
    print(f"状态文件: {state_file.resolve()}")
    return 0 if failed == 0 and invalid == 0 and running == 0 and expired == 0 and canceled == 0 else 1


def _print_record(rec: dict) -> None:
    print(f"[{rec['id']}] -> {rec['status']}"
          + (f" | {rec.get('file')}" if rec.get("file") else "")
          + (f" | {rec.get('error')}" if rec.get("error") else ""), flush=True)


def cmd_emit(args) -> int:
    """把每个任务编译成一个完整 HTTP 请求体，逐行写成 JSONL，便于审计或接自建网关。"""
    defaults, tasks = load_tasks(Path(args.tasks))
    if args.count:
        tasks = tasks[: args.count]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    provider = _provider_of(args)
    base = (args.base_url or BASE).rstrip("/")
    endpoint = (f"{base}{SUBMIT_SUFFIX}" if provider != PROVIDER_RUNNINGHUB
                else f"{base}/alibaba/wan-3.0/reference-to-video")
    headers = {"X-DashScope-Async": "enable"} if provider != PROVIDER_RUNNINGHUB else {}

    uploader = uploader_from_env() if args.upload else None
    if args.upload and uploader is None:
        print("⚠ --upload 需要 COS_* 环境变量；本次仍写本地路径。", file=sys.stderr)

    cache: dict = {}
    local_left = 0
    lines = 0
    with out.open("w", encoding="utf-8") as fh:
        for task in tasks:
            body, errors, _ = validate_task(
                task, defaults, provider=provider,
                default_mode=(args.mode or "reference"),
                probe=(None if args.no_probe else ffprobe_duration),
            )
            if errors:
                print(f"✗ {task.get('id')}: {'; '.join(errors)}", file=sys.stderr)
                return 2
            if uploader is not None:
                body = resolve_body(body, uploader, cache=cache)
            else:
                for field in ("imageUrls", "videoUrls", "audioUrls"):
                    local_left += sum(1 for v in (body.get(field) or [])
                                      if not str(v).startswith(("http://", "https://", "data:", "oss://")))
            wire = to_wire_body(body, provider, args.model or MODEL)
            fh.write(json.dumps(
                {"id": task.get("id"), "endpoint": endpoint,
                 "headers": headers, "body": wire},
                ensure_ascii=False,
            ) + "\n")
            lines += 1
    print(f"已写出 {lines} 个请求 -> {out}")
    print("每行一个请求：endpoint + headers + body，可直接用任何 HTTP 客户端回放。")
    if local_left:
        print(f"⚠ 其中 {local_left} 处是本地路径，服务端读不到；加 --upload（需 COS_*）后再写，"
              f"或先换成公网 URL。", file=sys.stderr)
    return 0


def cmd_upload(args) -> int:
    uploader = uploader_from_env(log=lambda msg: print(msg, file=sys.stderr))
    if uploader is None:
        raise SystemExit("上传需要 COS_* 环境变量（COS_ENABLED/COS_BUCKET/COS_SECRET_ID/COS_SECRET_KEY）。")
    suffix = Path(args.file).suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        url = resolve_image(args.file, uploader)
    elif suffix in (".mp4", ".mov", ".m4v"):
        url = resolve_video(args.file, uploader)
    elif suffix in (".wav", ".mp3"):
        url = resolve_audio(args.file, uploader)
    else:
        raise SystemExit(f"不认识的素材类型：{suffix}")
    print(url)
    return 0


def cmd_query(args) -> int:
    config = make_config(args, require_key=True)
    client = WanClient(config)
    result = client.get_task(args.task_id)
    print(json.dumps(result.raw or {
        "task_id": result.task_id, "status": result.status,
        "video_url": result.video_url, "usage": result.usage,
        "code": result.code, "message": result.message,
        "request_id": result.request_id,
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="万相3.0 官方 API 批量客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--api-key")
        p.add_argument("--provider", choices=(PROVIDER_DASHSCOPE, PROVIDER_RUNNINGHUB))
        p.add_argument("--model")
        p.add_argument("--base-url")
        p.add_argument("--mode", choices=("edit", "reference"))
        p.add_argument("--no-probe", action="store_true", help="不调用 ffprobe 检测素材时长")

    p_val = sub.add_parser("validate", help="只校验任务文件")
    p_val.add_argument("--tasks", required=True)
    common(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_build = sub.add_parser("build", help="打印将发送的官方 JSON（不提交）")
    p_build.add_argument("--tasks", required=True)
    p_build.add_argument("--index", type=int)
    p_build.add_argument("--no-endpoint", action="store_true")
    common(p_build)
    p_build.set_defaults(func=cmd_build)

    p_run = sub.add_parser("run", help="提交并等待结果")
    p_run.add_argument("--tasks", required=True)
    common(p_run)
    p_run.add_argument("--limit", type=int)
    p_run.add_argument("--concurrency", type=int, default=3)
    p_run.add_argument("--timeout", type=int, default=1800)
    p_run.add_argument("--interval", type=float, default=15.0)
    p_run.add_argument("--force", action="store_true", help="重跑已成功的任务")
    p_run.add_argument("--fresh", action="store_true", help="忽略已有状态文件")
    p_run.add_argument("--state-file", default="out/state.json")
    p_run.set_defaults(func=cmd_run)

    p_emit = sub.add_parser("emit", help="把任务编译成请求原文（JSONL，不提交）")
    p_emit.add_argument("--tasks", required=True)
    p_emit.add_argument("--out", default="batch/requests.jsonl")
    p_emit.add_argument("--count", type=int)
    p_emit.add_argument("--upload", action="store_true", help="把本地素材上传后写入 URL（需要 COS_*）")
    common(p_emit)
    p_emit.set_defaults(func=cmd_emit)

    p_up = sub.add_parser("upload", help="上传本地素材，返回 URL")
    p_up.add_argument("file")
    p_up.add_argument("--api-key")
    p_up.set_defaults(func=cmd_upload)

    p_q = sub.add_parser("query", help="查询单个任务")
    p_q.add_argument("task_id")
    common(p_q)
    p_q.set_defaults(func=cmd_query)

    args = parser.parse_args()
    if args.cmd in ("build", "validate", "emit") and getattr(args, "mode", None) is None:
        args.mode = "reference"
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
