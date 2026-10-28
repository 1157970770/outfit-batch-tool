#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""万相 3.0 · 阿里云百炼官方 API 适配层（提交 / 查询 / 状态归一 / 错误归一）。

官方接口（2026-10 文档）：
  POST {base}/services/aigc/video-generation/video-synthesis
       Headers: Authorization: Bearer sk-xxx
                X-DashScope-Async: enable
  GET  {base}/tasks/{task_id}

设计目标：
  · 上层（app/core.py、wan3_r2v.py）只认识 build_body / submit / wait_task；
  · 官方与旧 RunningHub 两套协议收在本文件，便于灰度与回滚；
  · 只用标准库，不引入 dashscope SDK；
  · 官方返回的字段名、状态、错误码全部在这里归一，业务层不碰原始 JSON。

官方文档：
  https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference
  https://help.aliyun.com/zh/model-studio/error-code
  https://help.aliyun.com/zh/model-studio/rate-limit
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PROVIDER_DASHSCOPE = "dashscope"
PROVIDER_RUNNINGHUB = "runninghub"

DASHSCOPE_DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"
RUNNINGHUB_DEFAULT_BASE = "https://www.runninghub.cn/openapi/v2"

SUBMIT_SUFFIX = "/services/aigc/video-generation/video-synthesis"
TASK_SUFFIX = "/tasks/"
RH_SUBMIT_PATH = "/alibaba/wan-3.0/reference-to-video"
RH_QUERY_PATH = "/query"

RESOLUTIONS = ("480P", "720P", "1080P")
RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
MODES = ("edit", "reference")

MAX_IMAGES, MAX_VIDEOS, MAX_AUDIOS = 10, 5, 5
MAX_VIDEO_SEGMENT = 15.0
MAX_VIDEO_TOTAL = 15.0
MAX_INPUT_PLUS_OUTPUT = 30.0
MAX_SEED = 2147483647
MAX_PROMPT = 20000

# 内部任务字段（用户/任务文件使用的名字）
BODY_KEYS = (
    "prompt", "mode", "imageUrls", "videoUrls", "audioUrls", "fileUrl", "linkUrl",
    "negativePrompt", "resolution", "aspectRatio", "duration", "audio", "seed",
    "promptExtend", "watermark",
)
TASK_META_KEYS = ("id", "output", "note")
LEGACY_KEYS = ("webhookUrl",)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"
STATUS_UNKNOWN = "unknown"

TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELED, STATUS_UNKNOWN)

_DASHSCOPE_STATUS = {
    "PENDING": STATUS_PENDING,
    "RUNNING": STATUS_RUNNING,
    "SUCCEEDED": STATUS_SUCCEEDED,
    "FAILED": STATUS_FAILED,
    "CANCELED": STATUS_CANCELED,
    "UNKNOWN": STATUS_UNKNOWN,
}
_RH_STATUS = {
    "QUEUED": STATUS_PENDING,
    "PENDING": STATUS_PENDING,
    "RUNNING": STATUS_RUNNING,
    "SUCCESS": STATUS_SUCCEEDED,
    "SUCCEEDED": STATUS_SUCCEEDED,
    "FAILED": STATUS_FAILED,
    "CANCELED": STATUS_CANCELED,
}


# ---------------------------------------------------------------------------
# 异常 / 结果
# ---------------------------------------------------------------------------

class WanError(RuntimeError):
    """一次可读的 API 错误；category 决定调用方是否重试。"""

    def __init__(self, category: str, message: str, *, code: str = "",
                 request_id: str = "", http_status: int = 0, retryable: bool = False):
        self.category = category
        self.message = message
        self.code = code
        self.request_id = request_id
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(message)

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"[{self.code}]")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        if self.http_status:
            parts.append(f"HTTP {self.http_status}")
        return " / ".join(parts)


class WaitTimeout(WanError):
    """轮询超时：任务可能还在跑，保留 task_id，不要重复提交。"""

    def __init__(self, task_id: str, timeout: int):
        self.task_id = task_id
        super().__init__(
            "timeout",
            f"等待超时（{timeout}s），taskId={task_id} 可能仍在运行；重跑会续查该任务而不重复提交",
            retryable=True,
        )


