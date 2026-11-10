#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自由视频 tab：选项下发与请求校验。

这一层不碰文件系统、不碰网络，只把「前端提交的 JSON」规整成引擎能吃的字段，
顺便把官方硬约束（时长 / 素材数量 / 比例 / 时长叠加）挡在建任务之前。
真正的二次校验仍由 `app/wan_api.validate_task()` 做——两边口径故意保持同源：
这里的常量全部从 wan_api 导入，避免以后官方改了参数、这里悄悄过期。
"""
from __future__ import annotations

from datetime import datetime

from app.wan_api import (MAX_AUDIOS, MAX_IMAGES, MAX_INPUT_PLUS_OUTPUT,
                         MAX_SEED, MAX_VIDEOS, MAX_VIDEO_TOTAL, MODES,
                         RATIOS, RESOLUTIONS)

#: 一次最多生成几个分镜（每个分镜都是独立计费的云端任务）
MAX_COUNT = 4
#: 提示词上限与官方一致（web 端不再额外收紧，长提示词是视频生产的常态）
PROMPT_LIMIT = 20000
#: 反向提示词上限（官方没有明确数值，按经验给）
NEGATIVE_LIMIT = 2000

MODES_UI = (
    ("auto", "自动判断", "有参考视频就按视频编辑，没有就按参考生视频"),
    ("reference", "参考生视频", "参考图 / 参考视频 / 参考音频 → 新视频；无素材即文生视频"),
    ("edit", "视频编辑", "在原视频上改风格 / 换主体，必须至少一段参考视频"),
)

RESOLUTION_LABELS = {
    "1080P": "1080P（最清晰）",
    "720P": "720P（均衡）",
    "480P": "480P（最快）",
}

RATIO_LABELS = {
    "16:9": "16:9 横屏",
    "9:16": "9:16 竖屏",
    "4:3": "4:3 横屏",
    "3:4": "3:4 竖屏",
    "1:1": "1:1 方形",
    "21:9": "21:9 宽银幕",
    "adaptive": "跟随参考视频（仅视频编辑）",
}

DURATION_CHOICES = (
    (5, "5 秒"),
    (7, "7 秒"),
    (10, "10 秒"),
    (15, "15 秒"),
    (-1, "自动（视频编辑智能时长）"),
)


class VideoGenError(ValueError):
    """可直接展示给用户的生产错误；status 决定 HTTP 码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


