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
  · `list_prefix` 用 GET bucket（ListObjects）实现，管理端云端检索的兜底数据源。

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
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

try:                      # 作为包导入
    from .storage import StorageConfig, StorageError, _with_retry
except ImportError:       # 直接运行时
    from storage import StorageConfig, StorageError, _with_retry  # type: ignore


class CosRestClient:
    def __init__(self, bucket: str, region: str, secret_id: str, secret_key: str,
                 token: str = ""):
        self.bucket = bucket
        self.region = region
        self.secret_id = secret_id
        self.secret_key = secret_key
        # P1-22：STS 临时凭证（tccli OAuth / AssumeRole）必须随请求携带安全令牌，
        # 否则签名再对也会全量 AccessDenied。
        self.token = str(token or "")
        self.host = f"{bucket}.cos.{region}.myqcloud.com"

    def _path(self, key: str) -> str:
        """请求行里的 URL 路径：必须百分号编码（中文 / 空格都编）。"""
        return "/" + urllib.parse.quote(key.lstrip("/"), safe="/-_.~")

    def _sign_path(self, key: str) -> str:
        """参与签名的 UriPathname：必须是**解码后**的原样路径，不要 %XX。

        2026-11-09 实测定论：COS 服务端先把请求行里的 %XX 解码，再用解码结果
        校验签名（403 响应里的 <FormatString> 就是中文原样：
        `put\n/input/tpl/哈尔滨-7.mp4\n\nhost=...`）。所以带中文的对象 Key
        若按百分号编码参与签名，会稳定 SignatureDoesNotMatch；按原始路径签名
        同一次上传立刻成功。历史证据：桶里 input/tpl 上千个对象没有一个中文名，
        网页上传中文名模板全挂在这条上。
        """
        return "/" + key.lstrip("/")

    def _auth(self, method: str, key: str, expires: int = 3600,
              params: dict | None = None) -> dict:
        start = int(time.time()) - 60
        end = int(time.time()) + int(expires)
        sign_time = f"{start};{end}"
        key_time = sign_time
        names = sorted((params or {}).keys())
        param_str = "&".join(
            f"{name}={urllib.parse.quote(str((params or {})[name]), safe='')}" for name in names
        )
        http_string = (
            f"{method.lower()}\n{self._sign_path(key)}\n{param_str}\n"
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
            "q-url-param-list": ";".join(names),
            "q-signature": signature,
        }

    def _authorization(self, method: str, key: str, params: dict | None = None) -> str:
        return "&".join(f"{k}={v}" for k, v in self._auth(method, key, params=params).items())

    def _headers(self, method: str, key: str, extra: dict | None = None,
                 params: dict | None = None) -> dict:
        """带安全令牌的请求头：临时凭证时缺 x-cos-security-token 必然 403。"""
        headers = {"Host": self.host, "Authorization": self._authorization(method, key, params=params)}
        if self.token:
            headers["x-cos-security-token"] = self.token
        if extra:
            headers.update(extra)
        return headers

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream",
            headers: dict | None = None) -> str:
        url = f"https://{self.host}{self._path(key)}"
        merged = self._headers("put", key, {"Content-Type": content_type})
        merged.update(headers or {})
        req = urllib.request.Request(url, data=data, method="PUT", headers=merged)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return str(resp.headers.get("ETag") or "")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise StorageError(f"COS 上传 {key} 失败：HTTP {exc.code} {body}") from None
        except urllib.error.URLError as exc:
            raise StorageError(f"COS 上传 {key} 网络错误：{exc.reason}") from None

    def put_file(self, key: str, path: str | Path, content_type: str = "",
                 headers: dict | None = None) -> str:
        src = Path(path)
        ctype = content_type or mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        return self.put(key, src.read_bytes(), ctype, headers)

    def put_tagging(self, key: str, tags: dict) -> None:
        """给已存在的对象打标签（PUT ?tagging）。

        中文键值对直接以 UTF-8 写进 XML（只做 XML 转义，不做 URL 编码）——
        COS 标签原生支持中文（键 ≤127 字符、值 ≤255 字符）。
        需要 CAM 里的 cos:PutObjectTagging；没开通时 COS 返回
        "No put tagging auth"，调用方应把它当成可选动作失败，不要影响出片。
        """
        items = "".join(
            f"<Tag><Key>{_xml_escape(str(k))}</Key>"
            f"<Value>{_xml_escape(str(v))}</Value></Tag>"
            for k, v in tags.items()
        )
        body = f"<Tagging><TagSet>{items}</TagSet></Tagging>".encode("utf-8")
        params = {"tagging": ""}
        url = f"https://{self.host}{self._path(key)}?tagging"
        req = urllib.request.Request(
            url, data=body, method="PUT",
            headers=self._headers(
                "put", key,
                {
                    "Content-Type": "application/xml",
                    "Content-MD5": __import__("base64").b64encode(
                        __import__("hashlib").md5(body).digest()).decode(),
                },
                params=params,
            ),
        )
        try:
            with urllib.request.urlopen(req, timeout=60):
                return
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", "replace")[:200]
            raise StorageError(f"COS 打标签失败：HTTP {exc.code} {message}") from None
        except urllib.error.URLError as exc:
            raise StorageError(f"COS 打标签网络错误：{exc.reason}") from None

    def list_prefix(self, prefix: str, limit: int = 1000) -> list[dict]:
        """GET bucket（ListObjects）：按前缀列举对象，返回 [{key, size}]。

        管理端云端检索的兜底数据源；只解析 XML 里需要的 Key/Size 两个字段。
        """
        out: list[dict] = []
        marker = ""
        base = prefix.strip().lstrip("/")
        while len(out) < limit:
            params = {"prefix": base, "max-keys": str(min(1000, limit - len(out)))}
            if marker:
                params["marker"] = marker
            query = "&".join(
                f"{name}={urllib.parse.quote(value, safe='')}" for name, value in sorted(params.items())
            )
            auth = self._auth("get", "", params=params)
            if self.token:
                # query 签名路径（列举 / 签名 URL）：令牌必须作为 query 参数携带
                auth["x-cos-security-token"] = self.token
            url = f"https://{self.host}/?{query}&" + urllib.parse.urlencode(auth)
            req = urllib.request.Request(url, headers={"Host": self.host})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    root = ET.fromstring(resp.read())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:200]
                raise StorageError(f"COS 列举失败：HTTP {exc.code} {body}") from None
            except urllib.error.URLError as exc:
                raise StorageError(f"COS 列举网络错误：{exc.reason}") from None
            # COS 的 ListBucketResult 实际不带 namespace（根标签就是 ListBucketResult）。
            # 早期写法把根标签当 namespace 拼成 {ListBucketResult}Contents，导致 findall
            # 永远匹配不到 → list_prefix 恒返回空（去重/清理/列举全部静默失效）。
            ns_prefix = ""
            if root.tag.startswith("{") and "}" in root.tag:
                ns_prefix = root.tag[: root.tag.index("}") + 1]
            for contents in root.findall(f"{ns_prefix}Contents"):
                node = contents.find(f"{ns_prefix}Key")
                if node is None or not (node.text or "").strip():
                    continue
                size_node = contents.find(f"{ns_prefix}Size")
                try:
                    size = int(size_node.text or 0) if size_node is not None else 0
                except ValueError:
                    size = 0
                out.append({"key": node.text.strip(), "size": size})
            truncated = (root.findtext(f"{ns_prefix}IsTruncated") or "").lower() == "true"
            if not truncated:
                break
            marker = root.findtext(f"{ns_prefix}NextMarker") or (out[-1]["key"] if out else "")
            if not marker:
                break
        return out[:limit]

    def sign_url(self, key: str, expires: int = 7 * 24 * 3600) -> str:
        params = self._auth("get", key, expires)
        if self.token:
            params["x-cos-security-token"] = self.token
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return f"https://{self.host}{self._path(key)}?{query}"

    def delete(self, key: str) -> None:
        url = f"https://{self.host}{self._path(key)}"
        req = urllib.request.Request(
            url, method="DELETE",
            headers=self._headers("delete", key),
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
        self.client = CosRestClient(cfg.bucket, cfg.region, cfg.secret_id, cfg.secret_key,
                                    token=str(getattr(cfg, "token", "") or ""))

    @property
    def provider(self) -> str:
        return "cos-rest"

    def tag_object(self, key: str, tags: dict) -> None:
        if not tags:
            return
        _with_retry(lambda: self.client.put_tagging(key.lstrip("/"), tags),
                    f"打标签 {key}")

    def put_path(self, path: str | Path, key: str, *, content_type: str = "",
                 meta: dict | None = None) -> str:
        src = Path(path)
        if not src.is_file():
            raise StorageError(f"待上传文件不存在：{src}")
        if len(key.encode("utf-8")) > 1024:
            raise StorageError("对象 Key 超过 1024 字节")
        headers = {}
        for name, value in (meta or {}).items():
            text = str(value)
            # COS 自定义头值必须是 ASCII：中文款号留在路径与标签里，元数据不强行塞
            if text and text.isascii():
                headers[f"x-cos-meta-{name}"] = text[:100]
        # P1-22：与 SDK 路径一致，网络动作统一走 _with_retry（指数退避，可重试错误）
        _with_retry(lambda: self.client.put_file(key.lstrip("/"), src, content_type, headers or None),
                    f"上传 {key}")
        return key

    def put_bytes(self, data: bytes, key: str, *, content_type: str = "") -> str:
        _with_retry(lambda: self.client.put(key.lstrip("/"), data,
                                            content_type or "application/octet-stream"),
                    f"上传 {key}")
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
        _with_retry(lambda: self.client.delete(key.lstrip("/")), f"删除 {key}")

    def list_prefix(self, prefix: str, limit: int = 1000) -> list[dict]:
        base = prefix.strip().lstrip("/")
        return _with_retry(lambda: self.client.list_prefix(base, limit), "列举对象")