class TaskExpired(WanError):
    """task_id 超过 24h 或查回 UNKNOWN；结果不可恢复，只能显式重跑。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__("expired", f"任务已过期（task_id 超过 24h 查不到）：{task_id}")


class TaskCanceled(WanError):
    """任务被服务端取消。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__("canceled", f"任务已取消：{task_id}")


@dataclass
class SubmitResult:
    task_id: str
    status: str = STATUS_PENDING
    request_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    status: str
    video_url: str = ""
    usage: dict = field(default_factory=dict)
    code: str = ""
    message: str = ""
    request_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCEEDED and bool(self.video_url)


# ---------------------------------------------------------------------------
# 错误归类
# ---------------------------------------------------------------------------

_AUTH_CODES = {
    "InvalidApiKey", "AccessDenied", "Unauthorized", "InvalidAccessKeyId",
    "SignatureDoesNotMatch", "InvalidApiKeyStatus",
}
_QUOTA_CODES = {
    "Arrearage", "QuotaExhausted", "FreeQuotaExhausted", "CommodityNotPurchased",
    "PrepaidBillOverdue", "PostpaidBillOverdue", "BudgetLimitExceeded",
    "AllocationQuota.FreeTierOnly",
}
_THROTTLE_CODES = {
    "Throttling", "Throttling.BurstRate", "Throttling.AllocationQuota",
    "Throttling.Concurrency", "Throttling.ServiceOverloaded",
    "Throttling.ResourceExhausted", "Requests rate limit exceeded",
    "Request rate increased too quickly", "limit_burst_rate",
}
_INVALID_CODES = {
    "InvalidParameter", "ModelNotFound", "Model not exist", "UnsupportedModel",
    "InvalidTaskId", "InvalidTaskStatus", "DataInspectionFailed",
}
_MODERATION_CODES = {
    "DataInspectionFailed", "InvalidContent", "InternalError.DataInspection",
}
_MEDIA_CODES = {
    "GatewayTimeout.InputDownload", "InputDownloadTimeout",
}


