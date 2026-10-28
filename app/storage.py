#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对象存储适配层（腾讯云 COS / 本地目录）。

上层（批量生产、归档脚本、界面）只认识这里的六个动作：

    put_path / put_bytes / exists / stat / sign_url / delete / list_prefix

换云、换桶、换成 MinIO，只要改本文件的 `open_storage` 分派，业务代码不动。

依赖：`cos-python-sdk-v5` 是可选增强；缺失时 `open_storage` 自动用
`app/cos_rest.py` 的标准库 REST 兜底（上传/签名/存在性/大小），本地后端零依赖。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

DEFAULT_EXPIRES = 3600              # 默认签名 1 小时
MAX_EXPIRES = 7 * 24 * 3600         # COS 永久密钥签名上限 7 天
MULTIPART_MIN = 8 * 1024 * 1024     # 超过 8MB 走分片上传
FINGERPRINT_CHUNK = 1 << 20         # 指纹只读首尾各 1MB，避免为了去重读整段视频

_SAFE_CHARS = re.compile(r"[^a-z0-9\-_.]+")
_DASHES = re.compile(r"-{2,}")


class StorageError(RuntimeError):
    """存储层统一异常，message 直接面向用户。"""


def _is_expired(stamp: str) -> bool:
    """判断 tccli 的 expiresAt（ISO8601 或 epoch 秒）是否已过期；判断不了就当没过期。"""
    stamp = (stamp or "").strip()
    if not stamp:
        return False
    try:
        if stamp.isdigit():
            return float(stamp) <= time.time()
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return dt.timestamp() <= time.time()
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class StorageConfig:
    """一份 COS 配置。字段名与腾讯云控制台/环境变量一一对应。"""

    provider: str = "cos"           # cos | local
    bucket: str = ""                # 形如 my-bucket-1250000000（必须带 APPID）
    region: str = ""                # 形如 ap-guangzhou / ap-shanghai
    secret_id: str = ""
    secret_key: str = ""
    token: str = ""                 # STS 临时凭证 Token（OAuth / AssumeRole 时需要）
    prefix: str = ""                # 对象前缀，例如 prod 或 test
    domain: str = ""                # CDN 加速域名，留空用 COS 默认域名
    scheme: str = "https"
    enabled: bool = True
    root: str = ""                  # provider=local 时的落盘根目录

    # -- 构造 --
    @classmethod
    def from_env(cls, **overrides: Any) -> "StorageConfig":
        """从环境变量读配置；显式传入的参数优先，便于界面覆盖。"""
        env_map = {
            "provider": "COS_PROVIDER",
            "bucket": "COS_BUCKET",
            "region": "COS_REGION",
            "secret_id": "COS_SECRET_ID",
            "secret_key": "COS_SECRET_KEY",
            "token": "COS_SECURITY_TOKEN",
            "prefix": "COS_PREFIX",
            "domain": "COS_DOMAIN",
            "scheme": "COS_SCHEME",
            "root": "COS_LOCAL_ROOT",
        }
        values: dict[str, Any] = {}
        for field, env in env_map.items():
            raw = os.environ.get(env, "").strip()
            if raw:
                values[field] = raw
        enabled = os.environ.get("COS_ENABLED", "").strip().lower()
        if enabled in {"1", "true", "yes", "on"}:
            values["enabled"] = True
        elif enabled in {"0", "false", "no", "off"}:
            values["enabled"] = False
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    # -- 本机已有凭证（不填密钥也能用） --
    @classmethod
    def from_local_profile(cls, profile: str = "", **overrides: Any) -> "StorageConfig":
        """按优先级凑配置：环境变量 → ~/.cos/credentials.json → ~/.tccli/<profile>.credential。

        场景：这台机器上已经 `tccli auth login` 过、或放好了 credentials.json，
        桌面版就不该再逼用户手抄一遍密钥。
        """
        cfg = cls.from_env(**overrides)
        if cfg.provider == "local" or (cfg.secret_id and cfg.secret_key):
            return cfg

        home = Path.home()

        cos_file = home / ".cos" / "credentials.json"
        if cos_file.exists():
            try:
                data = json.loads(cos_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StorageError(f"{cos_file} 读不了：{exc}") from None
            cfg.secret_id = cfg.secret_id or str(data.get("secret_id") or "")
            cfg.secret_key = cfg.secret_key or str(data.get("secret_key") or "")
            cfg.token = cfg.token or str(data.get("token") or "")
            cfg.region = cfg.region or str(data.get("region") or "")
            cfg.bucket = cfg.bucket or str(data.get("bucket") or "")

        if cfg.secret_id and cfg.secret_key:
            return cfg

        # tccli OAuth 临时凭证（约 2 小时，需定期重新登录）
        prof = profile or os.environ.get("COS_TCCLI_PROFILE", "").strip() or "agent1"
        cred_file = home / ".tccli" / f"{prof}.credential"
        if cred_file.exists():
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StorageError(f"{cred_file} 读不了：{exc}") from None
            expires = data.get("expiresAt") or data.get("expiredTime")
            if expires and _is_expired(str(expires)):
                raise StorageError(
                    f"tccli 凭证（profile={prof}）已过期，先跑一次：tccli auth login --profile {prof}"
                )
            cfg.secret_id = str(data.get("secretId") or "")
            cfg.secret_key = str(data.get("secretKey") or "")
            cfg.token = str(data.get("token") or "")

        return cfg

    # -- 校验 --
    def validate(self) -> None:
        """在真正发请求之前把配置问题说清楚，别等 403。"""
        if self.provider == "local":
            if not self.root:
                raise StorageError("本地存储需要指定 root 目录（COS_LOCAL_ROOT）")
            return
        if self.provider != "cos":
            raise StorageError(f"不支持的存储类型：{self.provider}（只认 cos / local）")
        if not self.bucket:
            raise StorageError("缺少存储桶名（形如 my-bucket-1250000000）")
        if not re.search(r"-\d{6,}$", self.bucket):
            raise StorageError(
                f"存储桶名「{self.bucket}」缺少 APPID 后缀。"
                "腾讯云 COS 的桶名必须形如 my-bucket-1250000000，APPID 在控制台「账号信息」里"
            )
        if not self.region:
            raise StorageError("缺少地域（形如 ap-guangzhou，控制台桶列表里能看到）")
        if not re.search(r"^ap-[a-z0-9-]+$", self.region):
            raise StorageError(f"地域「{self.region}」格式不对，应形如 ap-guangzhou")
        if not (self.secret_id and self.secret_key):
            raise StorageError("缺少 SecretId / SecretKey（建议用子账号密钥，不要用主账号）")

    def clean_prefix(self) -> str:
        return self.prefix.strip().strip("/")

    def describe(self) -> str:
        if self.provider == "local":
            return f"本地目录 {self.root}"
        return f"cos://{self.bucket}/{self.clean_prefix()}".rstrip("/")


# ---------------------------------------------------------------------------
# Key 规则：所有对象名都从这里生成，避免中文/空格/时间戳乱飞
# ---------------------------------------------------------------------------

def _slug(text: str, default: str = "file", limit: int = 48) -> str:
    """转成 URL 安全的小写片段；中文会退化成默认值（靠指纹保证唯一）。"""
    raw = unicodedata.normalize("NFKD", str(text))
    ascii_only = raw.encode("ascii", "ignore").decode("ascii")
    cleaned = _SAFE_CHARS.sub("-", ascii_only.lower())
    cleaned = _DASHES.sub("-", cleaned).strip("-._")
    return cleaned[:limit] or default


def object_key(kind: str, filename: str, *, batch: str = "", prefix: str = "",
               when: datetime | None = None, digest: str = "",
               ext: str | None = None) -> str:
    """生成对象 Key。

    结构：`{prefix}/{kind}/{YYYY-MM-DD}/{batch}/{文件名}-{指纹8}.{ext}`

    日期分区是为了让生命周期规则能按天扫；指纹是为了同一个文件重复上传时
    **命中同一个对象**，而不是每跑一次多存一份。
    """
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    suffix = ext if ext is not None else Path(filename).suffix
    suffix = _slug(suffix.lstrip("."), default="", limit=8)
    stem = _slug(Path(filename).stem, default="file")
    name = f"{stem}-{digest[:8]}" if digest else stem
    if suffix:
        name = f"{name}.{suffix}"

    parts = [p for p in (prefix.strip().strip("/"), _slug(kind, "misc"),
                         stamp, _slug(batch, "", 32) if batch else "") if p]
    return "/".join([*parts, name])


def file_fingerprint(path: str | Path) -> str:
    """文件指纹（长度 + 首尾各 1MB 的 sha256），用来做上传幂等。

    不是安全哈希，只回答"这两个文件是不是同一份内容"。
    """
    p = Path(path)
    size = p.stat().st_size
    hasher = hashlib.sha256()
    hasher.update(str(size).encode("ascii"))
    with p.open("rb") as fh:
        hasher.update(fh.read(FINGERPRINT_CHUNK))
        if size > 2 * FINGERPRINT_CHUNK:
            fh.seek(-FINGERPRINT_CHUNK, os.SEEK_END)
            hasher.update(fh.read(FINGERPRINT_CHUNK))
    return hasher.hexdigest()[:16]


def guess_content_type(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


# ---------------------------------------------------------------------------
# 错误处理小工具
# ---------------------------------------------------------------------------

def _call_method(exc: BaseException, name: str) -> Any:
    fn = getattr(exc, name, None)
    if callable(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - 探测性调用，失败就当没有
            return None
    return None


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    code = _call_method(exc, "get_status_code")
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "error_code", None) or _call_method(exc, "get_error_code") or ""
    return str(code)


def describe_error(exc: BaseException) -> str:
    """把 SDK 异常压成一行人话，带上排错线索。"""
    status = _status_code(exc)
    code = _error_code(exc)
    message = getattr(exc, "message", None) or _call_method(exc, "get_message") or str(exc)
    hint = {
        "SignatureDoesNotMatch": "检查 SecretKey 是否抄错/多了空格",
        "InvalidAccessKeyId": "SecretId 不存在，确认用的是子账号密钥且未被禁用",
        "AccessDenied": "子账号没有该桶/该前缀的权限，检查 CAM 策略",
        "NoSuchBucket": "桶名或地域不对（桶名必须带 APPID）",
        "RequestTimeTooSkewed": "本机时钟不准，校准系统时间",
        "NoSuchKey": "对象不存在",
    }.get(code, "")
    parts = [p for p in (
        f"HTTP {status}" if status else "",
        code,
        str(message)[:200],
        f"（{hint}）" if hint else "",
    ) if p]
    return " / ".join(parts) or type(exc).__name__


def _is_not_found(exc: BaseException) -> bool:
    return _status_code(exc) == 404 or _error_code(exc) in {"NoSuchKey", "NoSuchResource"}


def _is_retryable(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status is not None:
        return status >= 500 or status in (408, 429)
    return isinstance(exc, (OSError, TimeoutError)) or type(exc).__name__.endswith("ClientError")


def _with_retry(fn: Callable[[], Any], what: str, attempts: int = 3, base_delay: float = 0.5) -> Any:
    delay = base_delay
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 统一转成 StorageError
            last = exc
            if index == attempts - 1 or not _is_retryable(exc):
                break
            time.sleep(delay)
            delay *= 2
    assert last is not None
    raise StorageError(f"{what}失败：{describe_error(last)}") from None


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class Storage:
    """六个动作的约定。"""

    cfg: StorageConfig

    def put_path(self, path: str | Path, key: str, *, content_type: str = "",
                 meta: dict | None = None) -> str:
        raise NotImplementedError

    def put_bytes(self, data: bytes, key: str, *, content_type: str = "") -> str:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def stat(self, key: str) -> dict:
        raise NotImplementedError

    def sign_url(self, key: str, expires: int = DEFAULT_EXPIRES) -> str:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def list_prefix(self, prefix: str, limit: int = 1000) -> list[str]:
        raise NotImplementedError

    # -- 便利方法 --
    def uri(self, key: str) -> str:
        return f"{self.cfg.describe()}/{key}"

    def ensure_ready(self) -> None:
        """可选的连通性自检，供「测试连接」按钮调用。"""
        probe = object_key("_healthcheck", "ping.txt", prefix=self.cfg.clean_prefix())
        self.put_bytes(b"ok", probe, content_type="text/plain")
        self.delete(probe)


# ---------------------------------------------------------------------------
# 腾讯云 COS
# ---------------------------------------------------------------------------

class CosStorage(Storage):
    """腾讯云对象存储。SDK 延迟导入，没装依赖时给一条能照做的报错。"""

    def __init__(self, cfg: StorageConfig, client: Any | None = None):
        cfg.validate()
        self.cfg = cfg
        self._client = client

    # -- 客户端 --
    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from qcloud_cos import CosConfig, CosS3Client
            except ImportError:
                raise StorageError(
                    "缺少依赖：pip install cos-python-sdk-v5"
                    "（本地/TDD 用 provider=local，不需要这个包）"
                ) from None
            cos_cfg = CosConfig(
                Region=self.cfg.region,
                SecretId=self.cfg.secret_id,
                SecretKey=self.cfg.secret_key,
                Token=self.cfg.token or None,   # STS 临时凭证（tccli OAuth / AssumeRole）
                Scheme=self.cfg.scheme,
                Timeout=60,
            )
            self._client = CosS3Client(cos_cfg)
        return self._client

    def _key(self, key: str) -> str:
        key = key.strip().lstrip("/")
        if not key:
            raise StorageError("对象 Key 不能为空")
        if len(key.encode("utf-8")) > 1024:
            raise StorageError("对象 Key 超过 1024 字节")
        return key

    # -- 写 --
    def put_path(self, path: str | Path, key: str, *, content_type: str = "",
                 meta: dict | None = None) -> str:
        src = Path(path)
        if not src.is_file():
            raise StorageError(f"待上传文件不存在：{src}")
        key = self._key(key)
        ctype = content_type or guess_content_type(src.name)
        size = src.stat().st_size

        if size >= MULTIPART_MIN:
            # 大文件走分片：断点、并发、单片失败可重传
            _with_retry(
                lambda: self.client.upload_file(
                    Bucket=self.cfg.bucket, Key=key, LocalFilePath=str(src),
                    PartSize=10, MAXThread=5, EnableMD5=False,
                ),
                f"分片上传 {src.name}",
            )
        else:
            data = src.read_bytes()
            self._put_body(data, key, ctype, meta)
        return key

    def put_bytes(self, data: bytes, key: str, *, content_type: str = "") -> str:
        key = self._key(key)
        self._put_body(data, key, content_type or "application/octet-stream", None)
        return key

    def _put_body(self, data: bytes, key: str, ctype: str, meta: dict | None) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": self.cfg.bucket, "Key": key, "Body": data, "ContentType": ctype,
        }
        if meta:
            # COS 自定义头部必须 x-cos-meta- 前缀，值只能是 ASCII
            kwargs["Metadata"] = {f"x-cos-meta-{k}": str(v)[:100] for k, v in meta.items()}
        _with_retry(lambda: self.client.put_object(**kwargs), f"上传 {key}")

    # -- 读 --
    def exists(self, key: str) -> bool:
        key = self._key(key)
        for attempt in range(3):
            try:
                self.client.head_object(Bucket=self.cfg.bucket, Key=key)
                return True
            except Exception as exc:  # noqa: BLE001
                if _is_not_found(exc):
                    return False
                if attempt == 2 or not _is_retryable(exc):
                    raise StorageError(f"检查对象失败：{describe_error(exc)}") from None
                time.sleep(0.5 * (2 ** attempt))
        return False

    def stat(self, key: str) -> dict:
        key = self._key(key)
        try:
            head = _with_retry(
                lambda: self.client.head_object(Bucket=self.cfg.bucket, Key=key),
                f"读取对象信息 {key}",
            )
        except StorageError:
            raise
        size = head.get("Content-Length") or head.get("content-length") or 0
        return {
            "size": int(size),
            "etag": str(head.get("ETag") or head.get("etag") or "").strip('"'),
            "type": head.get("Content-Type") or head.get("content-type") or "",
        }

    def sign_url(self, key: str, expires: int = DEFAULT_EXPIRES) -> str:
        key = self._key(key)
        if expires <= 0:
            return self.public_url(key)
        expiry = max(60, min(int(expires), MAX_EXPIRES))
        try:
            url = _with_retry(
                lambda: self.client.get_presigned_url(
                    Bucket=self.cfg.bucket, Key=key, Expired=expiry, Method="GET",
                ),
                "生成签名链接",
            )
        except StorageError:
            raise
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise StorageError(f"签名链接异常：{url!r}")
        return url

    def public_url(self, key: str) -> str:
        """不走签名的直链：仅当桶是公有读，或 CDN 已配好回源鉴权时可用。"""
        key = self._key(key)
        quoted = urllib.parse.quote(key, safe="/-_.~")
        if self.cfg.domain:
            return f"{self.cfg.scheme}://{self.cfg.domain.strip('/')}/{quoted}"
        return f"{self.cfg.scheme}://{self.cfg.bucket}.cos.{self.cfg.region}.myqcloud.com/{quoted}"

    def delete(self, key: str) -> None:
        key = self._key(key)
        _with_retry(lambda: self.client.delete_object(Bucket=self.cfg.bucket, Key=key), f"删除 {key}")

    def list_prefix(self, prefix: str, limit: int = 1000) -> list[str]:
        base = self._key(prefix) if prefix else ""
        keys: list[str] = []
        marker: str | None = None
        while len(keys) < limit:
            params: dict[str, Any] = {
                "Bucket": self.cfg.bucket,
                "Prefix": base,
                "MaxKeys": min(1000, limit - len(keys)),
            }
            # Marker=None 会被 SDK 签名成空 query，触发 SignatureDoesNotMatch，必须省略
            if marker:
                params["Marker"] = marker
            resp = _with_retry(
                lambda: self.client.list_objects(**params),
                f"列举 {base or '/'}",
            )
            for item in resp.get("Contents") or []:
                key = item.get("Key")
                if key:
                    keys.append(str(key))
            truncated = str(resp.get("IsTruncated", "false")).lower() == "true"
            if not truncated or not keys:
                break
            marker = resp.get("NextMarker") or keys[-1]
        return keys[:limit]


# ---------------------------------------------------------------------------
# 本地目录（开发/测试/离线演练）
# ---------------------------------------------------------------------------

class LocalStorage(Storage):
    """把对象存成本地文件，接口和 COS 一致。

    用来在没开桶、没联网、没装 SDK 的情况下跑通整条链路；
    `sign_url` 返回 file:// 地址，仅供本机调试，不能给外部服务用。
    """

    def __init__(self, cfg: StorageConfig):
        cfg.validate()
        self.cfg = cfg
        self.root = Path(cfg.root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        key = key.strip().lstrip("/").replace("\\", "/")
        parts = [p for p in Path(key).parts if p not in ("", ".")]
        if not parts:
            raise StorageError(f"非法 Key：{key!r}")
        if any(p == ".." for p in parts) or Path(key).is_absolute() or ":" in parts[0]:
            raise StorageError(f"Key 越界：{key}")
        return self.root.joinpath(*parts)

    def put_path(self, path: str | Path, key: str, *, content_type: str = "",
                 meta: dict | None = None) -> str:
        src = Path(path)
        if not src.is_file():
            raise StorageError(f"待上传文件不存在：{src}")
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
        try:
            shutil.copyfile(src, tmp)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        return key

    def put_bytes(self, data: bytes, key: str, *, content_type: str = "") -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return key

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def stat(self, key: str) -> dict:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"对象不存在：{key}")
        return {"size": path.stat().st_size, "etag": file_fingerprint(path), "type": ""}

    def sign_url(self, key: str, expires: int = DEFAULT_EXPIRES) -> str:
        return self._path(key).as_uri()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_prefix(self, prefix: str, limit: int = 1000) -> list[str]:
        base = prefix.strip().lstrip("/")
        if not self.root.exists():
            return []
        out: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel.startswith(base):
                out.append(rel)
                if len(out) >= limit:
                    break
        return sorted(out)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def open_storage(cfg: StorageConfig | None) -> Storage | None:
    """按配置返回存储实例；未启用返回 None，调用方据此跳过归档。"""
    if cfg is None or not cfg.enabled:
        return None
    if cfg.provider == "local":
        return LocalStorage(cfg)
    if cfg.provider == "cos":
        # 客户机/打包机不一定装了 qcloud_cos；缺 SDK 时用标准库 REST 兜底，
        # 保证“源图上传 + 成品归档 + 签名 URL”这三件主流程仍然可用。
        try:
            import qcloud_cos  # noqa: F401
        except Exception:  # noqa: BLE001 - 缺包/依赖不全都走兜底
            try:
                from .cos_rest import CosRestStorage
            except ImportError:
                from cos_rest import CosRestStorage  # type: ignore
            return CosRestStorage(cfg)
        return CosStorage(cfg)
    raise StorageError(f"不支持的存储类型：{cfg.provider}")


# ---------------------------------------------------------------------------
# 自检：python3 app/storage.py
# ---------------------------------------------------------------------------

def _selfcheck() -> int:
    print("Key 规则演示：")
    demo = object_key("output", "第 7 套 服装.mp4", batch="20261024A",
                      prefix="prod", digest="9c1b2d4e5f607182")
    print("  ", demo)
    assert " " not in demo and demo.isascii(), demo

    tmp = Path.cwd() / ".storage-selfcheck"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        cfg = StorageConfig(provider="local", root=str(tmp))
        store = open_storage(cfg)
        assert store is not None
        src = tmp / "a.bin"
        src.write_bytes(b"x" * 1024)

        key = object_key("output", "a.bin", digest=file_fingerprint(src))
        store.put_path(src, key)
        assert store.exists(key), key
        assert store.stat(key)["size"] == 1024
        assert store.list_prefix("output") == [key], store.list_prefix("output")
        print("本地后端 OK：", store.sign_url(key))
        store.delete(key)
        assert not store.exists(key)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("自检通过（真实 COS 请运行 scripts/cos_check.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
