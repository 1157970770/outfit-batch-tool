#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""万相 3.0 · 参考生视频（alibaba/wan-3.0/reference-to-video）批量客户端。

只依赖标准库（HTTP 上传用系统自带 curl.exe）。
规格见 docs/wan-3.0-reference-to-video.md，本文件每条校验都对应其中一节。

用法:
  python3 wan3_r2v.py validate --tasks tasks.example.json
  python3 wan3_r2v.py build    --tasks tasks.example.json --index 0
  python3 wan3_r2v.py run      --tasks tasks.example.json [--limit N] [--concurrency 1]
  python3 wan3_r2v.py upload   ./assets/role.mp4
  python3 wan3_r2v.py query    2013508786110730241
"""

from __future__ import annotations

import argparse
import json
import os
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

BASE = os.environ.get("RH_BASE_URL", "https://www.runninghub.cn/openapi/v2").rstrip("/")
SUBMIT_PATH = "/alibaba/wan-3.0/reference-to-video"
QUERY_PATH = "/query"
UPLOAD_PATH = "/media/upload/binary"

RESOLUTIONS = ("480P", "720P", "1080P")
ASPECT_RATIOS = ("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16")
DURATIONS = ("auto",) + tuple(str(i) for i in range(2, 31))
MAX_VIDEOS, MAX_IMAGES, MAX_AUDIOS = 5, 10, 5
MAX_VIDEO_SEGMENT = 15.0   # 单段 1–15 秒
MAX_VIDEO_TOTAL = 15.0     # 参考视频总时长 ≤15 秒
MAX_INPUT_PLUS_OUTPUT = 30.0
MAX_SEED = 2147483647

BODY_KEYS = (
    "prompt", "videoUrls", "imageUrls", "audioUrls", "fileUrl", "linkUrl",
    "resolution", "aspectRatio", "duration", "audio", "seed",
    "promptExtend", "watermark", "webhookUrl",
)
MEDIA_KEYS = ("videoUrls", "imageUrls", "audioUrls")
TASK_META_KEYS = ("id", "output", "note")


class ApiError(RuntimeError):
    pass


class WaitTimeout(ApiError):
    """轮询超时：任务可能还在跑，保留 taskId 便于续查，不要重复提交。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def api_key(explicit: str | None) -> str:
    key = (explicit or os.environ.get("RUNNINGHUB_API_KEY", "")).strip()
    if not key:
        raise SystemExit("缺少 API Key：设置环境变量 RUNNINGHUB_API_KEY，或传 --api-key。")
    return key


