# -*- coding: utf-8 -*-
"""老张（laozhang.ai）图片接口客户端：标准库 urllib 实现。

接口契约（与桌面版一致，已按 2026-11-09 的文档核对）：
- 文生图：POST {base_url}images/generations，JSON 体 {model, prompt, size?, quality?}
- 改图：  POST {base_url}images/edits，multipart；参考图字段名固定 `image[]`，
          多个文件按传入顺序被模型视作「图1、图2…」，所以调用方必须自己保证顺序
- 返回：  {data: [{b64_json | url}]}，由 decoder.extract_image_bytes 取字节

只允许 VIP 按次计费型号（$0.03/张）；参数校验在 models.image_request_parameters。
"""
from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Sequence

from .decoder import extract_image_bytes
from .errors import ImageApiError, http_error_message, network_error_message, sanitize
from .models import image_request_parameters

DEFAULT_TIMEOUT = 300
USER_AGENT = "OutfitWebImageGen/1.0 (+local)"


def _endpoint(base_url: str, name: str) -> str:
    base = (base_url or "").strip()
    if not base:
        raise ImageApiError("生图接口地址未配置，请检查 server/imagegen.json 的 base_url")
    scheme = urllib.parse.urlsplit(base).scheme.lower()
    if scheme not in ("http", "https"):
        raise ImageApiError("生图接口地址必须以 http:// 或 https:// 开头")
    return base.rstrip("/") + "/" + name.lstrip("/")


def _headers(api_key: str, content_type: str | None = None) -> dict[str, str]:
    key = (api_key or "").strip()
    if not key:
        raise ImageApiError("生图接口密钥未配置，请检查 server/imagegen.json 的 api_key")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _read_response(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:8000]
        message, detail = http_error_message(exc.code, body)
        raise ImageApiError(message, status=exc.code, detail=detail) from exc
    except urllib.error.URLError as exc:
        raise ImageApiError(network_error_message(getattr(exc, "reason", exc), timeout)) from exc
    except TimeoutError as exc:
        raise ImageApiError(network_error_message(exc, timeout)) from exc

    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ImageApiError("接口没有返回 JSON，可能被网关拦截或地址配置有误") from exc


def _safe_filename(name: str) -> str:
    """multipart 的 filename 只能是 ascii，参考图我们统一重命名过，这里兜底。"""
    cleaned = "".join(ch for ch in str(name or "") if ch.isascii() and (ch.isalnum() or ch in "._-"))
    return cleaned or "reference.png"


def _multipart(fields: dict[str, str], files: Sequence[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = "----imggen" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for key, path in files:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = _safe_filename(path.name)
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
            f'filename="{filename}"\r\nContent-Type: {mime_type}\r\n\r\n'.encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def generate_image(
    prompt: str,
    size: str,
    api_key: str,
    *,
    base_url: str,
    model: str,
    quality: str = "high",
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    """文生图（不带参考图）。返回 PNG/JPEG 原始字节。"""
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        **image_request_parameters(model, size, quality),
    }
    request = urllib.request.Request(
        _endpoint(base_url, "images/generations"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_headers(api_key, "application/json"),
        method="POST",
    )
    return extract_image_bytes(_read_response(request, timeout), timeout=min(timeout, 180))


def edit_image(
    prompt: str,
    size: str,
    image_paths: Sequence[Path],
    api_key: str,
    *,
    base_url: str,
    model: str,
    quality: str = "high",
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    """参考图改图。`image_paths` 的顺序就是模型的「图1、图2…」。"""
    if not image_paths:
        raise ValueError("至少需要一张参考图片")

    fields = {
        "model": model,
        "prompt": prompt,
        **image_request_parameters(model, size, quality),
    }
    body, content_type = _multipart(fields, [("image[]", Path(path)) for path in image_paths])
    request = urllib.request.Request(
        _endpoint(base_url, "images/edits"),
        data=body,
        headers=_headers(api_key, content_type),
        method="POST",
    )
    return extract_image_bytes(_read_response(request, timeout), timeout=min(timeout, 180))


def masked_key(api_key: str) -> str:
    """给状态接口用的密钥指纹，绝不回显完整 Key。"""
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return sanitize(f"{key[:4]}…{key[-4:]}")