def classify_error(code: str = "", message: str = "", http_status: int = 0,
                   request_id: str = "") -> WanError:
    """把官方错误码/文案压成 (category, 中文提示, 是否可重试)。"""
    code = (code or "").strip()
    message = (message or "").strip()
    lowered = f"{code} {message}".lower()

    def make(category: str, text: str, retryable: bool = False) -> WanError:
        return WanError(category, text, code=code, request_id=request_id,
                        http_status=http_status, retryable=retryable)

    if code in _AUTH_CODES or (http_status in (401, 403) and "throttl" not in lowered):
        return make("auth", f"百炼 Key 无效或无权限：{message or code}。请检查 wan.api_key、地域与业务空间")
    if code in _QUOTA_CODES or "arrearage" in lowered or "balance" in lowered and "insufficient" in lowered:
        return make("quota", f"账户欠费/额度不足：{message or code}。请充值或提高预算后重试")
    if code in _THROTTLE_CODES or http_status == 429 or "rate limit" in lowered or "too many requests" in lowered:
        return make("throttle", f"触发限流：{message or code}。请降低并发/拉长轮询间隔后重试", True)
    if code in _MEDIA_CODES or "failed to download multimodal" in lowered or "download the media resource timed out" in lowered:
        return make("media_fetch", f"官方拉不到素材：{message or code}。请检查该 URL 公网可达/权限；图片可改 Base64，个别模板再考虑 OSS")
    if code in _MODERATION_CODES or "data inspection" in lowered or "moderation" in lowered:
        return make("moderation", f"素材/提示词未通过内容审核：{message or code}")
    if code in _INVALID_CODES or http_status == 400 or "model not exist" in lowered:
        return make("invalid", f"请求参数或模型名不对：{message or code}")
    if http_status >= 500 or code.startswith("InternalError") or "internal error" in lowered:
        return make("server", f"百炼服务端错误：{message or code}。稍后重试", True)
    if "timed out" in lowered or "timeout" in lowered:
        return make("network", f"网络超时：{message or code}", True)
    if http_status == 0:
        return make("network", f"网络错误：{message or code}", True)
    return make("unknown", f"调用失败：{message or code or '未知错误'}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class WanConfig:
    provider: str = PROVIDER_DASHSCOPE
    api_key: str = ""
    base_url: str = ""
    model: str = "wan3.0-video"
    region: str = ""
    default_mode: str = "edit"
    default_duration: int | None = None
    default_ratio: str | None = None
    poll_interval: float = 15.0
    poll_timeout: int = 1800
    max_concurrency: int = 3
    query_qps: float = 10.0
    price_per_second: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.provider = (self.provider or PROVIDER_DASHSCOPE).strip().lower()
        self.api_key = (self.api_key or "").strip()
        self.base_url = (self.base_url or "").strip().rstrip("/")
        self.model = (self.model or "wan3.0-video").strip()
        self.region = (self.region or "").strip()
        self.default_mode = (self.default_mode or "edit").strip().lower()
        if self.provider == PROVIDER_DASHSCOPE and not self.base_url:
            self.base_url = DASHSCOPE_DEFAULT_BASE
        if self.provider == PROVIDER_RUNNINGHUB and not self.base_url:
            self.base_url = RUNNINGHUB_DEFAULT_BASE
        if self.default_mode not in MODES:
            self.default_mode = "edit"
        self.poll_interval = max(1.0, float(self.poll_interval or 15.0))
        self.poll_timeout = max(60, int(self.poll_timeout or 1800))
        self.max_concurrency = max(1, int(self.max_concurrency or 3))
        self.query_qps = max(1.0, float(self.query_qps or 10.0))

    @classmethod
    def from_mapping(cls, cfg: dict) -> "WanConfig":
        """兼容服务端扁平配置（app/config.py 的 load()）与嵌套 wan 块。"""
        wan = cfg.get("wan") if isinstance(cfg.get("wan"), dict) else {}
        price = cfg.get("WAN_PRICE_PER_SECOND") or wan.get("price_per_second") or {}
        if not isinstance(price, dict):
            price = {}
        cleaned = {}
        for key, value in price.items():
            try:
                cleaned[str(key).upper()] = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                continue
        return cls(
            provider=str(cfg.get("WAN_PROVIDER") or wan.get("provider") or PROVIDER_DASHSCOPE),
            api_key=str(cfg.get("WAN_API_KEY") or wan.get("api_key") or ""),
            base_url=str(cfg.get("WAN_BASE_URL") or wan.get("base_url") or ""),
            model=str(cfg.get("WAN_MODEL") or wan.get("model") or "wan3.0-video"),
            region=str(cfg.get("WAN_REGION") or wan.get("region") or ""),
            default_mode=str(cfg.get("WAN_DEFAULT_MODE") or wan.get("default_mode") or "edit"),
            default_duration=cfg.get("WAN_DEFAULT_DURATION", wan.get("default_duration")),
            default_ratio=cfg.get("WAN_DEFAULT_RATIO") or wan.get("default_ratio"),
            poll_interval=float(cfg.get("WAN_POLL_INTERVAL") or wan.get("poll_interval") or 15),
            poll_timeout=int(cfg.get("WAN_POLL_TIMEOUT") or wan.get("poll_timeout") or 1800),
            max_concurrency=int(cfg.get("WAN_MAX_CONCURRENCY") or wan.get("max_concurrency") or 3),
            query_qps=float(cfg.get("WAN_QUERY_QPS") or wan.get("query_qps") or 10),
            price_per_second={k: v for k, v in cleaned.items() if v is not None},
        )

    def validate(self) -> None:
        if self.provider not in (PROVIDER_DASHSCOPE, PROVIDER_RUNNINGHUB):
            raise WanError("invalid", f"未知 provider：{self.provider}（只认 dashscope / runninghub）")
        if not self.api_key:
            raise WanError("auth", "缺少 API Key：服务端没有下发 wan.api_key")
        if not self.base_url.startswith(("http://", "https://")):
            raise WanError("invalid", f"base_url 不是 http(s) 地址：{self.base_url!r}")
        if self.provider == PROVIDER_DASHSCOPE and not self.base_url.rstrip("/").endswith("/api/v1"):
            # 允许自建代理，但提示最常见的漏写
            if "/api/v1" not in self.base_url:
                raise WanError(
                    "invalid",
                    f"base_url 应指向 .../api/v1（收到 {self.base_url!r}）。"
                    "可用 https://dashscope.aliyuncs.com/api/v1 或业务空间专属域名",
                )
        if not self.model:
            raise WanError("invalid", "缺少模型名（wan.model）")


# ---------------------------------------------------------------------------
# 任务校验 + 官方请求体
# ---------------------------------------------------------------------------

def _as_bool(value, field_name: str) -> bool:
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
    raise ValueError(f"{field_name} 需要布尔值，收到 {value!r}")


def _as_int(value, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 需要整数，收到布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{field_name} 需要整数，收到 {value!r}")


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "data:", "oss://"))


