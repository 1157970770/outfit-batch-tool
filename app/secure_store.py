#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地敏感数据加密存储。

目标：配置文件里的 RH Key / token 落盘时**没有明文**，用记事本打开只会看到一串 base64。

实现优先级：
  1. Windows DPAPI（CryptProtectData）—— 用当前登录用户的凭据加密，
     换用户/换机器都解不开，系统级、无需第三方库；
  2. 其他平台或 DPAPI 失败时，退化为 HMAC-SHA256 派生密钥的流加密 + 校验标签
     （属于"混淆级"，能防记事本和随手拷走，防不了逆向）。

注意：这挡的是"客户打开文件看到明码"，不是对抗专业逆向。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
from ctypes import wintypes
from typing import Any

_SALT = b"outfit-tool/store/v1"

CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        in_blob, _keep = _to_blob(data)
        out_blob = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(in_blob), None, None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
            )
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001
        return None


def _stream_key() -> bytes:
    seed = (_SALT + os.getenv("COMPUTERNAME", "").encode() + os.getenv("USERNAME", "").encode())
    return hashlib.sha256(seed).digest()


def _xor_stream(data: bytes, nonce: bytes) -> bytes:
    key = _stream_key()
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out[: len(data)]))


def seal(obj: Any) -> str:
    """把任意 JSON 可序列化对象变成一串可安全落盘的字符串。"""
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    protected = _dpapi(raw, protect=True)
    if protected is not None:
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    nonce = os.urandom(16)
    body = _xor_stream(raw, nonce)
    tag = hmac.new(_stream_key(), nonce + body, hashlib.sha256).digest()[:16]
    return "xor:" + base64.b64encode(nonce + tag + body).decode("ascii")


def unseal(text: str) -> dict | None:
    """解不开（换机器/换用户/文件损坏）返回 None，由调用方当作"没有缓存"。"""
    text = (text or "").strip()
    try:
        if text.startswith("dpapi:"):
            raw = base64.b64decode(text[6:])
            plain = _dpapi(raw, protect=False)
            if plain is None:
                return None
            return json.loads(plain.decode("utf-8"))
        if text.startswith("xor:"):
            raw = base64.b64decode(text[4:])
            nonce, tag, body = raw[:16], raw[16:32], raw[32:]
            expect = hmac.new(_stream_key(), nonce + body, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(tag, expect):
                return None
            return json.loads(_xor_stream(body, nonce).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None