def now_text() -> str:
    """本机时间（与网页端其它列表一致的展示格式）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def options(*, configured: bool, reason: str, model: str, provider: str,
            cos_enabled: bool) -> dict:
    """下发给前端的全部选项与限制。"""
    return {
        "configured": bool(configured),
        "reason": reason,
        "model": model,
        "provider": provider,
        "cos_enabled": bool(cos_enabled),
        "modes": [{"value": value, "label": label, "hint": hint}
                  for value, label, hint in MODES_UI],
        "resolutions": [{"value": key, "label": RESOLUTION_LABELS.get(key, key)}
                        for key in RESOLUTIONS],
        "ratios": [{"value": key, "label": RATIO_LABELS.get(key, key)} for key in RATIOS],
        "durations": [{"value": value, "label": label} for value, label in DURATION_CHOICES],
        "defaults": {
            "mode": "auto",
            "resolution": "1080P",
            "ratio": "16:9",
            "duration": 5,
            "count": 1,
            "prompt_extend": True,
            "watermark": False,
            "audio": False,
        },
        "limits": {
            "max_count": MAX_COUNT,
            "max_images": MAX_IMAGES,
            "max_videos": MAX_VIDEOS,
            "max_audios": MAX_AUDIOS,
            "prompt_limit": PROMPT_LIMIT,
            "negative_limit": NEGATIVE_LIMIT,
            "video_max_seconds": MAX_VIDEO_TOTAL,
            "video_max_mb": 100,
            "audio_max_mb": 15,
            "image_max_mb": 20,
        },
        "modes_engine": list(MODES),
    }


def _as_int(value, field: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise VideoGenError(f"{field}需要填写整数。")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise VideoGenError(f"{field}需要填写整数。") from None


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


def validate_request(payload: dict, *, image_count: int,
                     video_metas: list[dict], audio_count: int) -> dict:
    """把前端请求规整成任务字段；不合法直接抛 VideoGenError。

    `video_metas` 是已校验过的上传记录（含 duration），用来挡「输入+输出 ≤30 秒」。
    """
    if not isinstance(payload, dict):
        raise VideoGenError("请求格式不正确。")

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise VideoGenError("请填写提示词，描述你要生成的视频。")
    if len(prompt) > PROMPT_LIMIT:
        raise VideoGenError(f"提示词最多 {PROMPT_LIMIT} 字（当前 {len(prompt)} 字）。")

    negative = str(payload.get("negative_prompt") or "").strip()
    if len(negative) > NEGATIVE_LIMIT:
        raise VideoGenError(f"反向提示词最多 {NEGATIVE_LIMIT} 字。")

    mode_choice = str(payload.get("mode") or "auto").strip().lower()
    if mode_choice not in {"auto", "reference", "edit"}:
        raise VideoGenError("生成方式无效。")
    has_videos = bool(video_metas)
    mode = "edit" if mode_choice in {"edit", "auto"} and has_videos else "reference"
    if mode_choice == "edit" and not has_videos:
        raise VideoGenError("「视频编辑」至少需要一段参考视频；只给图片请用「参考生视频」。")

    resolution = str(payload.get("resolution") or "1080P").strip().upper()
    if resolution not in RESOLUTIONS:
        raise VideoGenError(f"分辨率只能是 {' / '.join(RESOLUTIONS)}。")

    ratio = str(payload.get("ratio") or "").strip().lower()
    ratio = ratio or ("adaptive" if mode == "edit" else "16:9")
    if ratio not in RATIOS:
        raise VideoGenError(f"画面比例只能是 {' / '.join(RATIOS)}。")
    if mode != "edit" and ratio == "adaptive":
        raise VideoGenError("参考生视频需要具体画面比例（如 16:9 / 9:16 / 1:1）。")

    duration = _as_int(payload.get("duration"), "时长", 5)
    if duration != -1 and not 2 <= duration <= 30:
        raise VideoGenError("时长只能是 -1（自动）或 2–30 的整数。")
    if mode != "edit" and duration == -1:
        # -1 是视频编辑的「智能时长」；参考生视频没有原片可参照，按 5 秒走。
        duration = 5

    count = _as_int(payload.get("count"), "生成数量", 1)
    if not 1 <= count <= MAX_COUNT:
        raise VideoGenError(f"生成数量需在 1–{MAX_COUNT} 之间。")

    raw_seed = payload.get("seed")
    if raw_seed is None or str(raw_seed).strip() == "":
        seed = None
    else:
        seed = _as_int(raw_seed, "随机种子", -1)
        if seed != -1 and not 0 <= seed <= MAX_SEED:
            raise VideoGenError(f"随机种子需在 -1（随机）或 0–{MAX_SEED} 之间。")

    if image_count > MAX_IMAGES:
        raise VideoGenError(f"参考图最多 {MAX_IMAGES} 张。")
    if len(video_metas) > MAX_VIDEOS:
        raise VideoGenError(f"参考视频最多 {MAX_VIDEOS} 段。")
    if audio_count > MAX_AUDIOS:
        raise VideoGenError(f"参考音频最多 {MAX_AUDIOS} 段。")

    known = [meta.get("duration") for meta in video_metas if meta.get("duration")]
    total_seconds = round(sum(known), 2) if len(known) == len(video_metas) else None
    if total_seconds is not None:
        if total_seconds > MAX_VIDEO_TOTAL:
            raise VideoGenError(f"参考视频总时长 {total_seconds:g}s，超过 {MAX_VIDEO_TOTAL:g} 秒上限。")
        if duration != -1 and total_seconds + duration > MAX_INPUT_PLUS_OUTPUT:
            raise VideoGenError(
                f"参考视频 {total_seconds:g}s + 输出 {duration}s = "
                f"{total_seconds + duration:g}s，超过 {MAX_INPUT_PLUS_OUTPUT:g} 秒上限；"
                "请缩短参考视频或输出时长。"
            )

    return {
        "prompt": prompt,
        "negative_prompt": negative,
        "mode_choice": mode_choice,
        "mode": mode,
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "count": count,
        "seed": seed,
        "audio": as_bool(payload.get("audio"), False),
        "prompt_extend": as_bool(payload.get("prompt_extend"), True),
        "watermark": as_bool(payload.get("watermark"), False),
        "video_seconds": total_seconds,
    }