def _mode_default(mode: str | None, provider: str, default_mode: str) -> str:
    raw = (mode or default_mode or ("edit" if provider == PROVIDER_DASHSCOPE else "reference")).strip().lower()
    return raw if raw in MODES else "reference"


def validate_task(task: dict, defaults: dict | None = None, *,
                  provider: str = PROVIDER_DASHSCOPE,
                  default_mode: str = "reference",
                  probe: Callable[[str], float | None] | None = None,
                  video_durations: list[float | None] | None = None
                  ) -> tuple[dict, list[str], list[str]]:
    """把任务文件 + defaults 归一成内部体，并做官方硬约束校验。

    返回 (body, errors, warnings)。errors 非空时不要提交。
    body 的键是内部名字（imageUrls/aspectRatio/promptExtend...），
    真正发给官方前用 to_wire_body() 转换。
    """
    defaults = defaults or {}
    errors: list[str] = []
    warnings: list[str] = []

    unknown = set(task) - set(BODY_KEYS) - set(TASK_META_KEYS)
    if unknown:
        errors.append(f"未知字段: {', '.join(sorted(unknown))}（拼错的字段会被服务端静默忽略，这里直接拦下）")
    for key in LEGACY_KEYS:
        if key in task:
            warnings.append(f"{key} 在官方版不再支持（用轮询），已忽略")
            task = {k: v for k, v in task.items() if k != key}

    body: dict[str, Any] = {}
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

    mode = _mode_default(str(body.get("mode") or ""), provider, default_mode)
    body["mode"] = mode

    # prompt
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        errors.append("prompt 必填，且不能为空字符串")
        prompt = prompt if isinstance(prompt, str) else ""
    elif len(prompt) > MAX_PROMPT:
        errors.append(f"prompt 长度 {len(prompt)}，超过官方上限 {MAX_PROMPT} 字符")
    elif len(prompt) > 5000:
        warnings.append(f"prompt 长度 {len(prompt)}，建议精简到 5000 字以内")
    body["prompt"] = prompt.strip()

    # 素材数组
    for key in ("imageUrls", "videoUrls", "audioUrls"):
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
                errors.append(f"{key}[{idx}] 必须是非空字符串（URL / oss:// / 本地路径）")

    videos = body.get("videoUrls") or []
    images = body.get("imageUrls") or []
    audios = body.get("audioUrls") or []

    if len(images) > MAX_IMAGES:
        errors.append(f"imageUrls 最多 {MAX_IMAGES} 张，当前 {len(images)} 张")
    if len(videos) > MAX_VIDEOS:
        errors.append(f"videoUrls 最多 {MAX_VIDEOS} 段，当前 {len(videos)} 段")
    if len(audios) > MAX_AUDIOS:
        errors.append(f"audioUrls 最多 {MAX_AUDIOS} 段，当前 {len(audios)} 段")

    # file / link
    file_url, link_url = body.get("fileUrl"), body.get("linkUrl")
    if file_url and link_url:
        errors.append("fileUrl 与 linkUrl 互斥，只能二选一")
    for field_name, value in (("fileUrl", file_url), ("linkUrl", link_url)):
        if value is not None and not isinstance(value, str):
            errors.append(f"{field_name} 必须是字符串 URL")

    # 模式
    if mode == "edit" and not videos:
        errors.append("mode=edit 需要至少一段参考视频（videoUrls）；只给图片请用 mode=reference")
    if mode == "reference" and not (images or videos or audios or file_url or link_url):
        warnings.append("mode=reference 但没有参考素材，模型会按文生视频处理")

    # 枚举
    resolution = body.get("resolution", "1080P")
    if resolution not in RESOLUTIONS:
        errors.append(f"resolution 只能是 {RESOLUTIONS}，收到 {resolution!r}")
        resolution = "1080P"
    body["resolution"] = resolution

    default_ratio = "adaptive" if mode == "edit" else "9:16"
    ratio = body.get("aspectRatio", default_ratio)
    if ratio not in RATIOS:
        errors.append(f"aspectRatio 只能是 {RATIOS}，收到 {ratio!r}")
        ratio = default_ratio
    body["aspectRatio"] = ratio

    default_duration = -1 if mode == "edit" else 7
    duration = body.get("duration", default_duration)
    if isinstance(duration, str) and duration.strip().lower() == "auto":
        warnings.append('duration="auto" 在官方版对应 -1（智能时长），已自动转换')
        duration = -1
    if isinstance(duration, bool):
        errors.append(f"duration 需要整数，收到 {duration!r}")
        duration = default_duration
    else:
        try:
            duration = _as_int(duration, "duration")
        except ValueError as exc:
            errors.append(str(exc))
            duration = default_duration
    if duration != -1 and not 2 <= duration <= 30:
        errors.append(f"duration 只能是 -1 或 2–30 的整数，收到 {duration}")
        duration = default_duration
    body["duration"] = duration

    # 布尔 / seed
    for field_name, default in (("audio", False), ("promptExtend", False), ("watermark", False)):
        try:
            body[field_name] = _as_bool(body.get(field_name, default), field_name)
        except ValueError as exc:
            errors.append(str(exc))
            body[field_name] = default

    if "seed" in body and body["seed"] is not None:
        try:
            seed = _as_int(body["seed"], "seed")
            if provider == PROVIDER_RUNNINGHUB and seed == -1:
                errors.append("RunningHub 不接受 seed=-1，请用 0–2147483647")
            elif seed != -1 and not 0 <= seed <= MAX_SEED:
                errors.append(f"seed 超范围 [-1, {MAX_SEED}]，收到 {seed}")
            body["seed"] = seed
        except ValueError as exc:
            errors.append(str(exc))
            body.pop("seed", None)

    if (file_url or link_url) and body.get("promptExtend") is False:
        errors.append("传了 fileUrl/linkUrl 时 promptExtend 必须为 true（官方会开深度思考）")

    # 视频时长
    durations: list[float | None]
    if video_durations is not None:
        durations = list(video_durations)
    elif videos and probe is not None:
        durations = []
        for src in videos:
            if _is_url(src):
                durations.append(None)
            else:
                try:
                    durations.append(probe(src))
                except Exception:  # noqa: BLE001 - 探测失败按未知处理
                    durations.append(None)
    else:
        durations = [None] * len(videos)

    known = [d for d in durations if d is not None]
    for idx, secs in enumerate(durations):
        if secs is None:
            continue
        if not 1.0 - 1e-6 <= secs <= MAX_VIDEO_SEGMENT + 1e-6:
            errors.append(f"videoUrls[{idx}] 时长 {secs:.1f}s，单段必须在 1–15 秒内")

    if videos and len(known) == len(videos):
        total = sum(known)
        if total > MAX_VIDEO_TOTAL + 1e-6:
            errors.append(f"参考视频总时长 {total:.1f}s，超过 15 秒上限")
        if duration != -1 and total + duration > MAX_INPUT_PLUS_OUTPUT + 1e-6:
            errors.append(
                f"参考视频 {total:.1f}s + 输出 {duration}s = {total + duration:.0f}s，超过 30 秒上限"
            )
    elif videos and duration != -1 and any(d is None for d in durations):
        if duration > 15:
            warnings.append(
                f"有参考视频但检测不到其时长，输出 {duration}s 时请自行确认「输入+输出 ≤30 秒」"
            )

    # 本地路径提醒（真正上传由 media 层做）
    for key in ("imageUrls", "videoUrls", "audioUrls"):
        for idx, item in enumerate(body.get(key) or []):
            if isinstance(item, str) and not _is_url(item):
                from pathlib import Path
                if not Path(item).exists():
                    warnings.append(f"{key}[{idx}] 本地文件不存在: {item}（提交前需上传或改成 URL）")

    # prompt 指代提示
    if (videos or images or audios) and prompt and not re.search(r"(视频|图|音频)\s*\d+", prompt):
        warnings.append("prompt 里没有「视频1 / 图1 / 音频1」这类指代，模型可能不会用上参考素材")

    return body, errors, warnings


