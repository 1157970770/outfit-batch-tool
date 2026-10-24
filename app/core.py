#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换装批量生产器 · 核心引擎（不依赖界面，可单独测试）。

职责：模板清单 → 任务规划 → 上传服装图 → 并发提交 → 轮询 → 下载 → 校验 → 记账落盘。
界面层只负责调用 run() 并接收事件回调。
"""

from __future__ import annotations

import csv
import json
import os
import random
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

try:                      # 作为包导入（测试/界面）
    from .storage import Storage, file_fingerprint, object_key
except ImportError:       # 直接 python3 app/core.py 时
    from storage import Storage, file_fingerprint, object_key  # type: ignore

BASE_DEFAULT = "https://www.runninghub.cn/openapi/v2"
SUBMIT_PATH = "/alibaba/wan-3.0/reference-to-video"
QUERY_PATH = "/query"
UPLOAD_PATH = "/media/upload/binary"
ACCOUNT_URL = "https://www.runninghub.cn/uc/openapi/accountStatus"

DEFAULT_PROMPT = (
    "视频1中的人物，保留其面部、发型、肤色与动作不变，"
    "仅将身上服装更换为图1中无头模特展示的这套服装（版型、颜色、材质、细节一致），"
    "人物头部完整、面部清晰、颈部衔接自然，画面自然真实，布料质感清晰"
)

GROUP_SIZE = 100
TOTAL_TEMPLATES = 500


class EngineError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 模板清单
# ---------------------------------------------------------------------------

def load_manifest(source: str, timeout: int = 15) -> list[dict]:
    """source 可以是 https URL、本地文件路径。返回 [{id, url, duration}]。"""
    if source.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(source, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8-sig")
        except (urllib.error.URLError, OSError) as exc:
            raise EngineError(f"模板清单下载失败：{exc}") from None
    else:
        path = Path(source)
        if not path.exists():
            raise EngineError(f"模板清单不存在：{source}")
        raw = path.read_text(encoding="utf-8-sig")

    data = json.loads(raw)
    items = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise EngineError("模板清单格式不对，应为数组或 {templates:[...]}")

    out = []
    for idx, item in enumerate(items):
        if isinstance(item, str):
            item = {"id": f"{idx + 1:03d}", "url": item}
        tid = str(item.get("id") or f"{idx + 1:03d}").zfill(3)
        url = item.get("url")
        if not url:
            raise EngineError(f"模板 {tid} 缺少 url")
        out.append({"id": tid, "url": url, "duration": item.get("duration")})
    return out


def plan_batch(templates: list[dict], mode: str, group: int | None = None,
               count: int = GROUP_SIZE, rng: random.Random | None = None) -> list[dict]:
    """mode: group（按第 N 组取 100 条）/ random（随机取 count 条）/ first（取前 count 条）。"""
    rng = rng or random.Random()
    if mode == "group":
        if not group:
            raise EngineError("未指定分组")
        start = (group - 1) * GROUP_SIZE
        picked = templates[start:start + GROUP_SIZE]
        if len(picked) < GROUP_SIZE:
            raise EngineError(f"第 {group} 组模板不足 {GROUP_SIZE} 个（清单共 {len(templates)} 个）")
    elif mode == "random":
        picked = rng.sample(templates, min(count, len(templates)))
    elif mode == "first":
        picked = templates[:count]
    else:
        raise EngineError(f"未知的分组方式：{mode}")
    return picked


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def post_json(path: str, payload: dict, key: str, base: str, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise EngineError(f"HTTP {exc.code}: {raw[:300]}") from None
    except urllib.error.URLError as exc:
        raise EngineError(f"网络错误：{exc.reason}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise EngineError(f"响应不是 JSON：{raw[:200]}") from None


def upload_media(path: str, key: str, base: str) -> str:
    exe = shutil.which("curl")
    if not exe:
        raise EngineError("缺少 curl，无法上传素材")
    result = subprocess.run(
        [exe, "-sS", "--fail-with-body", "-X", "POST", f"{base}{UPLOAD_PATH}",
         "-H", f"Authorization: Bearer {key}", "-F", f"file=@{path}", "--max-time", "600"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise EngineError(f"上传失败：{(result.stdout or result.stderr)[:300]}")
    data = json.loads(result.stdout)
    if data.get("code") != 0:
        raise EngineError(f"上传被拒：{json.dumps(data, ensure_ascii=False)[:300]}")
    url = data["data"]["download_url"]
    if not url.startswith(("http://", "https://")):
        raise EngineError(f"上传返回的地址不完整：{url}")
    return url


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=600) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)


def check_account(key: str, timeout: int = 15) -> dict:
    """校验 RunningHub Key 并读取余额。返回 {ok, balance, coins, currency, api_type, message}。"""
    req = urllib.request.Request(
        ACCOUNT_URL,
        data=json.dumps({"apikey": key}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "message": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "message": f"网络错误：{exc.reason}"}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "message": f"返回不是 JSON：{raw[:120]}"}

    if data.get("code") != 0:
        return {"ok": False, "message": data.get("msg") or "Key 无效"}

    info = data.get("data") or {}
    return {
        "ok": True,
        "balance": info.get("remainMoney"),
        "coins": info.get("remainCoins"),
        "currency": info.get("currency", "CNY"),
        "api_type": info.get("apiType", ""),
        "running": info.get("currentTaskCounts", "0"),
        "message": "Key 有效",
    }


def probe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------

@dataclass
class BatchSpec:
    batch_id: str
    image_path: str
    prompt: str
    out_dir: Path
    templates: list[dict]
    api_key: str
    base: str = BASE_DEFAULT
    resolution: str = "720P"
    duration: str = "7"
    aspect_ratio: str = "9:16"
    concurrency: int = 3
    poll_interval: float = 5.0
    poll_timeout: int = 1800
    expect_seconds: float = 7.0
    # 可选：产物同步到对象存储（None = 只落本地，保持旧行为）
    storage: Storage | None = None
    archive_prefix: str = "output"
    archive_expires: int = 7 * 24 * 3600


@dataclass
class BatchState:
    spec: BatchSpec
    records: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "batch_id": self.spec.batch_id,
                "started": getattr(self, "started", ""),
                "concurrency": self.spec.concurrency,
                "prompt": self.spec.prompt,
                "records": list(self.records.values()),
            }


class BatchRunner:
    """一条批次 = 一个实例。run() 阻塞，直到跑完 / 被停止。"""

    def __init__(self, spec: BatchSpec, on_event=None, log=None):
        self.spec = spec
        self.on_event = on_event or (lambda evt: None)
        self.log = log or (lambda msg: None)
        self.pause_event = threading.Event()
        self.pause_event.set()          # set = 运行中
        self.stop_event = threading.Event()
        self.state = BatchState(spec)
        self.state.started = time.strftime("%Y-%m-%d %H:%M:%S")
        self._image_url: str | None = None
        self._upload_lock = threading.Lock()
        self._save_lock = threading.Lock()

    # ---- 控制 ----
    def pause(self) -> None:
        self.pause_event.clear()
        self.log("已暂停（正在跑的几条会先跑完）")

    def resume(self) -> None:
        self.pause_event.set()
        self.log("已继续")

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()
        self.log("正在停止…")

    # ---- 主流程 ----
    def run(self) -> dict:
        spec = self.spec
        spec.out_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_image()
        self._save_state()

        total = len(spec.templates)
        done = 0
        self._emit("started", total=total)

        with ThreadPoolExecutor(max_workers=max(1, spec.concurrency)) as pool:
            futures = {pool.submit(self._run_one, tpl): tpl for tpl in spec.templates}
            for fut in as_completed(futures):
                tpl = futures[fut]
                try:
                    rec = fut.result()
                except Exception as exc:  # 单条异常不影响整批
                    rec = {"id": tpl["id"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                    with self.state.lock:
                        self.state.records[tpl["id"]] = rec
                else:
                    with self.state.lock:
                        self.state.records[tpl["id"]] = rec
                done += 1
                self._save_state()
                self._emit("progress", done=done, total=total, record=rec)

        self._export_csv()
        self._save_state()
        summary = {
            "total": total,
            "success": sum(1 for r in self.state.records.values() if r.get("status") == "success"),
            "failed": sum(1 for r in self.state.records.values() if r.get("status") == "failed"),
            "cost": self._total_cost(),
            "out_dir": str(spec.out_dir),
            "archived": sum(1 for r in self.state.records.values() if r.get("cos_key")),
            "archive_failed": sum(1 for r in self.state.records.values() if r.get("archive_error")),
        }
        self._emit("finished", summary=summary)
        return summary

    def _ensure_image(self) -> None:
        with self._upload_lock:
            if self._image_url:
                return
            self.log(f"上传服装图：{Path(self.spec.image_path).name}")
            self._image_url = upload_media(self.spec.image_path, self.spec.api_key, self.spec.base)
            self.log("服装图已就绪，100 条任务共用这一个地址")

    def _run_one(self, tpl: dict) -> dict:
        spec = self.spec
        tid = tpl["id"]
        out_file = spec.out_dir / f"{tid}.mp4"
        rec = {"id": tid, "template": tpl["url"], "status": "pending",
               "file": str(out_file), "cost": None, "seconds": None, "error": None}

        self.pause_event.wait()
        if self.stop_event.is_set():
            rec["status"] = "skipped"
            return rec

        body = {
            "prompt": spec.prompt,
            "imageUrls": [self._image_url],
            "videoUrls": [tpl["url"]],
            "resolution": spec.resolution,
            "aspectRatio": spec.aspect_ratio,
            "duration": spec.duration,
            "audio": False,
            "promptExtend": False,
            "watermark": False,
        }
        started = time.time()
        try:
            resp = post_json(SUBMIT_PATH, body, spec.api_key, spec.base)
            task_id = resp.get("taskId")
            if not task_id:
                raise EngineError(f"提交失败：{resp.get('errorMessage') or resp}")
            rec["task_id"] = task_id
            with self.state.lock:
                self.state.records[tid] = dict(rec)
            self._save_state()

            final = self._wait(task_id)
            results = final.get("results") or []
            if not results or not results[0].get("url"):
                raise EngineError("任务成功但没有结果地址")
            download_file(results[0]["url"], out_file)

            rec["status"] = "success"
            rec["cost"] = ((final.get("usage") or {}).get("consumeMoney"))
            rec["seconds"] = round(time.time() - started, 1)
            rec["verify"] = self._verify(out_file)
            self._archive(rec, out_file)
        except EngineError as exc:
            rec["status"] = "failed"
            rec["error"] = str(exc)
        except Exception as exc:
            rec["status"] = "failed"
            rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec

    def _wait(self, task_id: str) -> dict:
        spec = self.spec
        deadline = time.time() + spec.poll_timeout
        while time.time() < deadline:
            self.pause_event.wait()
            if self.stop_event.is_set():
                raise EngineError("已停止")
            time.sleep(spec.poll_interval)
            resp = post_json(QUERY_PATH, {"taskId": task_id}, spec.api_key, spec.base, timeout=45)
            status = resp.get("status")
            if status == "SUCCESS":
                return resp
            if status == "FAILED":
                raise EngineError(f"[{resp.get('errorCode')}] {resp.get('errorMessage') or '任务失败'}")
        raise EngineError(f"等待超时（{spec.poll_timeout}s）")

    def _archive(self, rec: dict, path: Path) -> None:
        """把刚下载的成品推进对象存储。

        归档失败**不算任务失败**：视频已经在本机了，记下原因下轮补传即可。
        """
        storage = self.spec.storage
        if storage is None or not path.exists():
            return
        key = object_key(
            self.spec.archive_prefix, path.name, batch=self.spec.batch_id,
            prefix=getattr(storage.cfg, "clean_prefix", lambda: "")(),
            digest=file_fingerprint(path),
        )
        try:
            storage.put_path(path, key)
            rec["cos_key"] = key
            rec["cos_url"] = storage.sign_url(key, self.spec.archive_expires)
        except Exception as exc:  # noqa: BLE001 - 归档是附加动作，不能拖垮整批
            rec["archive_error"] = f"{type(exc).__name__}: {exc}"
            self.log(f"[{rec['id']}] 归档失败：{exc}")

    def _verify(self, path: Path) -> dict:
        info = {"size_kb": round(path.stat().st_size / 1024) if path.exists() else 0}
        if info["size_kb"] < 50:
            info["warn"] = "文件过小，疑似下载不完整"
        secs = probe_duration(path)
        if secs is not None:
            info["duration"] = round(secs, 2)
            if abs(secs - self.spec.expect_seconds) > 2.0:
                info["warn"] = f"时长 {secs:.1f}s 与预期 {self.spec.expect_seconds:g}s 不符"
        return info

    def _total_cost(self) -> float:
        total = 0.0
        for rec in self.state.records.values():
            try:
                total += float(rec.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        return round(total, 2)

    def _save_state(self) -> None:
        path = self.spec.out_dir / "batch.json"
        with self._save_lock:  # Windows 下并发替换同一个文件会报占用
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def _export_csv(self) -> None:
        path = self.spec.out_dir / "report.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["模板编号", "状态", "耗时(秒)", "扣费", "文件大小(KB)", "时长(秒)", "错误", "对象Key"])
            for rid in sorted(self.state.records):
                rec = self.state.records[rid]
                verify = rec.get("verify") or {}
                writer.writerow([rid, rec.get("status"), rec.get("seconds"), rec.get("cost"),
                                 verify.get("size_kb"), verify.get("duration"), rec.get("error"),
                                 rec.get("cos_key") or ""])

    def _emit(self, kind: str, **payload) -> None:
        self.on_event({"type": kind, **payload})


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def unique_batch_dir(root: Path, batch_id: str) -> Path:
    """批次号重复时自动追加 -2 / -3，不覆盖历史。"""
    safe = "".join(c for c in batch_id if c.isalnum() or c in "-_") or "batch"
    candidate = root / safe
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = root / f"{safe}-{index}"
        if not candidate.exists():
            return candidate
        index += 1
