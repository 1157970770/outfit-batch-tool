#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页用户密码哈希、会话签名与登录限流。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from collections import deque

PBKDF2_ROUNDS = 310_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_password(password: str) -> dict:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return {"salt": _b64(salt), "hash": _b64(digest), "rounds": PBKDF2_ROUNDS}


def verify_password(password: str, record: dict) -> bool:
    try:
        expected = _unb64(str(record["hash"]))
        salt = _unb64(str(record["salt"]))
        rounds = int(record.get("rounds", PBKDF2_ROUNDS))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError):
        return False


class SessionSigner:
    def __init__(self, secret: str, hours: int = 12):
        self.secret = _unb64(secret)
        self.hours = max(1, min(168, int(hours)))

    def issue(self, username: str, user: dict) -> str:
        payload = {"u": username, "r": user.get("role", "user"),
                   "v": int(user.get("version", 1)), "exp": int(time.time() + self.hours * 3600)}
        raw = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        sig = _b64(hmac.new(self.secret, raw.encode("ascii"), hashlib.sha256).digest())
        return raw + "." + sig

    def verify(self, token: str, users: dict) -> dict | None:
        try:
            raw, signature = token.split(".", 1)
            expected = hmac.new(self.secret, raw.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(signature)):
                return None
            payload = json.loads(_unb64(raw).decode("utf-8"))
            username = str(payload["u"])
            user = users.get(username)
            if not user or not user.get("enabled", True):
                return None
            if int(payload["exp"]) < time.time() or int(payload.get("v", 1)) != int(user.get("version", 1)):
                return None
            if user.get("expires") and str(user["expires"]) < time.strftime("%Y-%m-%d"):
                return None
            return {"username": username, "role": user.get("role", "user")}
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None


class LoginLimiter:
    """登录失败计数：按「桶」限流。

    一个桶就是一个 key。调用方应当**同时**传 IP 桶与账号桶：
    只按「IP+用户名」分桶等于没限流——换个用户名就是一个新桶，永远撞不到上限；
    只按账号分桶又挡不住拿随机用户名刷 CPU 的行为。所以这里支持
    同一份存储里按不同 key、不同上限各自计数（IP 桶上限由调用方给）。

    key 的数量必须是有限的：随机用户名会不断制造新桶，所以空桶用完即删、
    总桶数超过 `max_keys` 时按插入顺序淘汰最老的（近似 LRU）。
    """

    def __init__(self, limit: int = 8, window: int = 600, max_keys: int = 2000):
        self.limit, self.window = limit, window
        self.max_keys = max(16, int(max_keys))
        self.lock = threading.Lock()
        self.failures: dict[str, deque] = {}
        self.order: deque[str] = deque()

    def allowed(self, key: str, limit: int | None = None) -> bool:
        cap = int(limit if limit is not None else self.limit)
        now = time.time()
        with self.lock:
            values = self.failures.get(key)
            if not values:
                return True
            while values and now - values[0] > self.window:
                values.popleft()
            if not values:
                self._drop_locked(key)
                return True
            return len(values) < cap

    def failed(self, key: str) -> None:
        with self.lock:
            values = self.failures.get(key)
            if values is None:
                values = self.failures[key] = deque()
                self.order.append(key)
                self._evict_locked()
            values.append(time.time())

    def succeeded(self, key: str) -> None:
        with self.lock:
            self._drop_locked(key)

    def _drop_locked(self, key: str) -> None:
        if self.failures.pop(key, None) is not None:
            try:
                self.order.remove(key)
            except ValueError:
                pass

    def _evict_locked(self) -> None:
        while len(self.failures) > self.max_keys and self.order:
            self._drop_locked(self.order[0])