def _media_list(body: dict) -> list[dict]:
    mode = body.get("mode", "reference")
    videos = list(body.get("videoUrls") or [])
    images = list(body.get("imageUrls") or [])
    audios = list(body.get("audioUrls") or [])
    media: list[dict] = []

    # file / link 与参考素材可组合；官方按 type 分别计数
    if body.get("fileUrl"):
        media.append({"type": "file", "url": str(body["fileUrl"])})
    if body.get("linkUrl"):
        media.append({"type": "link", "url": str(body["linkUrl"])})

    if mode == "edit":
        media += [{"type": "reference_video", "url": str(u)} for u in videos]
        media += [{"type": "reference_image", "url": str(u)} for u in images]
    else:
        media += [{"type": "reference_image", "url": str(u)} for u in images]
        media += [{"type": "reference_video", "url": str(u)} for u in videos]
    media += [{"type": "reference_audio", "url": str(u)} for u in audios]
    return media


def to_wire_body(body: dict, provider: str = PROVIDER_DASHSCOPE,
                 model: str = "wan3.0-video") -> dict:
    """内部体 → 真正发给服务端的请求体。"""
    if provider == PROVIDER_RUNNINGHUB:
        out: dict[str, Any] = {"prompt": body.get("prompt", "")}
        for key in ("imageUrls", "videoUrls", "audioUrls"):
            if body.get(key):
                out[key] = list(body[key])
        if body.get("fileUrl"):
            out["fileUrl"] = body["fileUrl"]
        if body.get("linkUrl"):
            out["linkUrl"] = body["linkUrl"]
        out["resolution"] = body.get("resolution", "1080P")
        out["aspectRatio"] = body.get("aspectRatio", "adaptive")
        out["duration"] = str(body.get("duration", 5))
        out["audio"] = bool(body.get("audio", False))
        out["promptExtend"] = bool(body.get("promptExtend", False))
        out["watermark"] = bool(body.get("watermark", False))
        if body.get("seed") is not None:
            out["seed"] = body["seed"]
        return out

    input_obj: dict[str, Any] = {"prompt": body.get("prompt", "")}
    if body.get("negativePrompt"):
        input_obj["negative_prompt"] = str(body["negativePrompt"])
    media = _media_list(body)
    if media:
        input_obj["media"] = media

    parameters: dict[str, Any] = {
        "resolution": body.get("resolution", "1080P"),
        "ratio": body.get("aspectRatio", "adaptive"),
        "duration": int(body.get("duration", 5)),
        "audio": bool(body.get("audio", False)),
        "prompt_extend": bool(body.get("promptExtend", False)),
        "watermark": bool(body.get("watermark", False)),
    }
    if body.get("seed") is not None:
        parameters["seed"] = int(body["seed"])

    return {
        "model": model or "wan3.0-video",
        "input": input_obj,
        "parameters": parameters,
    }


