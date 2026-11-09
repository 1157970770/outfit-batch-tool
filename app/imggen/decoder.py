# -*- coding: utf-8 -*-
"""从接口响应里取出图片字节：优先 b64_json，其次 url（再下载一次）。"""
from __future__ import annotations

import base64
import binascii
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


def _normalize_b64(value: str) -> str:
    value = value.strip()
    if value.startswith("data:"):
        value = value.split(",", 1)[1]
    value += "=" * ((4 - len(value) % 4) % 4)
    return value


def _download(url: str, timeout: int) -> bytes:
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("接口返回的图片地址格式不受支持")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.URLError as exc:
        raise ValueError(f"下载接口返回的图片失败：{getattr(exc, 'reason', exc)}") from exc
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("接口返回的图片超过 64MB，已放弃下载")
    return data


def extract_image_bytes(payload: Mapping[str, Any], *, timeout: int = 120) -> bytes:
    items = payload.get("data") or []
    if not items:
        raise ValueError("API 返回内容中没有找到图片数据")

    first = items[0]
    if not isinstance(first, Mapping):
        raise ValueError("API 返回的图片数据结构不正确")

    b64_value = first.get("b64_json")
    if b64_value:
        try:
            return base64.b64decode(_normalize_b64(str(b64_value)), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("接口返回的 base64 图片数据损坏") from exc

    url = first.get("url")
    if url:
        url = str(url)
        if url.startswith("data:"):
            return base64.b64decode(_normalize_b64(url))
        return _download(url, timeout)

    raise ValueError("API 返回内容里没有 b64_json 或 url 字段")