def as_bool(value, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{field} 需要布尔值，收到 {value!r}")


def as_int(value, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 需要整数，收到布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{field} 需要整数，收到 {value!r}")


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


# ---------------------------------------------------------------------------
# 构造 + 校验
# ---------------------------------------------------------------------------

def build_body(task: dict, defaults: dict, probe: bool = True) -> tuple[dict, list[str], list[str]]:
    """返回 (请求体, errors, warnings)。errors 非空时不要提交。"""
    errors: list[str] = []
    warnings: list[str] = []

    unknown = set(task) - set(BODY_KEYS) - set(TASK_META_KEYS)
    if unknown:
        errors.append(f"未知字段: {', '.join(sorted(unknown))}（拼错的字段会被服务端静默忽略，这里直接拦下）")

    body: dict = {}
    for key in BODY_KEYS:
        if key in task:
            value = task[key]
        elif key in defaults:
            value = defaults[key]
        else:
            continue
        if value is None:
            continue
        body[key] = value

    # prompt
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        errors.append("prompt 必填，且不能为空字符串")
        prompt = prompt if isinstance(prompt, str) else ""
    elif len(prompt) > 5000:
        warnings.append(f"prompt 长度 {len(prompt)}，建议精简到 2000 字以内")

    # 三个素材数组
    for key in MEDIA_KEYS:
        if key not in body:
            continue
        value = body[key]
        if isinstance(value, str):
            errors.append(f"{key} 必须是数组，收到字符串（单个素材也要包成 [\"...\"]）")
            body[key] = [value]
        elif not isinstance(value, list):
            errors.append(f"{key} 必须是数组，收到 {type(value).__name__}")
            body[key] = []
        for idx, item in enumerate(body[key]):
            if not isinstance(item, str) or not item.strip():
                errors.append(f"{key}[{idx}] 必须是非空字符串（URL 或本地路径）")

    for key in MEDIA_KEYS:
        for idx, item in enumerate(body.get(key) or []):
            if isinstance(item, str) and not item.startswith(("http://", "https://", "data:")):
                if not Path(item).exists():
                    warnings.append(f"{key}[{idx}] 本地文件不存在: {item}（提交前需上传或改成 URL）")

    videos = body.get("videoUrls") or []
    images = body.get("imageUrls") or []
    audios = body.get("audioUrls") or []

    if len(videos) > MAX_VIDEOS:
        errors.append(f"videoUrls 最多 {MAX_VIDEOS} 段，当前 {len(videos)} 段")
    if len(images) > MAX_IMAGES:
        errors.append(f"imageUrls 最多 {MAX_IMAGES} 张，当前 {len(images)} 张")
    if len(audios) > MAX_AUDIOS:
        errors.append(f"audioUrls 最多 {MAX_AUDIOS} 段，当前 {len(audios)} 段")

    # fileUrl / linkUrl
    file_url, link_url = body.get("fileUrl"), body.get("linkUrl")
    if file_url and link_url:
        errors.append("fileUrl 与 linkUrl 互斥，只能二选一")
    for field, value in (("fileUrl", file_url), ("linkUrl", link_url)):
        if value is not None and not isinstance(value, str):
            errors.append(f"{field} 必须是字符串 URL")

    # 枚举字段
    resolution = body.get("resolution", "1080P")
    if resolution not in RESOLUTIONS:
        errors.append(f"resolution 只能是 {RESOLUTIONS}，收到 {resolution!r}")
    body["resolution"] = resolution

    aspect = body.get("aspectRatio", "adaptive")
    if aspect not in ASPECT_RATIOS:
        errors.append(f"aspectRatio 只能是 {ASPECT_RATIOS}，收到 {aspect!r}")
    body["aspectRatio"] = aspect

    duration = body.get("duration", "5")
    if isinstance(duration, bool):
        errors.append(f"duration 需要字符串枚举，收到 {duration!r}")
        duration = "5"
    elif isinstance(duration, (int, float)):
        warnings.append(f"duration 传了数字 {duration}，已自动转成字符串 \"{int(duration)}\"（接口要字符串）")
        duration = str(int(duration))
    elif isinstance(duration, str):
        duration = duration.strip()
    if duration not in DURATIONS:
        errors.append(f"duration 只能是 auto 或 \"2\"-\"30\"，收到 {duration!r}")
        duration = "5"
    body["duration"] = duration

    # 布尔 / 整数
    for field, default in (("audio", True), ("promptExtend", True), ("watermark", False)):
        try:
            body[field] = as_bool(body.get(field, default), field)
        except ValueError as exc:
            errors.append(str(exc))

    if "seed" in body:
        try:
            seed = as_int(body["seed"], "seed")
            if not 0 <= seed <= MAX_SEED:
                errors.append(f"seed 超范围 [0, {MAX_SEED}]，收到 {seed}")
            body["seed"] = seed
        except ValueError as exc:
            errors.append(str(exc))
            body.pop("seed", None)

    # 深度思考模式：带文档/网页时 promptExtend 必须为 true
    if (file_url or link_url) and body.get("promptExtend") is False:
        errors.append("传了 fileUrl/linkUrl 时 promptExtend 必须为 true（会自动开深度思考）")

    # 视频时长约束
    video_durations: list[float | None] = []
    if videos and probe:
        for src in videos:
            video_durations.append(ffprobe_duration(src))
    else:
        video_durations = [None] * len(videos)

    for idx, secs in enumerate(video_durations):
        if secs is None:
            continue
        if not 1.0 - 1e-6 <= secs <= MAX_VIDEO_SEGMENT + 1e-6:
            errors.append(f"videoUrls[{idx}] 时长 {secs:.1f}s，单段必须在 1–15 秒内")

    if videos and any(s is not None for s in video_durations) and len(videos) == len(video_durations):
        known = [s for s in video_durations if s is not None]
        if len(known) == len(videos):
            total = sum(known)
            if total > MAX_VIDEO_TOTAL + 1e-6:
                errors.append(f"参考视频总时长 {total:.1f}s，超过 15 秒上限")
            if duration != "auto":
                combined = total + int(duration)
                if combined > MAX_INPUT_PLUS_OUTPUT + 1e-6:
                    errors.append(
                        f"参考视频 {total:.1f}s + 输出 {duration}s = {combined:.0f}s，超过 30 秒上限"
                    )

    if videos and duration != "auto" and any(s is None for s in video_durations):
        if int(duration) > 15:
            warnings.append(
                f"有参考视频但检测不到其时长，输出 {duration}s 时请自行确认「输入+输出 ≤30 秒」"
            )

    # prompt 指代提示
    if (videos or images or audios) and prompt and not re.search(r"(视频|图|音频)\s*\d+", prompt):
        warnings.append("prompt 里没有「视频1 / 图1 / 音频1」这类指代，模型可能不会用上参考素材")

    return body, errors, warnings


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def post_json(path: str, payload: dict, key: str, timeout: int = 90) -> dict:
    url = f"{BASE}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {exc.code}: {raw[:600]}") from None
    except urllib.error.URLError as exc:
        raise ApiError(f"网络错误: {exc.reason}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(f"响应不是 JSON: {raw[:300]}") from None


def upload(path: str, key: str) -> str:
    exe = shutil.which("curl")
    if not exe:
        raise SystemExit("上传需要 curl，未找到可执行的 curl。")
    result = subprocess.run(
        [exe, "-sS", "--fail-with-body", "-X", "POST", f"{BASE}{UPLOAD_PATH}",
         "-H", f"Authorization: Bearer {key}", "-F", f"file=@{path}", "--max-time", "300"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ApiError(f"上传失败: {(result.stdout or result.stderr)[:400]}")
    data = json.loads(result.stdout)
    if data.get("code") != 0:
        raise ApiError(f"上传被拒: {json.dumps(data, ensure_ascii=False)[:400]}")
    url = data["data"]["download_url"]
    if not url.startswith(("http://", "https://")):
        print(f"⚠ 上传返回的 download_url 不是完整地址（{url}），"
              f"若提交报素材不可达，尝试拼上 CDN 前缀或改用 fileName={data['data'].get('fileName')}",
              file=sys.stderr)
    return url


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    return dest


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


_upload_locks: dict[str, threading.Lock] = {}
_upload_locks_guard = threading.Lock()


def _upload_lock_for(key: str) -> threading.Lock:
    with _upload_locks_guard:
        return _upload_locks.setdefault(key, threading.Lock())


def resolve_media(body: dict, key: str, cache: dict, cache_lock: threading.Lock) -> dict:
    """把本地素材路径替换成上传后的 URL（同一文件并发下也只传一次）。"""
    for field in MEDIA_KEYS:
        values = body.get(field) or []
        if not values:
            continue
        resolved = []
        for value in values:
            if value.startswith(("http://", "https://", "data:")):
                resolved.append(value)
                continue
            path = Path(value)
            if not path.exists():
                raise ApiError(f"本地素材不存在: {value}")
            cache_key = str(path.resolve())
            with cache_lock:
                cached = cache.get(cache_key)
            if cached:
                resolved.append(cached)
                continue
            with _upload_lock_for(cache_key):  # 同一文件串行上传，避免并发重复传
                with cache_lock:
                    cached = cache.get(cache_key)
                if not cached:
                    print(f"  上传素材 {path.name} ...", flush=True)
                    cached = upload(str(path), key)
                    with cache_lock:
                        cache[cache_key] = cached
            resolved.append(cached)
        body[field] = resolved
    return body


def submit(body: dict, key: str) -> dict:
    return post_json(SUBMIT_PATH, body, key)


def wait(task_id: str, key: str, timeout: int, interval: float) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        time.sleep(interval)
        try:
            resp = post_json(QUERY_PATH, {"taskId": task_id}, key, timeout=45)
        except ApiError as exc:
            print(f"  ! 轮询失败（继续重试）: {exc}", file=sys.stderr)
            continue
        status = resp.get("status", "UNKNOWN")
        if status != last:
            print(f"  · {status}", flush=True)
            last = status
        if status == "SUCCESS":
            return resp
        if status == "FAILED":
            reason = resp.get("errorMessage") or json.dumps(resp.get("failedReason") or {}, ensure_ascii=False)
            raise ApiError(f"任务失败 [{resp.get('errorCode', '')}] {reason}")
    raise WaitTimeout(f"等待超时（{timeout}s），taskId={task_id} 可能仍在运行；重跑会续查该任务而不重复提交")


def run_one(task: dict, defaults: dict, key: str, args, state: dict, lock: threading.Lock,
            cache: dict, cache_lock: threading.Lock) -> dict:
    tid = str(task.get("id") or f"task{len(state) + 1}")
    record: dict = {"id": tid, "status": "pending", "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    previous = state.get(tid) or {}
    if previous.get("taskId") and previous.get("status") == "running":
        record["taskId"] = previous["taskId"]  # 上次提交过但没等到结果

    body, errors, warnings = build_body(task, defaults, probe=not args.no_probe)
    for msg in warnings:
        note_warning(tid, msg)
    if errors:
        record["status"] = "invalid"
        record["error"] = "; ".join(errors)
        return record

    try:
        body = resolve_media(body, key, cache, cache_lock)
        if not record.get("taskId"):
            resp = submit(body, key)
            task_id = resp.get("taskId")
            if not task_id:
                raise ApiError(f"提交未返回 taskId: {json.dumps(resp, ensure_ascii=False)[:300]}")
            record["taskId"] = task_id
            record["status"] = "running"
            with lock:  # 立即落盘：进程中途挂掉后重启只续查，不重复扣费
                state[tid] = dict(record)
                save_state(args.state_file, state)
            print(f"[{tid}] 已提交 taskId={task_id} status={resp.get('status')}", flush=True)
        else:
            print(f"[{tid}] 复用已有 taskId={record['taskId']}，继续等待结果", flush=True)

        final = wait(record["taskId"], key, args.timeout, args.interval)
        results = final.get("results") or []
        if not results:
            raise ApiError("SUCCESS 但没有 results")
        first = results[0]
        url = first.get("url")
        if not url:
            record["status"] = "success"
            record["text"] = first.get("text")
            return record

        ext = first.get("outputType") or "mp4"
        out_name = task.get("output") or f"out/{tid}.{ext}"
        dest = Path(out_name)
        if dest.suffix.lower().lstrip(".") != ext.lower():
            dest = dest.with_suffix(f".{ext}")
        download(url, dest)

        usage = final.get("usage") or {}
        record.update({
            "status": "success",
            "file": str(dest.resolve()),
            "outputType": ext,
            "cost": usage.get("consumeMoney") or usage.get("thirdPartyConsumeMoney"),
            "costTime": usage.get("taskCostTime"),
            "expiresNote": "结果链接 24 小时失效，已下载到本地",
        })
    except WaitTimeout as exc:
        record["status"] = "running"
        record["error"] = str(exc)
    except ApiError as exc:
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


def cmd_validate(args) -> int:
    defaults, tasks = load_tasks(Path(args.tasks))
    failures = 0
    warn_counts: dict[str, int] = {}
    quiet = len(tasks) > 10  # 大批量只汇总，不刷屏
    for idx, task in enumerate(tasks):
        tid = task.get("id") or f"#{idx}"
        _, errors, warnings = build_body(task, defaults, probe=not args.no_probe)
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
    body, errors, warnings = build_body(tasks[idx], defaults, probe=not args.no_probe)
    for msg in warnings:
        print(f"提示: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"错误: {msg}", file=sys.stderr)
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 2 if errors else 0


def cmd_run(args) -> int:
    defaults, tasks = load_tasks(Path(args.tasks))
    _warned.clear()
    _upload_locks.clear()
    if args.limit:
        tasks = tasks[: args.limit]
    key = api_key(args.api_key)

    state_file = Path(args.state_file)
    state = load_state(state_file)
    if args.fresh:
        state = {}

    todo = []
    for idx, task in enumerate(tasks):
        tid = str(task.get("id") or f"task{idx + 1}")
        prev = state.get(tid)
        if prev and prev.get("status") == "success" and not args.force:
            print(f"[{tid}] 已完成，跳过（--force 重跑）")
            continue
        todo.append(task)

    if not todo:
        print("没有待执行任务。")
        return 0

    print(f"待执行 {len(todo)} 个任务，并发 {args.concurrency}，轮询上限 {args.timeout}s\n")
    lock = threading.Lock()
    cache: dict = {}
    cache_lock = threading.Lock()
    records = []
    if args.concurrency <= 1:
        for task in todo:
            rec = run_one(task, defaults, key, args, state, lock, cache, cache_lock)
            records.append(rec)
            print(f"[{rec['id']}] -> {rec['status']}"
                  + (f" | {rec.get('file')}" if rec.get("file") else "")
                  + (f" | {rec.get('error')}" if rec.get("error") else ""), flush=True)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(run_one, t, defaults, key, args, state, lock, cache, cache_lock)
                       for t in todo]
            for fut in as_completed(futures):
                rec = fut.result()
                records.append(rec)
                print(f"[{rec['id']}] -> {rec['status']}"
                      + (f" | {rec.get('file')}" if rec.get("file") else "")
                      + (f" | {rec.get('error')}" if rec.get("error") else ""), flush=True)

    ok = sum(1 for r in records if r["status"] == "success")
    failed = sum(1 for r in records if r["status"] == "failed")
    running = sum(1 for r in records if r["status"] == "running")
    invalid = sum(1 for r in records if r["status"] == "invalid")
    cost = 0.0
    for rec in records:
        try:
            cost += float(rec.get("cost") or 0)
        except (TypeError, ValueError):
            pass
    print(f"\n完成 {ok} / 失败 {failed} / 超时待续 {running} / 校验不通过 {invalid}")
    if cost:
        print(f"本批实际扣费 ¥{cost:.2f}")
    print(f"状态文件: {state_file.resolve()}")
    return 0 if failed == 0 and invalid == 0 and running == 0 else 1


def cmd_emit(args) -> int:
    """把每个任务编译成一个完整 HTTP 请求体，逐行写成 JSONL，便于审计或接自建网关。"""
    defaults, tasks = load_tasks(Path(args.tasks))
    if args.count:
        tasks = tasks[: args.count]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    key = api_key(args.api_key) if args.upload else None
    cache: dict = {}
    cache_lock = threading.Lock()
    local_left = 0
    lines = 0
    with out.open("w", encoding="utf-8") as fh:
        for task in tasks:
            body, errors, _ = build_body(task, defaults, probe=not args.no_probe)
            if errors:
                print(f"✗ {task.get('id')}: {'; '.join(errors)}", file=sys.stderr)
                return 2
            if key:
                body = resolve_media(body, key, cache, cache_lock)
            else:
                for field in MEDIA_KEYS:
                    local_left += sum(1 for v in (body.get(field) or [])
                                      if not v.startswith(("http://", "https://", "data:")))
            fh.write(json.dumps({"id": task.get("id"), "endpoint": f"{BASE}{SUBMIT_PATH}", "body": body},
                                ensure_ascii=False) + "\n")
            lines += 1
    print(f"已写出 {lines} 个请求 -> {out}")
    print("每行一个请求：endpoint + body，可直接用任何 HTTP 客户端回放。")
    if local_left:
        print(f"⚠ 其中 {local_left} 处是本地路径，服务端读不到；加 --upload 上传后再写，或先换成公网 URL。",
              file=sys.stderr)
    return 0


def cmd_upload(args) -> int:
    key = api_key(args.api_key)
    print(upload(args.file, key))
    return 0


def cmd_query(args) -> int:
    key = api_key(args.api_key)
    resp = post_json(QUERY_PATH, {"taskId": args.task_id}, key)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="万相3.0 参考生视频 批量客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--api-key")
        p.add_argument("--no-probe", action="store_true", help="不调用 ffprobe 检测素材时长")

    p_val = sub.add_parser("validate", help="只校验任务文件")
    p_val.add_argument("--tasks", required=True)
    common(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_build = sub.add_parser("build", help="打印将发送的 JSON（不提交）")
    p_build.add_argument("--tasks", required=True)
    p_build.add_argument("--index", type=int)
    common(p_build)
    p_build.set_defaults(func=cmd_build)

    p_run = sub.add_parser("run", help="提交并等待结果")
    p_run.add_argument("--tasks", required=True)
    common(p_run)
    p_run.add_argument("--limit", type=int)
    p_run.add_argument("--concurrency", type=int, default=1)
    p_run.add_argument("--timeout", type=int, default=1800)
    p_run.add_argument("--interval", type=float, default=5.0)
    p_run.add_argument("--force", action="store_true", help="重跑已成功的任务")
    p_run.add_argument("--fresh", action="store_true", help="忽略已有状态文件")
    p_run.add_argument("--state-file", default="out/state.json")
    p_run.set_defaults(func=cmd_run)

    p_emit = sub.add_parser("emit", help="把任务编译成 100 个请求原文（JSONL，不提交）")
    p_emit.add_argument("--tasks", required=True)
    p_emit.add_argument("--out", default="batch/requests.jsonl")
    p_emit.add_argument("--count", type=int)
    p_emit.add_argument("--upload", action="store_true", help="把本地素材上传后写入 URL（需要 API Key）")
    common(p_emit)
    p_emit.set_defaults(func=cmd_emit)

    p_up = sub.add_parser("upload", help="上传本地素材，返回 URL")
    p_up.add_argument("file")
    p_up.add_argument("--api-key")
    p_up.set_defaults(func=cmd_upload)

    p_q = sub.add_parser("query", help="查询单个任务")
    p_q.add_argument("task_id")
    p_q.add_argument("--api-key")
    p_q.set_defaults(func=cmd_query)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