# ---------------------------------------------------------------------------
# HTTP 基础
# ---------------------------------------------------------------------------

def _request_json(method: str, url: str, api_key: str, payload: dict | None = None,
                  headers: dict | None = None, timeout: int = 90) -> dict:
    data = None
    request_headers = {"Authorization": f"Bearer {api_key}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise _error_from_body(raw, int(exc.code)) from None
    except urllib.error.URLError as exc:
        raise WanError("network", f"网络错误：{exc.reason}", retryable=True) from None
    except TimeoutError:
        raise WanError("network", "请求超时", retryable=True) from None

    try:
        data_obj = json.loads(raw)
    except json.JSONDecodeError:
        raise WanError("server", f"响应不是 JSON：{raw[:300]}", http_status=status) from None
    if isinstance(data_obj.get("code"), str) and data_obj.get("code") and not data_obj.get("output"):
        # 官方错误体：{"code","message","request_id"}
        raise _error_from_body(raw, status)
    return data_obj


def _error_from_body(raw: str, status: int) -> WanError:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    code = str(data.get("code") or data.get("errorCode") or "").strip()
    message = str(data.get("message") or data.get("errorMessage") or "").strip()
    request_id = str(data.get("request_id") or data.get("requestId") or "").strip()
    if not message:
        message = raw[:300] or f"HTTP {status}"
    return classify_error(code, message, status, request_id)


def _join(base: str, suffix: str) -> str:
    return f"{base.rstrip('/')}/{suffix.lstrip('/')}"


# ---------------------------------------------------------------------------
# DashScope Provider
# ---------------------------------------------------------------------------

class DashScopeProvider:
    name = PROVIDER_DASHSCOPE

    def __init__(self, cfg: WanConfig, limiter: "_RateLimiter"):
        self.cfg = cfg
        self.limiter = limiter

    def submit(self, body: dict) -> SubmitResult:
        url = _join(self.cfg.base_url, SUBMIT_SUFFIX)
        data = _request_json(
            "POST", url, self.cfg.api_key, body,
            headers={"X-DashScope-Async": "enable"},
            timeout=90,
        )
        output = data.get("output") or {}
        task_id = str(output.get("task_id") or "").strip()
        if not task_id:
            code = str(data.get("code") or "").strip()
            message = str(data.get("message") or "提交未返回 task_id").strip()
            raise classify_error(code, message, 200, str(data.get("request_id") or ""))
        return SubmitResult(
            task_id=task_id,
            status=_DASHSCOPE_STATUS.get(str(output.get("task_status") or "").upper(), STATUS_PENDING),
            request_id=str(data.get("request_id") or ""),
            raw=data,
        )

    def get_task(self, task_id: str) -> TaskResult:
        self.limiter.acquire()
        url = _join(self.cfg.base_url, TASK_SUFFIX + urllib.parse.quote(task_id, safe=""))
        data = _request_json("GET", url, self.cfg.api_key, timeout=45)
        output = data.get("output") or {}
        if not isinstance(output, dict):
            output = {}
        status = _DASHSCOPE_STATUS.get(str(output.get("task_status") or "").upper(), STATUS_UNKNOWN)
        return TaskResult(
            task_id=str(output.get("task_id") or task_id),
            status=status,
            video_url=str(output.get("video_url") or ""),
            usage=data.get("usage") or {},
            code=str(output.get("code") or data.get("code") or ""),
            message=str(output.get("message") or data.get("message") or ""),
            request_id=str(data.get("request_id") or ""),
            raw=data,
        )


# ---------------------------------------------------------------------------
# RunningHub Provider（回滚用）
# ---------------------------------------------------------------------------

class RunningHubProvider:
    name = PROVIDER_RUNNINGHUB

    def __init__(self, cfg: WanConfig, limiter: "_RateLimiter"):
        self.cfg = cfg
        self.limiter = limiter

    def submit(self, body: dict) -> SubmitResult:
        url = _join(self.cfg.base_url, RH_SUBMIT_PATH)
        data = _request_json("POST", url, self.cfg.api_key, body, timeout=90)
        task_id = str(data.get("taskId") or "").strip()
        if not task_id:
            raise classify_error(
                str(data.get("errorCode") or data.get("code") or ""),
                str(data.get("errorMessage") or data.get("msg") or "提交未返回 taskId"),
                200,
            )
        return SubmitResult(
            task_id=task_id,
            status=_RH_STATUS.get(str(data.get("status") or "").upper(), STATUS_PENDING),
            request_id=str(data.get("clientId") or ""),
            raw=data,
        )

    def get_task(self, task_id: str) -> TaskResult:
        self.limiter.acquire()
        url = _join(self.cfg.base_url, RH_QUERY_PATH)
        data = _request_json("POST", url, self.cfg.api_key, {"taskId": task_id}, timeout=45)
        status = _RH_STATUS.get(str(data.get("status") or "").upper(), STATUS_UNKNOWN)
        results = data.get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        return TaskResult(
            task_id=task_id,
            status=status,
            video_url=str(first.get("url") or ""),
            usage=data.get("usage") or {},
            code=str(data.get("errorCode") or ""),
            message=str(data.get("errorMessage") or ""),
            request_id=str(data.get("clientId") or ""),
            raw=data,
        )


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------

class _RateLimiter:
    """最小间隔令牌桶；查询接口官方 RPS 20，默认只用到 10。"""

    def __init__(self, qps: float):
        self.interval = 1.0 / max(1.0, float(qps))
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            time.sleep(wait)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class WanClient:
    """一次批次复用一个实例；线程安全（provider 无共享可变状态，限流器有锁）。"""

    def __init__(self, cfg: WanConfig):
        cfg.validate()
        self.cfg = cfg
        self.limiter = _RateLimiter(cfg.query_qps)
        self.provider = (
            DashScopeProvider(cfg, self.limiter)
            if cfg.provider == PROVIDER_DASHSCOPE
            else RunningHubProvider(cfg, self.limiter)
        )

    # ---- 构造 ----
    def build_body(self, task: dict, defaults: dict | None = None, *,
                   default_mode: str | None = None,
                   probe: Callable[[str], float | None] | None = None,
                   video_durations: list[float | None] | None = None
                   ) -> tuple[dict, list[str], list[str]]:
        mode = default_mode if default_mode is not None else self.cfg.default_mode
        body, errors, warnings = validate_task(
            task, defaults or {},
            provider=self.cfg.provider,
            default_mode=mode,
            probe=probe,
            video_durations=video_durations,
        )
        return body, errors, warnings

    def wire_body(self, body: dict) -> dict:
        return to_wire_body(body, self.cfg.provider, self.cfg.model)

    # ---- 提交 / 查询 ----
    def submit(self, wire_body: dict) -> SubmitResult:
        attempts = 3
        delay = 1.0
        last: WanError | None = None
        for index in range(attempts):
            try:
                return self.provider.submit(wire_body)
            except WanError as exc:
                last = exc
                # 只对明确的限流重试；其他错误（含 5xx）不重试，避免重复建任务
                if exc.category != "throttle" or index == attempts - 1:
                    raise
                time.sleep(delay * (2 ** index) + random.uniform(0, 0.5))
        assert last is not None
        raise last

    def get_task(self, task_id: str) -> TaskResult:
        return self.provider.get_task(task_id)

    def wait_task(self, task_id: str, *, timeout: int | None = None,
                  interval: float | None = None,
                  on_status: Callable[[str], None] | None = None) -> TaskResult:
        timeout = int(timeout if timeout is not None else self.cfg.poll_timeout)
        interval = float(interval if interval is not None else self.cfg.poll_interval)
        deadline = time.time() + timeout
        last_status = ""
        network_failures = 0

        while time.time() < deadline:
            time.sleep(max(0.05, interval * random.uniform(0.8, 1.2)))
            try:
                result = self.get_task(task_id)
            except WanError as exc:
                if exc.category in ("network", "server", "throttle"):
                    network_failures += 1
                    if network_failures >= 5:
                        # 保留 task_id 交给上层记为 running，不要误判为失败
                        raise WaitTimeout(task_id, timeout) from None
                    continue
                raise
            network_failures = 0
            if result.status != last_status:
                last_status = result.status
                if on_status:
                    try:
                        on_status(result.status)
                    except Exception:  # noqa: BLE001 - 回调不能影响主流程
                        pass
            if result.status in TERMINAL_STATUSES:
                return result

        raise WaitTimeout(task_id, timeout)


# ---------------------------------------------------------------------------
# 计费
# ---------------------------------------------------------------------------

def estimate_cost(usage: dict | None, resolution: str,
                  price_per_second: dict[str, float] | None
                  ) -> tuple[float, float | None]:
    """返回 (计费秒数, 预估费用)。官方没有金额，只能用配置单价估算。"""
    usage = usage or {}
    try:
        seconds = float(usage.get("duration") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        try:
            seconds = float(usage.get("output_video_duration") or 0)
        except (TypeError, ValueError):
            seconds = 0.0

    price = None
    prices = price_per_second or {}
    for key in (resolution, f"{usage.get('SR')}P" if usage.get("SR") else "", str(usage.get("SR") or "")):
        if key and key in prices and prices[key]:
            price = float(prices[key])
            break
    return round(seconds, 2), (round(seconds * price, 4) if price else None)
