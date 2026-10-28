#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯云 COS 的最小 REST 实现（标准库，不依赖 qcloud_cos）。

为什么有它：
  · 打包机/客户机不一定装了 `cos-python-sdk-v5`；
  · 官方版换装链路只需要三个动作：上传源图、上传成品、签名 URL；
  · 缺 SDK 时用本文件兜底，避免“配置都填好了却卡在缺少依赖”。

限制：
  · 单次 PUT（COS 单对象上限 5GB；本项目源图/5–10s 成片远小于此）；
  · `exists/stat` 用 Range GET 0-0 实现，兼容子账号只有 GetObject 的权限；
  · `list_prefix` 不支持；需要完整能力请安装 cos-python-sdk-v5。

签名算法与腾讯云 COS V5 一致：SignKey = HMAC-SHA1(SecretKey, q-key-time) 的 hexdigest。
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:                      # 作为包导入
    from .storage import StorageConfig, StorageError
except ImportError:       # 直接运行时
    from storage import StorageConfig, StorageError  # type: ignore


class CosRestClient:
    def __init__(self, bucket: str, region: str, secret_id: str, secret_key: str):
        self.bucket = bucket
        self.region = region
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.host = f"{bucket}.cos.{region}.myqcloud.com"

    def _path(self, key: str) -> str:
        return "/" + urllib.parse.quote(key.lstrip("/"), safe="/-_.~")

    def _auth(self, method: str, key: str, expires: int = 3600) -> dict:
        start = int(time.time()) - 60
        end = int(time.time()) + int(expires)
        sign_time = f"{start};{end}"
        key_time = sign_time
        http_string = (
            f"{method.lower()}\n{self._path(key)}\n\n"
            f"host={urllib.parse.quote(self.host, safe='')}\n"
        )
        string_to_sign = (
            "sha1\n" + sign_time + "\n"
            + hashlib.sha1(http_string.encode()).hexdigest() + "\n"
        )
        # 注意：COS 的 SignKey 是 hexdigest，不是 digest
        sign_key = hmac.new(
            self.secret_key.encode(), key_time.encode(), hashlib.sha1
        ).hexdigest().encode()
        signature = hmac.new(
            sign_key, string_to_sign.encode(), hashlib.sha1
        ).hexdigest()
        return {
            "q-sign-algorithm": "sha1",
            "q-ak": self.secret_id,
            "q-sign-time": sign_time,
            "q-key-time": key_time,
            "q-header-list": "host",
            "q-url-param-list": "",
            "q-signature": signature,
        }

    def _authorization(self, method: str, key: str) -> str:
        return "&".join(f"{k}={v}" for k, v in self._auth(method, key).items())

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        url = f"https://{self.host}{self._path(key)}"
        req = urllib.request.Request(
            url, data=data, method="PUT",
            headers={
                "Host": self.host,
                "Content-Type": content_type,
                "Authorization": self._authorization("put", key),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return str(resp.headers.get("ETag") or "")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise StorageError(f"COS 上传 {key} 失败：HTTP {exc.code} {body}") from None
        except urllib.error.URLError as exc:
            raise StorageError(f"COS 上传 {key} 网络错误：{exc.reason}") from None

    def put_file(self, key: str, path: str | Path, content_type: str = "") -> str:
        src = Path(path)
        ctype = content_type or mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        return self.put(key, src.read_bytes(), ctype)

    def sign_url(self, key: str, expires: int = 7 * 24 * 3600) -> str:
        params = self._auth("get", key, expires)
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return f"https://{self.host}{self._path(key)}?{query}"

    def delete(self, key: str) -> None:
        url = f"https://{self.host}{self._path(key)}"
        req = urllib.request.Request(
            url, method="DELETE",
            headers={"Host": self.host, "Authorization": self._authorization("delete", key)},
        )
        try:
            with urllib.request.urlopen(req, timeout=60):
                return
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            body = exc.read().decode("utf-8", "replace")[:300]
            raise StorageError(f"COS 删除 {key} 失败：HTTP {exc.code} {body}") from None


class CosRestStorage:
    """Storage 的鸭子类型实现，只覆盖本项目主流程需要的动作。"""

    def __init__(self, cfg: StorageConfig):
        cfg.validate()
        self.cfg = cfg
        self.client = CosRestClient(cfg.bucket, cfg.region, cfg.secret_id, cfg.secret_key)

    @property
    def provider(self) -> str:
        return "cos-rest"

    def put_path(self, path: str | Path, key: str, *, content_type: str = "",
                 meta: dict | None = None) -> str:
        src = Path(path)
        if not src.is_file():
            raise StorageError(f"待上传文件不存在：{src}")
        if len(key.encode("utf-8")) > 1024:
            raise StorageError("对象 Key 超过 1024 字节")
        self.client.put_file(key.lstrip("/"), src, content_type)
        return key

    def put_bytes(self, data: bytes, key: str, *, content_type: str = "") -> str:
        self.client.put(key.lstrip("/"), data, content_type or "application/octet-stream")
        return key

    def _range_stat(self, key: str) -> dict | None:
        """用 Range GET 0-0 取对象大小/ETag；比 HEAD 稳（子账号有 GetObject 权限）。"""
        url = self.client.sign_url(key.lstrip("/"), 60)
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                content_range = headers.get("content-range", "")
                size = 0
                if "/" in content_range:
                    try:
                        size = int(content_range.rsplit("/", 1)[-1])
                    except ValueError:
                        size = 0
                if not size:
                    try:
                        size = int(headers.get("content-length") or 0)
                    except ValueError:
                        size = 0
                return {
                    "size": size,
                    "etag": str(headers.get("etag") or "").strip('"'),
                    "type": headers.get("content-type") or "",
                }
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            body = exc.read().decode("utf-8", "replace")[:300]
            raise StorageError(f"COS 读取 {key} 失败：HTTP {exc.code} {body}") from None

    def exists(self, key: str) -> bool:
        return self._range_stat(key) is not None

    def stat(self, key: str) -> dict:
        info = self._range_stat(key)
        if info is None:
            raise StorageError(f"对象不存在：{key}")
        return info

    def sign_url(self, key: str, expires: int = 3600) -> str:
        if expires <= 0:
            return self.public_url(key)
        return self.client.sign_url(key.lstrip("/"), max(60, min(int(expires), 7 * 24 * 3600)))

    def public_url(self, key: str) -> str:
        key = key.lstrip("/")
        quoted = urllib.parse.quote(key, safe="/-_.~")
        if self.cfg.domain:
            return f"{self.cfg.scheme}://{self.cfg.domain.strip('/')}/{quoted}"
        return f"{self.cfg.scheme}://{self.cfg.bucket}.cos.{self.cfg.region}.myqcloud.com/{quoted}"

    def delete(self, key: str) -> None:
        self.client.delete(key.lstrip("/"))

    def list_prefix(self, prefix: str, limit: int = 1000) -> list[str]:
        raise StorageError(
            "COS 快速实现不支持 list_prefix；安装 cos-python-sdk-v5 获得完整能力"
        )
