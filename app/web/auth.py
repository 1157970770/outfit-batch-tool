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
from collections import defaultdict, deque

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
    def __init__(self, limit: int = 8, window: int = 600):
        self.limit, self.window = limit, window
        self.lock = threading.Lock()
        self.failures: dict[str, deque] = defaultdict(deque)

    def allowed(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            values = self.failures[key]
            while values and now - values[0] > self.window:
                values.popleft()
            return len(values) < self.limit

    def failed(self, key: str) -> None:
        with self.lock:
            self.failures[key].append(time.time())

    def succeeded(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)
