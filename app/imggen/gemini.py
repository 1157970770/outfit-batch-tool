# -*- coding: utf-8 -*-
"""Nano Banana（Google Gemini 原生协议）客户端：标准库 urllib 实现。

和 api.py 的 Images 协议完全不是一套东西，所以单独一个模块：

- 入口：`POST {host}/v1beta/models/{model}:generateContent`，JSON 体
- 出图：`candidates[].content.parts[].inlineData.data`（base64）；**必须跳过
  `thought: true` 的思考中间图**，同一响应里可能有多张，最后一张才是成品
- 比例 / 分辨率：`generationConfig.imageConfig.aspectRatio` + `imageSize`
  （`1K`/`2K`/`4K`，大写 K；小写有的型号会报错），`1K` 是默认档
- 参考图：`parts` 里追加多段 `inlineData`，顺序即模型的「图1、图2…」，最多 14 张
- 计费陷阱：**HTTP 200 但没返回图片也会扣一次**（审核拦截不报错码、或提示词
  没明确要求出图），所以这里绝不自动重试，只能把原因交给用户改提示词

文档：https://docs.laozhang.ai/api-capabilities/nano-banana-image
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .api import _headers, _read_response
from .errors import ImageApiError, sanitize
from .models import GEMINI_SIZE_BY_VALUE

DEFAULT_TIMEOUT = 360
# 请求体上限 20MB，base64 会膨胀 1/3，所以内联原图合计按 15MB 兜底。
MAX_INLINE_BYTES = 15 * 1024 * 1024
_DATA_URL_RE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)")


def native_endpoint(base_url: str, model: str) -> str:
    """把配置里的 `.../v1` 换成 Gemini 原生入口 `.../v1beta/models/<model>:generateContent`。"""
    base = (base_url or "").strip()
    if not base:
        raise ImageApiError("生图接口地址未配置，请检查 server/imagegen.json 的 base_url")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme.lower() not in ("http", "https"):
        raise ImageApiError("生图接口地址必须以 http:// 或 https:// 开头")
    root = base.rstrip("/")
    for suffix in ("/v1beta", "/v1"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    name = urllib.parse.quote(str(model or "").strip(), safe=".-_")
    if not name:
        raise ImageApiError("未指定图片模型")
    return f"{root}/v1beta/models/{name}:generateContent"


def split_size(value: str) -> tuple[str, str]:
    """`4:5@2K` → (`4:5`, `2K`)；`auto` → 两个空串（不传 imageConfig）。"""
    text = str(value or "").strip() or "auto"
    spec = GEMINI_SIZE_BY_VALUE.get(text)
    if spec is None:
        raise ImageApiError(f"不支持的输出规格：{text}")
    return spec.aspect_ratio, spec.image_size


def _mime_type(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0] or ""
    if guessed.startswith("image/"):
        return guessed
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".bmp": "image/bmp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/png")


def _payload(
    prompt: str,
    image_paths: Sequence[Path],
    aspect_ratio: str,
    image_size: str,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    total = 0
    for item in image_paths:
        path = Path(item)
        raw = path.read_bytes()
        total += len(raw)
        parts.append({
            "inlineData": {
                "mimeType": _mime_type(path),
                "data": base64.b64encode(raw).decode("ascii"),
            },
        })
    if total > MAX_INLINE_BYTES:
        raise ImageApiError(
            f"参考图合计 {total / 1024 / 1024:.1f}MB，超过 15MB 内联上限，请减少张数或压缩后再试。")

    generation: dict[str, Any] = {"responseModalities": ["IMAGE"]}
    image_config: dict[str, str] = {}
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    if image_size:
        image_config["imageSize"] = image_size
    if image_config:
        generation["imageConfig"] = image_config
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation,
    }


def _decode_b64(data: str) -> bytes:
    text = str(data or "").strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    text = re.sub(r"\s+", "", text)
    if not text:
        return b""
    padding = (-len(text)) % 4
    try:
        return base64.b64decode(text + "=" * padding, validate=False)
    except (binascii.Error, ValueError):
        raise ImageApiError("接口返回的 base64 图片数据损坏") from None


def _extract_image(result: dict[str, Any]) -> bytes:
    """按官方文档取最后一张非思考图；顺带兼容网关把图塞进文本 data URL 的情况。"""
    candidates = result.get("candidates") or []
    texts: list[str] = []
    image: bytes = b""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                if part.get("thought") or inline.get("thought"):
                    continue  # 思考中间图，不是成品
                decoded = _decode_b64(inline.get("data"))
                if decoded:
                    image = decoded
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())

    if image:
        return image

    for text in texts:
        match = _DATA_URL_RE.search(text)
        if match:
            decoded = _decode_b64(match.group(2))
            if decoded:
                return decoded

    feedback = result.get("promptFeedback") or {}
    reason = str(feedback.get("blockReason") or "").strip()
    if not reason and candidates and isinstance(candidates[0], dict):
        reason = str(candidates[0].get("finishReason") or "").strip()
    detail = sanitize(" ".join(texts), 200)
    message = "模型没有返回图片"
    if reason:
        message += f"（{reason}）"
    if detail:
        message += f"：{detail}"
    message += ("。提示词里要明确说「生成一张图片」；这次请求可能已被计费，"
                "请先改提示词再试，不要原样重复提交。")
    raise ImageApiError(message)


def _post(url: str, payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_headers(api_key, "application/json"),
        method="POST",
    )
    return _read_response(request, timeout)


def generate_image(
    prompt: str,
    size: str,
    api_key: str,
    *,
    base_url: str,
    model: str,
    quality: str = "auto",
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    """文生图（不带参考图）。`size` 形如 `4:5@2K` 或 `auto`。"""
    aspect_ratio, image_size = split_size(size)
    result = _post(native_endpoint(base_url, model),
                   _payload(prompt, (), aspect_ratio, image_size), api_key, timeout)
    return _extract_image(result)


def edit_image(
    prompt: str,
    size: str,
    image_paths: Sequence[Path],
    api_key: str,
    *,
    base_url: str,
    model: str,
    quality: str = "auto",
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    """参考图改图：整图重绘，不支持 mask；`image_paths` 顺序即「图1、图2…」。"""
    if not image_paths:
        raise ValueError("至少需要一张参考图片")
    aspect_ratio, image_size = split_size(size)
    result = _post(native_endpoint(base_url, model),
                   _payload(prompt, image_paths, aspect_ratio, image_size), api_key, timeout)
    return _extract_image(result)


__all__ = ["DEFAULT_TIMEOUT", "edit_image", "generate_image", "native_endpoint", "split_size"]
