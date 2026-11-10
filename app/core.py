#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换装批量生产器 · 核心引擎（不依赖界面，可单独测试）。

职责：模板清单 → 任务规划 → 上传服装图 → 并发提交 → 轮询 → 下载 → 校验 → 记账落盘。
界面层只负责调用 run() 并接收事件回调。

API 协议在 app/wan_api.py（官方 DashScope / 旧 RunningHub 双通道），
素材上传在 app/media.py（COS 源目录 / Base64 兜底），本文件只编排。
"""

from __future__ import annotations

import csv
import http.client
import json
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
    from . import traffic
    from .storage import Storage, file_fingerprint, object_key
    from .wan_api import (
        MAX_IMAGES, STATUS_CANCELED, STATUS_FAILED, STATUS_SUCCEEDED, STATUS_UNKNOWN,
        TaskCanceled, TaskExpired, WaitStopped, WaitTimeout, WanClient, WanConfig, WanError,
        estimate_cost,
    )
    from .media import (
        MediaError, SourceUploader, base64_budget_error, describe_media_source,
        resolve_body, resolve_image,
    )
except ImportError:       # 直接 python3 app/core.py 时
    import traffic  # type: ignore
    from storage import Storage, file_fingerprint, object_key  # type: ignore
    from wan_api import (  # type: ignore
        MAX_IMAGES, STATUS_CANCELED, STATUS_FAILED, STATUS_SUCCEEDED, STATUS_UNKNOWN,
        TaskCanceled, TaskExpired, WaitStopped, WaitTimeout, WanClient, WanConfig, WanError,
        estimate_cost,
    )
    from media import (  # type: ignore
        MediaError, SourceUploader, base64_budget_error, describe_media_source,
        resolve_body, resolve_image,
    )

DEFAULT_PROMPT = (
    "编辑视频1：保持视频1中人物的面部、发型、肤色、动作、背景与镜头不变，"
    "仅将人物身上服装替换为图1中无头模特展示的这套服装（版型、颜色、材质、细节一致），"
    "人物头部完整、面部清晰、颈部衔接自然，画面自然真实，布料质感清晰"
)

GROUP_SIZE = 100
TOTAL_TEMPLATES = 500

# 重启/续跑时可接回的记录状态：running = 重启前正在跑；timeout = 本机等超时，
# 但云端 taskId 仍可能有效（24h 内可查），接回只等不重提，避免二次计费。
REATTACHABLE_STATES = ("running", "timeout")


class EngineError(RuntimeError):
    pass


class SubmitUnknown(WanError):
    """提交请求已发出，但没拿到明确响应：云端可能已经建了任务并按秒计费。

    这类结果**不能**当普通失败处理——直接重试可能生成第二条视频、付两次钱。
    单独一个状态，让账目与重试按钮都不把它当成「没花钱」。
    """

    def __init__(self, detail: str):
        super().__init__(
            "unknown",
            f"提交结果未知（云端可能已创建任务并计费，请先到控制台核对，不要直接重试）：{detail}",
        )


# ---------------------------------------------------------------------------
# 模板清单
# ---------------------------------------------------------------------------

def load_manifest(source: str, timeout: int = 15) -> list[dict]:
    """source 可以是 https URL、本地文件路径。返回 [{id, url, key, duration}]。"""
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
        url = (item.get("url") or "").strip()
        key = (item.get("key") or "").strip().lstrip("/")
        if not url and not key:
            raise EngineError(f"模板 {tid} 缺少 url（公有桶）或 key（私有桶）")
        entry = {"id": tid, "url": url, "key": key, "duration": item.get("duration")}
        # 保留可用于"和本地标签清单对齐"的鉴别信息；仅在有值时写入，保持旧行为不变。
        for field in ("name", "shot", "size_mb", "relative_path", "filename", "fingerprint"):
            value = item.get(field)
            if value not in (None, ""):
                entry[field] = value
        out.append(entry)
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
# HTTP / 本地工具
# ---------------------------------------------------------------------------

DOWNLOAD_ATTEMPTS = 4                      # 首次 + 3 次重试
DOWNLOAD_RETRY_DELAYS = (2, 5, 12)         # 秒；断线多为瞬时抖动，退避够用即可


def download_file(url: str, dest: Path, attempts: int = DOWNLOAD_ATTEMPTS) -> None:
    """把成片取回本地，断线自动重试。

    为什么必须重试：下载发生在云端**已经出片并按秒计费之后**。一次 TLS 断流
    （2026-11-10 job b1cc 的 `UNEXPECTED_EOF_WHILE_READING`）就让这条已付费的成片
    丢掉，用户只能去控制台按 taskId 手工找回。抖动的网络重试几次基本都能过。

    每次尝试都从零写 `.part`，不做续传：成片只有十几 MB，重下的代价远小于
    「半个文件 + Range 续传」拼出一个静默损坏的成品。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            if tmp.exists():
                tmp.unlink()
            req = urllib.request.Request(
                url, headers={"User-Agent": "outfit-batch/1.0", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            if tmp.stat().st_size <= 0:
                raise OSError("下载到的文件是空的")
            tmp.replace(dest)
            return
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # 4xx（除 408/429）是确定的拒绝：签名过期、无权限，重试没有意义。
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                break
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last_exc = exc
        if attempt < attempts:
            time.sleep(DOWNLOAD_RETRY_DELAYS[min(attempt - 1, len(DOWNLOAD_RETRY_DELAYS) - 1)])
    try:
        if tmp.exists():
            tmp.unlink()      # 别把半个文件留在输出目录里冒充成片
    except OSError:
        pass
    if last_exc is not None:
        raise last_exc
    raise OSError("下载失败，原因未知")


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


def status_counts(records) -> dict:
    """按 status 汇总记录数。

    非成功终态必须分列：下游只看 success/failed 时，「5 成功 + 5 无效」会被
    报成「全部完成」。running 也单独计数 —— 它表示还在等云端出片；
    等待超时是独立终态 timeout，不混进 running。
    """
    counts = {"total": 0, "success": 0, "failed": 0, "invalid": 0,
              "expired": 0, "canceled": 0, "skipped": 0, "running": 0,
              "unknown": 0, "timeout": 0}
    for record in records:
        counts["total"] += 1
        status = str((record or {}).get("status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def submit_outcome_unknown(exc: WanError) -> bool:
    """提交阶段的错误里，哪些「可能已经在云端建了任务」。

    4xx 是服务端明确拒绝（参数/鉴权/限流），一定没建任务，可以安全重试；
    5xx 与网络中断说明请求可能已送达，云端可能已经建任务并开始计费。
    """
    if exc.http_status >= 500:
        return True
    return exc.category in ("network", "timeout") and not exc.http_status


def _as_seconds(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------

# 6 个固定标签栏位（v0.10.0）：top=上衣 必填、进 COS 归档路径；其余只进对象标签。
# 放在引擎层，网页层（app/web/jobs.py）与归档共用同一份定义。
TAG_SLOTS = (
    ("top", "上衣"),
    ("lining", "内衬"),
    ("pants", "裤子"),
    ("shoes", "鞋子"),
    ("scene", "场景"),
    ("note", "特殊备注"),
)
TAG_SLOT_LABELS = dict(TAG_SLOTS)


@dataclass
class BatchSpec:
    batch_id: str
    image_path: str
    prompt: str
    out_dir: Path
    templates: list[dict]
    api_key: str
    base: str = ""
    provider: str = "dashscope"
    model: str = "wan3.0-video"
    mode: str = "edit"
    resolution: str = "720P"
    duration: str | int | None = None
    aspect_ratio: str | None = None
    concurrency: int = 3
    poll_interval: float = 15.0
    poll_timeout: int = 1800
    expect_seconds: float = 7.0
    # 可选：产物同步到对象存储（None = 只落本地）
    storage: Storage | None = None
    # 可选：模板视频所在桶（清单里写 key 时用它签名）；缺省复用 storage
    template_storage: Storage | None = None
    archive_prefix: str = "output"
    archive_expires: int = 7 * 24 * 3600
    # 款号（每批次一个）：写进对象路径与元数据/标签，方便在 COS 里按款找片
    style_no: str = ""
    # 6 栏位标签（top/lining/pants/shoes/scene/note）：v0.10.0 起，路径取 top；style_no 仅老数据兜底
    tags: dict = field(default_factory=dict)
    # 服装图源目录：默认 {COS_PREFIX}/output/source
    source_prefix: str = ""
    source_expires: int = 7 * 24 * 3600
    price_per_second: dict = field(default_factory=dict)
    # 网页多批次共享全局在飞额度（submit → wait 全程占用）；桌面默认 None，不改变原行为。
    inflight_semaphore: threading.Semaphore | None = None
    # 兼容旧调用：只钳提交阶段。新代码请用 inflight_semaphore。
    submit_semaphore: threading.Semaphore | None = None
    # 服务重启接回：{template_id: 重启前那条记录}。
    #   status=success  → 不再跑，沿用旧记录（避免二次计费/重复下载）
    #   status=running 且带 task_id → 不提交，接着等那个 taskId
    #   没有记录 / pending → 正常提交
    resume: dict = field(default_factory=dict)
    # 多图：全部图片按槽位顺序（含定妆图）。为空时回退 image_path（桌面/CLI 老调用）。
    # 必须放在末尾：image_path 等前排字段无默认值，中间插带默认值的字段会报 TypeError。
    image_paths: list[str] = field(default_factory=list)

    def image_sources(self) -> list[str]:
        """传给模型的图片顺序：images[0]=图1 定妆，images[1..]=图2…图10。"""
        if self.image_paths:
            return list(self.image_paths)
        return [self.image_path] if self.image_path else []


@dataclass
class BatchState:
    spec: BatchSpec
    records: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            billable = 0.0
            estimated = 0.0
            for rec in self.records.values():
                try:
                    billable += float(rec.get("billable_seconds") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    estimated += float(rec.get("estimated_cost") or 0)
                except (TypeError, ValueError):
                    pass
            return {
                "batch_id": self.spec.batch_id,
                "started": getattr(self, "started", ""),
                "provider": self.spec.provider,
                "model": self.spec.model,
                "mode": self.spec.mode,
                "concurrency": self.spec.concurrency,
                "prompt": self.spec.prompt,
                "billable_seconds": round(billable, 2),
                "estimated_cost": round(estimated, 2) if estimated else None,
                "records": list(self.records.values()),
            }


class BatchRunner:
    """一条批次 = 一个实例。run() 阻塞，直到跑完 / 被停止。"""

    def __init__(self, spec: BatchSpec, on_event=None, log=None):
        self.spec = spec
        # P2 回调隔离：jobs._on_event / _append_log 若抛 OSError（盘满、路径失效），
        # 会被上层的兜底 except 误判成「该条 failed」，甚至打断整批。回调异常只吞掉。
        event_cb = on_event or (lambda evt: None)
        log_cb = log or (lambda msg: None)

        def _safe_event(evt):
            try:
                event_cb(evt)
            except Exception:  # noqa: BLE001 - 回调不能影响主流程
                pass

        def _safe_log(msg):
            try:
                log_cb(msg)
            except Exception:  # noqa: BLE001
                pass

        self.on_event = _safe_event
        self.log = _safe_log
        self.pause_event = threading.Event()
        self.pause_event.set()          # set = 运行中
        self.stop_event = threading.Event()
        self.state = BatchState(spec)
        self.state.started = time.strftime("%Y-%m-%d %H:%M:%S")
        self._image_url: str | None = None      # 兼容老调用与测试手写注入
        self._image_urls: list[str] = []        # 多图真源，顺序=槽位=imageUrls
        self._upload_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._media_cache: dict = {}
        self._client = WanClient(self._wan_config())
        # 本地目录后端只用于测试/离线演练，签名出来是 file://，官方拉不到；
        # 这种场景直接走 Base64，避免把 file:// 塞进请求体。
        remote_storage = (
            spec.storage is not None
            and str(getattr(spec.storage.cfg, "provider", "")).lower() != "local"
        )
        self._uploader = (
            SourceUploader(
                spec.storage, spec.source_prefix,
                expires=spec.source_expires, log=self.log,
            )
            if remote_storage else None
        )

    def _wan_config(self) -> WanConfig:
        spec = self.spec
        return WanConfig(
            provider=spec.provider,
            api_key=spec.api_key,
            base_url=spec.base,
            model=spec.model,
            default_mode=spec.mode,
            default_duration=spec.duration,
            default_ratio=spec.aspect_ratio,
            poll_interval=spec.poll_interval,
            poll_timeout=spec.poll_timeout,
            max_concurrency=spec.concurrency,
            price_per_second=spec.price_per_second,
        )

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

        total = len(spec.templates)
        # 重启接回：分三类，只有第一类要跑。
        #   ① 没有记录 / pending：从没提交过 → 正常提交
        #   ② running / timeout 且带 task_id：重启前已提交（或本机等超时）→ 接回原云端
        #      任务，绝不重提；timeout 的 taskId 在 24h 内仍然有效
        #   ③ 其它（success/failed/invalid/expired/canceled/unknown…）：已经是定论，沿用旧记录
        pending_templates = []
        need_submit = False
        kept = 0
        for tpl in spec.templates:
            prior = (spec.resume or {}).get(str(tpl["id"])) or {}
            prev_status = str(prior.get("status") or "")
            reattach = prev_status in REATTACHABLE_STATES and bool(prior.get("task_id"))
            if not prior or prev_status in ("", "pending"):
                pending_templates.append(tpl)
                need_submit = True
                continue
            if reattach:
                pending_templates.append(tpl)
                continue
            keep = dict(prior)
            if prev_status == "running":
                # 重启前正在跑却没记下 taskId：接不回来，也不能重提（云端可能已计费），
                # 落在 unknown（不可一键重试），提示去控制台核对。
                keep["status"] = "unknown"
                keep["error"] = ("服务重启时这条正在运行，但没有可核对的 taskId；"
                                 "请到控制台核对后再决定是否重试。")
            with self.state.lock:
                self.state.records[tpl["id"]] = keep
            kept += 1

        # 纯接回的批次不需要再传定妆图/重签模板：那两件事只服务「要提交」的条目，
        # 一旦图片被清理/尺寸不合规，不该因为这个把接回也一起拖死。
        if pending_templates:
            self._prepare_templates()
            if need_submit:
                self._ensure_image()
        self._save_state()

        done = kept
        self._emit("started", total=total)
        if kept:
            # 先把进度条回填到重启前的样子，页面上立刻能看到「已好 N 条」。
            self._emit("progress", done=done, total=total, record=None)
            self.log(f"重启接回：{kept} 条沿用旧记录，剩余 {len(pending_templates)} 条继续")

        with ThreadPoolExecutor(max_workers=max(1, spec.concurrency)) as pool:
            futures = {pool.submit(self._run_one, tpl): tpl for tpl in pending_templates}
            for fut in as_completed(futures):
                tpl = futures[fut]
                try:
                    rec = fut.result()
                except Exception as exc:  # 单条异常不影响整批
                    rec = {"id": tpl["id"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                with self.state.lock:
                    self.state.records[tpl["id"]] = rec
                done += 1
                self._save_state()
                self._emit("progress", done=done, total=total, record=rec)

        self._export_csv()
        self._save_state()
        counts = status_counts(self.state.records.values())
        summary = dict(counts)
        summary.update({
            "total": total,
            "unsuccessful": max(0, total - counts["success"]),
            "billable_seconds": round(sum(
                float(r.get("billable_seconds") or 0) for r in self.state.records.values()
            ), 2),
            "estimated_cost": self._total_estimated_cost(),
            "cost": self._total_estimated_cost(),  # 兼容旧界面字段
            "out_dir": str(spec.out_dir),
            "provider": spec.provider,
            "model": spec.model,
            "archived": sum(1 for r in self.state.records.values() if r.get("cos_key")),
            "archive_failed": sum(1 for r in self.state.records.values() if r.get("archive_error")),
        })
        self._emit("finished", summary=summary)
        return summary

    def _prepare_templates(self) -> None:
        """清单里写的是对象 key（私有桶）时，开工前统一签成有效 URL。"""
        pending = [t for t in self.spec.templates if not t.get("url") and t.get("key")]
        if not pending:
            return
        signer = self.spec.template_storage or self.spec.storage
        if signer is None:
            raise EngineError(
                "模板清单用的是 COS 对象 key，需要签名才能给模型访问，"
                "但当前没有启用 COS。"
            )
        self.log(f"为 {len(pending)} 个模板生成签名链接（24 小时有效）")
        for tpl in pending:
            try:
                tpl["url"] = signer.sign_url(tpl["key"], 24 * 3600)
            except Exception as exc:  # noqa: BLE001
                raise EngineError(f"模板 {tpl['id']} 签名失败：{exc}") from None

    def _ensure_image(self) -> None:
        """开工前把全部槽位图片解析成 URL / Base64，任一张失败即整批失败。

        为什么整批失败而不是跳过：图是整批共用素材，少一张会让 prompt 里的
        「图3…图10」整体错位，产出的视频内容是错的却照常计费。这里在线程池
        之前调用，失败时 job 直接 failed，一条都不会提交。
        """
        with self._upload_lock:
            if self._image_urls:
                return
            # 测试用 object.__new__ 手工注入 _image_url（单图老路径），沿用不重传。
            if self._image_url:
                self._image_urls = [self._image_url]
                return
            paths = self.spec.image_sources()
            if not paths:
                raise EngineError("缺少定妆图")
            if len(paths) > MAX_IMAGES:
                raise EngineError(f"图片最多 {MAX_IMAGES} 张（1 张定妆图 + 9 张细节图）。")
            budget = base64_budget_error(paths, cos_enabled=self._uploader is not None)
            if budget:
                raise EngineError(budget)
            urls: list[str] = []
            # 记下每张图「会从 COS 被万相拉走」的字节数：inline(base64) 的不产生
            # 外网流量，记为 0；COS 上的就是本地文件大小（上传没有改动字节）。
            sizes: list[int] = []
            for index, path in enumerate(paths, 1):
                if not Path(path).is_file():
                    raise EngineError(f"第 {index} 张图片已丢失：{Path(path).name}")
                self.log(f"准备第 {index}/{len(paths)} 张图片：{Path(path).name}")
                try:
                    url = resolve_image(path, self._uploader)
                except MediaError as exc:
                    raise EngineError(f"第 {index} 张图片处理失败：{exc}") from None
                urls.append(url)
                sizes.append(int(Path(path).stat().st_size) if url.startswith("http") else 0)
            self._image_urls = urls
            self._image_bytes = sizes
            self._image_url = urls[0]
            self.log(f"{len(urls)} 张图片已就绪（{describe_media_source(urls[0])}），"
                     f"{len(self.spec.templates)} 条任务共用")

    def _run_one(self, tpl: dict) -> dict:
        spec = self.spec
        tid = tpl["id"]
        out_file = spec.out_dir / f"{tid}.mp4"
        template_seconds = _as_seconds(tpl.get("duration"))
        rec = {"id": tid, "template": tpl["url"], "status": "pending",
               "provider": spec.provider, "model": spec.model, "mode": spec.mode,
               "resolution": spec.resolution, "file": str(out_file),
               "billable_seconds": None, "estimated_cost": None, "cost": None,
               "seconds": None, "error": None}

        self.pause_event.wait()
        if self.stop_event.is_set():
            rec["status"] = "skipped"
            return rec

        # 重启接回：这条在重启前已经提交过（有 task_id），云端任务可能还在跑/已出片。
        # 绝不能重新提交——那就是同一条模板二次计费。直接接着等那条 taskId。
        # timeout 也走这条路：等待超时≠云端失败，同一条 taskId 仍然有效（24h 内可查）。
        prior = (spec.resume or {}).get(str(tid)) or {}
        reattach = ""
        if str(prior.get("status") or "") in REATTACHABLE_STATES and prior.get("task_id"):
            reattach = str(prior["task_id"])
            rec.update({
                "task_id": reattach,
                "request_id": prior.get("request_id") or "",
                "submitted_at": prior.get("submitted_at") or "",
                "task_expires_at": prior.get("task_expires_at") or 0,
                "resumed": True,
            })
            self.log(f"[{tid}] 接回{'超时' if prior.get('status') == 'timeout' else '重启前'}"
                     f"的云端任务 taskId={reattach}（不重新提交）")

        body, errors, warnings = {}, [], []
        if not reattach:
            task = {
                "id": tid,
                "mode": spec.mode,
                "prompt": spec.prompt,
                # 多图原样按槽位顺序下发；getattr 兜住测试里手工注入 _image_url 的 runner。
                "imageUrls": list(getattr(self, "_image_urls", None)
                                  or ([self._image_url] if self._image_url else [])),
                "videoUrls": [tpl["url"]],
                "resolution": spec.resolution,
                "audio": False,
                "promptExtend": False,
                "watermark": False,
            }
            if spec.duration is not None:
                task["duration"] = spec.duration
            if spec.aspect_ratio:
                task["aspectRatio"] = spec.aspect_ratio

            body, errors, warnings = self._client.build_body(
                task, video_durations=[template_seconds], default_mode=spec.mode,
            )
            for msg in warnings:
                self.log(f"[{tid}] 提示：{msg}")
            if errors:
                rec["status"] = "invalid"
                rec["error"] = "; ".join(errors)
                return rec

        started = time.time()
        # 全局在飞额度：submit → wait 全程占位，保证跨 job 的「同时运行任务数」有上限。
        # 只有线程池在开会话的话，100 个线程就会同时压 100 条云端任务。
        gate = spec.inflight_semaphore or spec.submit_semaphore
        try:
            if reattach:
                # 接回路径不需要组包/再传图：云端那条任务已经收下并计费了。
                wire = None
            else:
                # 桌面链路里 image/video 通常已是 URL；这里兜底处理本地路径
                body = resolve_body(body, self._uploader, cache=self._media_cache)
                wire = self._client.wire_body(body)
            if gate is not None:
                with gate:
                    final = self._submit_and_wait(wire, rec, reattach=reattach)
            else:
                final = self._submit_and_wait(wire, rec, reattach=reattach)
            if final is None:      # 提交前已被停止
                return rec
            if final.status == STATUS_UNKNOWN:
                raise TaskExpired(final.task_id)
            if final.status == STATUS_CANCELED:
                raise TaskCanceled(final.task_id)
            if final.status == STATUS_FAILED:
                raise EngineError(
                    f"任务失败 [{final.code or 'UNKNOWN'}] {final.message or '无错误信息'}"
                    + (f" request_id={final.request_id}" if final.request_id else "")
                )
            if final.status != STATUS_SUCCEEDED or not final.video_url:
                raise EngineError(f"任务状态 {final.status} 但没有结果地址")

            # 记账必须放在下载**之前**：云端已经出片并按秒计费，下载失败不能让这笔账凭空消失。
            usage = final.usage or {}
            billable, estimated = estimate_cost(usage, spec.resolution, spec.price_per_second)
            rec.update({
                "request_id": final.request_id or rec.get("request_id", ""),
                "usage": usage,
                "billable_seconds": billable,
                "estimated_cost": estimated,
                "seconds": round(time.time() - started, 1),
            })
            try:
                download_file(final.video_url, out_file)
            except Exception as exc:  # noqa: BLE001 - 账已记下，下载失败要如实标出来
                rec["status"] = "failed"
                rec["download_failed"] = True
                rec["error"] = (
                    f"云端已出片并按 {billable:g} 秒计费，但下载失败："
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                rec["status"] = "success"
                try:
                    rec["verify"] = self._verify(out_file, template_seconds)
                except Exception as exc:  # noqa: BLE001 - 校验只是附加信息
                    rec["verify_error"] = f"{type(exc).__name__}: {exc}"
                self._archive(rec, out_file)
        except SubmitUnknown as exc:
            rec["status"] = "unknown"
            rec["error"] = str(exc)
        except WaitStopped as exc:
            # 本机不再等，云端那条任务仍在跑：结果未知，且**不能**允许一键重试，
            # 否则同一条模板会被二次提交、二次计费。落在 unknown（不可重试）而不是 canceled。
            rec["status"] = "unknown"
            rec["stopped"] = True
            rec["error"] = str(exc)
        except WaitTimeout as exc:
            # 终态 timeout（P1-19）：等待超时≠还在跑，云端大概率已出片并计费。
            # 保留 task_id 供控制台核对，纳入重试白名单；不再落 running 永久卡死。
            rec["status"] = "timeout"
            rec["error"] = str(exc)
        except TaskExpired as exc:
            rec["status"] = "expired"
            rec["error"] = str(exc)
        except TaskCanceled as exc:
            rec["status"] = "canceled"
            rec["error"] = str(exc)
        except WanError as exc:
            rec["status"] = "failed"
            rec["error"] = str(exc)
            if exc.request_id:
                rec["request_id"] = exc.request_id
        except (EngineError, MediaError) as exc:
            rec["status"] = "failed"
            rec["error"] = str(exc)
        except Exception as exc:  # 单个任务炸掉不影响整批
            rec["status"] = "failed"
            rec["error"] = f"{type(exc).__name__}: {exc}"

        with self.state.lock:
            self.state.records[tid] = rec
        self._save_state()
        return rec

    def _note_egress(self, tid: str) -> None:
        """记一笔「这条任务即将让万相来拉素材」的推定流量。

        真正的拉取发生在阿里云侧，本机看不到字节，只能按对象真实大小估：
        模板体积来自清单里的 ``size_mb``（catalog 已带进 ``spec.templates``），
        图片体积是上传前的本地文件大小（COS 上传不改字节，base64 内联不产生外网流量，
        记为 0）。这些数字要和 ``preview`` 的实测值分开看，见 app/traffic.py。
        """
        if not traffic.is_enabled():
            return
        try:
            size = 0
            for tpl in self.spec.templates:
                if str(tpl.get("id")) == str(tid):
                    size = int(round(float(tpl.get("size_mb") or 0) * 1024 * 1024))
                    break
            traffic.record("template", size, tid=tid, job=self.spec.batch_id)
        except Exception:  # noqa: BLE001 - 观测失败不能影响生产
            pass
        try:
            sizes = list(getattr(self, "_image_bytes", None) or [])
            traffic.record("image", sum(sizes), n=len(sizes), job=self.spec.batch_id)
        except Exception:  # noqa: BLE001
            pass

    def _submit_and_wait(self, wire: dict, rec: dict, reattach: str = ""):
        """提交并等待单条任务；调用方负责在飞额度（进入前已占位）。

        reattach 非空 = 服务重启后接回云端已有的 taskId：**不提交**，只接着等结果，
        避免同一条模板被二次提交、二次计费。

        返回终态 TaskResult；提交前就被停止则返回 None（rec 已标 skipped）。
        """
        spec = self.spec
        tid = rec["id"]
        if self.stop_event.is_set():
            rec["status"] = "skipped"
            return None
        if reattach:
            rec["status"] = "running"
            rec["resumed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with self.state.lock:
                self.state.records[tid] = dict(rec)
            self._save_state()
            self._emit("update", record=dict(rec))
            return self._client.wait_task(
                reattach, timeout=spec.poll_timeout, interval=spec.poll_interval,
                on_status=lambda status: self.log(f"[{tid}] {status}"),
                should_stop=self.stop_event.is_set,
            )
        try:
            self._note_egress(tid)   # 只记账：提交那一刻，素材才真正被万相拉取
            resp = self._client.submit(wire)
        except WanError as exc:
            # 请求可能已送达云端：结果未知，既不能当失败重试，也不能当没花钱。
            if submit_outcome_unknown(exc):
                raise SubmitUnknown(str(exc)) from None
            raise
        rec["task_id"] = resp.task_id
        rec["request_id"] = resp.request_id
        rec["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["task_expires_at"] = int(time.time()) + 24 * 3600
        rec["status"] = "running"
        with self.state.lock:
            self.state.records[tid] = dict(rec)
        self._save_state()
        self.log(f"[{tid}] 已提交 taskId={resp.task_id} status={resp.status}")
        self._emit("update", record=dict(rec))
        return self._client.wait_task(
            resp.task_id, timeout=spec.poll_timeout, interval=spec.poll_interval,
            on_status=lambda status: self.log(f"[{tid}] {status}"),
            # 点了取消就尽快放手：云端那条任务收不回来，但本机不必再占着在飞名额陪跑到超时。
            should_stop=self.stop_event.is_set,
        )

    def recheck_timeouts(self, records, on_update=None) -> list[dict]:
        """按 task_id 复查「等待超时」的记录：只查询云端，绝不重新提交。

        为什么需要：等待超时只说明本机没在 30 分钟内等到结果，云端那条任务
        往往还在跑、甚至已经出片并按秒计费。直接「重试」= 重新提交 = 二次计费，
        正确做法是拿原 taskId 问一次云端，再决定要不要补下载。

        每条记录的结果：
          - succeeded      → 下载 + 补记账（billable/estimated）+ COS 归档 → success
          - failed/canceled→ 落对应终态；云端已判死，之后重试不会重复计费
          - unknown        → expired（云端查不到，超过 24h 或已清理）
          - pending/running→ 保持 timeout，只更新 checked_at / cloud_status，不占在飞额度
          - 查询失败       → 保持 timeout，记下原因，稍后可再点一次

        返回更新后的记录副本列表；on_update(rec) 每处理完一条回调一次（用于边跑边刷页面）。
        """
        spec = self.spec
        out_dir = spec.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        durations = {str((tpl or {}).get("id")): _as_seconds((tpl or {}).get("duration"))
                     for tpl in (spec.templates or [])}
        updated: list[dict] = []
        for item in records or []:
            rec = dict(item)
            tid = rec.get("id")
            task_id = str(rec.get("task_id") or "")
            rec["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if not task_id:
                rec["cloud_status"] = "no_task_id"
                rec["error"] = "这条没有 taskId，无法向云端复查；只能重新提交（会二次计费）"
                self._recheck_emit(updated, rec, on_update)
                continue
            try:
                final = self._client.get_task(task_id)
            except WanError as exc:
                rec["cloud_status"] = f"error:{exc.category}"
                rec["error"] = (f"复查失败（{exc.category}）：{exc}。云端任务可能仍在运行，"
                                "稍后再点「复查超时任务」")
                self._recheck_emit(updated, rec, on_update)
                continue
            rec["cloud_status"] = final.status
            rec["request_id"] = final.request_id or rec.get("request_id", "")
            if final.status == STATUS_SUCCEEDED and final.video_url:
                self._recheck_success(rec, final, out_dir / f"{tid}.mp4",
                                      durations.get(str(tid)))
            elif final.status in (STATUS_FAILED, STATUS_CANCELED, STATUS_UNKNOWN):
                rec["status"] = {
                    STATUS_FAILED: "failed",
                    STATUS_CANCELED: "canceled",
                    STATUS_UNKNOWN: "expired",
                }[final.status]
                rec["error"] = (
                    f"云端已判定：{final.status}"
                    + (f" [{final.code}]" if final.code else "")
                    + (f" {final.message}" if final.message else "")
                    + "；现在可以安全重试，不会重复计费"
                )
            else:
                rec["status"] = "timeout"
                rec["error"] = (f"云端仍在运行（taskId={task_id}，状态 {final.status}）；"
                                "已复查但还没有结果，稍后再点一次「复查超时任务」")
            self._recheck_emit(updated, rec, on_update)
        return updated

    @staticmethod
    def _recheck_emit(updated: list[dict], rec: dict, on_update) -> None:
        updated.append(rec)
        if on_update is not None:
            try:
                on_update(dict(rec))
            except Exception:  # noqa: BLE001 - 回调不能打断复查
                pass

    def _recheck_success(self, rec: dict, final, out_file: Path, template_seconds) -> None:
        """云端已出片：补记账 + 补下载 + 归档，把记录翻成 success。"""
        usage = final.usage or {}
        billable, estimated = estimate_cost(usage, self.spec.resolution,
                                           self.spec.price_per_second)
        rec.update({
            "usage": usage,
            "billable_seconds": billable,
            "estimated_cost": estimated,
            "file": str(out_file),
            "error": None,
        })
        try:
            if not (out_file.is_file() and out_file.stat().st_size > 0):
                download_file(final.video_url, out_file)
        except Exception as exc:  # noqa: BLE001 - 云端已计费，下载失败要如实标出来
            # 保持 timeout：再点一次「复查超时任务」会重新查到这个 taskId 并补下载，
            # 不会重新提交、不会二次计费。
            rec["status"] = "timeout"
            rec["download_failed"] = True
            rec["error"] = (f"云端已出片并按 {billable:g} 秒计费，但下载失败："
                            f"{type(exc).__name__}: {exc}；可再点一次「复查超时任务」重试下载")
            return
        rec["status"] = "success"
        rec["download_failed"] = False
        try:
            rec["verify"] = self._verify(out_file, template_seconds)
        except Exception as exc:  # noqa: BLE001 - 校验只是附加信息
            rec["verify_error"] = f"{type(exc).__name__}: {exc}"
        self._archive(rec, out_file)

    def _archive(self, rec: dict, path: Path) -> None:
        """把刚下载的成品推进对象存储。

        归档失败**不算任务失败**：视频已经在本机了，记下原因下轮补传即可。
        """
        storage = self.spec.storage
        if storage is None or not path.exists():
            return
        spec_tags = {str(k): str(v) for k, v in (getattr(self.spec, "tags", None) or {}).items()
                     if str(v).strip()}
        # 路径段：新标签取「上衣」，老数据退回 style_no；都没有则路径里没有款号段。
        style = spec_tags.get("top") or str(getattr(self.spec, "style_no", "") or "").strip()
        key = object_key(
            self.spec.archive_prefix, path.name, batch=self.spec.batch_id,
            prefix=storage.cfg.clean_prefix(),
            digest=file_fingerprint(path),
            style=style,
        )
        meta = {"batch": self.spec.batch_id}
        if style:
            meta["style-no"] = style   # x-cos-meta 只收 ASCII；中文标签走下面的 tagging
        try:
            storage.put_path(path, key, meta=meta)
            rec["cos_key"] = key
            rec["cos_url"] = storage.sign_url(key, self.spec.archive_expires)
            # 对象标签：6 栏位（中文键值对，COS 标签原生支持）+ 批次号；老数据退回 style_no。
            cos_tags: dict = {}
            for slot, value in spec_tags.items():
                cos_tags[TAG_SLOT_LABELS.get(slot, slot)] = value
            if not cos_tags and style:
                cos_tags = {"款号": style}
            if cos_tags and not getattr(self, "_tags_unavailable", False):
                # 标签是可选能力：账号没开通 cos:PutObjectTagging 时这里会失败，
                # 只记一条警告，不影响出片（路径与元数据仍然带款号）。
                try:
                    storage.tag_object(key, {**cos_tags, "batch": self.spec.batch_id})
                    rec["cos_tags"] = cos_tags
                except Exception as exc:  # noqa: BLE001
                    rec["tag_warning"] = f"{type(exc).__name__}: {exc}"[:200]
                    self._tags_unavailable = True   # 一批里只提示一次，避免每条都刷日志
                    self.log(f"[{rec['id']}] 对象标签未写入（不影响出片，本批不再重试）：{exc}")
        except Exception as exc:  # noqa: BLE001 - 归档是附加动作，不能拖垮整批
            rec["archive_error"] = f"{type(exc).__name__}: {exc}"
            self.log(f"[{rec['id']}] 归档失败：{exc}")

    def _verify(self, path: Path, expected: float | None = None) -> dict:
        info = {"size_kb": round(path.stat().st_size / 1024) if path.exists() else 0}
        if info["size_kb"] < 50:
            info["warn"] = "文件过小，疑似下载不完整"
        secs = probe_duration(path)
        if secs is not None:
            info["duration"] = round(secs, 2)
            target = expected if expected is not None else self.spec.expect_seconds
            if target and abs(secs - target) > 2.0:
                info["warn"] = f"时长 {secs:.1f}s 与预期 {target:g}s 不符"
        return info

    def _total_estimated_cost(self) -> float | None:
        total = 0.0
        found = False
        for rec in self.state.records.values():
            value = rec.get("estimated_cost")
            if value is None:
                continue
            found = True
            try:
                total += float(value)
            except (TypeError, ValueError):
                pass
        return round(total, 2) if found else None

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
            writer.writerow([
                "模板编号", "状态", "模式", "耗时(秒)", "计费秒数", "输出时长(秒)",
                "分辨率", "预估费用", "文件大小(KB)", "实测时长(秒)", "错误",
                "对象Key", "request_id",
            ])
            for rid in sorted(self.state.records):
                rec = self.state.records[rid]
                verify = rec.get("verify") or {}
                usage = rec.get("usage") or {}
                writer.writerow([
                    rid, rec.get("status"), rec.get("mode"), rec.get("seconds"),
                    rec.get("billable_seconds"), usage.get("output_video_duration"),
                    rec.get("resolution"), rec.get("estimated_cost"),
                    verify.get("size_kb"), verify.get("duration"), rec.get("error"),
                    rec.get("cos_key") or "", rec.get("request_id") or "",
                ])

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
