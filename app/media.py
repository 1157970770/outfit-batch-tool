#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材适配层：本地文件 → 官方可用的 URL / Base64，以及官方硬约束预检。

背景：
  · 官方万相 3.0 只接受公网 URL、`oss://` 临时 URL，图片额外支持 Base64；
  · RunningHub 的 `/media/upload/binary` 在官方版不存在；
  · 桌面版已经有腾讯云 COS 子账号（只能写成品区），所以服装图直接传到
    `{prefix}/output/source/` 下，签 7 天 URL，100 条任务共用一次上传。

职责边界：
  · 本文件只管“本地文件怎么变成 URL/Base64”和“图片/视频是否符合官方限制”；
  · 真正的对象存储读写仍在 app/storage.py；
  · 不做 HTTP 生成调用（那是 app/wan_api.py）。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

try:                      # 作为包导入（界面/测试）
    from .storage import (
        Storage, StorageError, file_fingerprint, guess_content_type, object_key,
    )
except ImportError:       # 直接 python3 app/media.py 时
    from storage import (  # type: ignore
        Storage, StorageError, file_fingerprint, guess_content_type, object_key,
    )

MAX_IMAGE_MB = 20.0
MAX_VIDEO_MB = 100.0
MAX_AUDIO_MB = 15.0
MAX_VIDEO_SECONDS = 15.0
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".wav", ".mp3"}
IMAGE_FORMATS = {"JPEG", "PNG", "BMP", "WEBP"}
SOURCE_EXPIRES = 7 * 24 * 3600


class MediaError(RuntimeError):
    """素材层错误，message 直接面向用户。"""


# ---------------------------------------------------------------------------
# 图片预检
# ---------------------------------------------------------------------------

def inspect_image(path: str | Path) -> dict:
    """用 Pillow 读图片元信息；没装 Pillow 时返回空字典（只告警）。"""
    src = Path(path)
    info: dict = {"path": str(src), "size_mb": round(src.stat().st_size / 1024 / 1024, 3)}
    try:
        from PIL import Image
    except ImportError:
        info["warn"] = "未安装 Pillow，跳过图片格式/尺寸预检"
        return info

    try:
        with Image.open(src) as img:
            info.update({
                "format": (img.format or "").upper(),
                "mode": img.mode,
                "width": img.width,
                "height": img.height,
                "has_alpha": ("A" in img.getbands()) or ("transparency" in img.info),
            })
    except Exception as exc:  # noqa: BLE001 - 坏图直接给用户原因
        raise MediaError(f"图片打不开：{src.name}（{exc}）") from None
    return info


def validate_image(path: str | Path) -> dict:
    """校验官方图片限制；返回元信息，不合规抛 MediaError。"""
    info = inspect_image(path)
    size_mb = info.get("size_mb", 0)
    if size_mb > MAX_IMAGE_MB:
        raise MediaError(f"图片 {Path(path).name} 有 {size_mb:.1f}MB，超过官方 20MB 上限")

    fmt = info.get("format")
    if fmt and fmt not in IMAGE_FORMATS:
        raise MediaError(f"图片 {Path(path).name} 格式 {fmt} 不支持（只收 JPEG/PNG/BMP/WEBP）")
    width, height = info.get("width"), info.get("height")
    if width and height:
        if not (240 <= width <= 8000 and 240 <= height <= 8000):
            raise MediaError(f"图片 {Path(path).name} 尺寸 {width}×{height}，单边需在 240–8000px")
        ratio = max(width / height, height / width)
        if ratio > 8.0:
            raise MediaError(f"图片 {Path(path).name} 宽高比 {ratio:.1f}:1，超过 8:1 上限")
    return info


def _flatten_to_jpeg(src: Path) -> Path:
    """把带透明通道/非 RGB 的图合成白底并转成 JPEG 临时文件。"""
    try:
        from PIL import Image
    except ImportError:
        raise MediaError("图片带透明通道，但本机没有 Pillow，无法自动转白底；请另存为 JPG") from None

    fd, raw = tempfile.mkstemp(prefix="wan-src-", suffix=".jpg")
    os.close(fd)
    dest = Path(raw)
    try:
        with Image.open(src) as img:
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                out = background
            else:
                out = img.convert("RGB")
            out.save(dest, format="JPEG", quality=95)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest


def prepare_image(path: str | Path) -> tuple[Path, str]:
    """返回 (可上传的本地路径, content_type)。带 alpha 的图会转成临时 JPEG。"""
    src = Path(path)
    info = validate_image(src)
    if info.get("has_alpha") or info.get("mode") not in (None, "RGB", "L"):
        tmp = _flatten_to_jpeg(src)
        return tmp, "image/jpeg"
    ctype = guess_content_type(src.name)
    if ctype == "application/octet-stream":
        ctype = "image/png" if src.suffix.lower() == ".png" else "image/jpeg"
    return src, ctype


def to_data_uri(path: str | Path) -> str:
    """图片转 Base64 Data URI；官方只对图片支持这种写法。"""
    src, ctype = prepare_image(path)
    try:
        data = src.read_bytes()
    finally:
        if src != Path(path):
            src.unlink(missing_ok=True)
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise MediaError(f"图片 Base64 前已超过 {MAX_IMAGE_MB:g}MB 上限")
    return f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"


# ---------------------------------------------------------------------------
# 视频/音频预检
# ---------------------------------------------------------------------------

def probe_video(path: str | Path) -> dict:
    """ffprobe 读时长/分辨率/帧率；没有 ffprobe 返回空字典。"""
    src = Path(path)
    info = {"path": str(src), "size_mb": round(src.stat().st_size / 1024 / 1024, 3)}
    exe = shutil.which("ffprobe")
    if not exe:
        info["warn"] = "本机没有 ffprobe，跳过视频时长/帧率预检"
        return info
    try:
        result = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate:format=duration",
             "-of", "json", str(src)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            info["warn"] = f"ffprobe 读不了：{result.stderr.strip()[:120]}"
            return info
        import json
        data = json.loads(result.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        duration = (data.get("format") or {}).get("duration")
        if duration is not None:
            info["duration"] = round(float(duration), 2)
        if stream.get("width"):
            info["width"] = int(stream["width"])
        if stream.get("height"):
            info["height"] = int(stream["height"])
        rate = str(stream.get("r_frame_rate") or "")
        if "/" in rate:
            num, den = rate.split("/", 1)
            try:
                info["fps"] = round(float(num) / float(den), 2) if float(den) else None
            except (ValueError, ZeroDivisionError):
                pass
        elif rate:
            try:
                info["fps"] = round(float(rate), 2)
            except ValueError:
                pass
    except (OSError, subprocess.SubprocessError, ValueError):
        info["warn"] = "ffprobe 执行失败，跳过视频预检"
    return info


def validate_video(path: str | Path) -> dict:
    info = probe_video(path)
    src = Path(path)
    if src.suffix.lower() not in VIDEO_EXT:
        raise MediaError(f"视频 {src.name} 扩展名不支持（只收 mp4/mov）")
    if info.get("size_mb", 0) > MAX_VIDEO_MB:
        raise MediaError(f"视频 {src.name} 有 {info['size_mb']:.1f}MB，超过官方 100MB 上限")
    duration = info.get("duration")
    if duration is not None and not 1.0 <= duration <= MAX_VIDEO_SECONDS:
        raise MediaError(f"视频 {src.name} 时长 {duration:.1f}s，单段需在 1–15 秒")
    width, height = info.get("width"), info.get("height")
    if width and height:
        if not (240 <= width <= 4096 and 240 <= height <= 4096):
            raise MediaError(f"视频 {src.name} 尺寸 {width}×{height}，单边需在 240–4096px")
        if max(width / height, height / width) > 8.0:
            raise MediaError(f"视频 {src.name} 宽高比超过 8:1")
    fps = info.get("fps")
    if fps and fps < 16:
        raise MediaError(f"视频 {src.name} 帧率 {fps}fps，官方要求 ≥16fps")
    return info


def validate_audio(path: str | Path) -> dict:
    src = Path(path)
    size_mb = round(src.stat().st_size / 1024 / 1024, 3)
    if src.suffix.lower() not in AUDIO_EXT:
        raise MediaError(f"音频 {src.name} 扩展名不支持（只收 wav/mp3）")
    if size_mb > MAX_AUDIO_MB:
        raise MediaError(f"音频 {src.name} 有 {size_mb:.1f}MB，超过官方 15MB 上限")
    return {"path": str(src), "size_mb": size_mb}


# ---------------------------------------------------------------------------
# 服装图 → COS 源目录
# ---------------------------------------------------------------------------

class SourceUploader:
    """把本地素材传到可写桶，返回签名 URL。

    默认传到 `{cos.prefix}/output/source/`：现有子账号的权限是
    `video-output-.../prod/output/*`，放这里不用改 CAM；它不是成品，
    文档要求配 30 天生命周期或批跑完手工清理。
    """

    def __init__(self, storage: Storage, prefix: str = "", *,
                 expires: int = SOURCE_EXPIRES,
                 log: Callable[[str], None] | None = None):
        self.storage = storage
        self.prefix = (prefix or "").strip().strip("/")
        self.expires = expires
        self.log = log or (lambda _msg: None)
        self._cache: dict[str, str] = {}

    def default_prefix(self) -> str:
        if self.prefix:
            return self.prefix
        base = ""
        try:
            base = self.storage.cfg.clean_prefix()
        except Exception:  # noqa: BLE001 - LocalStorage 也有 clean_prefix
            base = ""
        # object_key 会自动追加 /source/，所以这里只到 output
        return f"{base}/output".strip("/") if base else "output"

    def key_for(self, path: str | Path, name: str | None = None) -> str:
        src = Path(path)
        return object_key(
            "source", name or src.name, prefix=self.default_prefix(),
            digest=file_fingerprint(src),
        )

    def upload(self, path: str | Path, name: str | None = None) -> str:
        src = Path(path)
        cache_key = str(src.resolve())
        if cache_key in self._cache:
            return self._cache[cache_key]

        key = self.key_for(src, name=name)
        ctype = guess_content_type(src.name)
        try:
            if not self.storage.exists(key):
                self.log(f"上传源图 → {key}")
                self.storage.put_path(src, key, content_type=ctype)
            else:
                self.log(f"源图已存在，复用 → {key}")
            url = self.storage.sign_url(key, self.expires)
        except StorageError as exc:
            raise MediaError(f"源图上传失败：{exc}") from None
        if not url.startswith(("http://", "https://")):
            raise MediaError(f"源图签名链接不可用（{url[:60]}），请检查 COS provider 配置")
        self._cache[cache_key] = url
        return url


def uploader_from_env(log: Callable[[str], None] | None = None) -> SourceUploader | None:
    """命令行用：有 COS_* 环境变量就建上传器；没有返回 None（图片走 Base64）。"""
    try:
        from .storage import StorageConfig, open_storage
    except ImportError:
        try:
            from storage import StorageConfig, open_storage  # type: ignore
        except ImportError:
            return None
    cfg = StorageConfig.from_env()
    if not cfg.enabled or not (cfg.bucket and cfg.secret_id and cfg.secret_key):
        return None
    try:
        storage = open_storage(cfg)
    except Exception:  # noqa: BLE001 - 配置不完整就当没有
        return None
    if storage is None:
        return None
    prefix = (os.environ.get("COS_SOURCE_PREFIX") or "").strip().strip("/")
    if not prefix:
        base = cfg.clean_prefix()
        prefix = f"{base}/output" if base else "output"
    return SourceUploader(storage, prefix, log=log)


# ---------------------------------------------------------------------------
# 统一解析
# ---------------------------------------------------------------------------

def is_remote(value: str) -> bool:
    return value.startswith(("http://", "https://", "data:", "oss://"))


def resolve_image(path: str | Path, uploader: SourceUploader | None) -> str:
    src = Path(path)
    if not src.exists():
        raise MediaError(f"图片不存在：{src}")
    if uploader is not None:
        prepared, _ctype = prepare_image(src)
        name = src.name if prepared == src else f"{src.stem}.jpg"
        try:
            return uploader.upload(prepared, name=name)
        finally:
            if prepared != src:
                prepared.unlink(missing_ok=True)
    return to_data_uri(src)


def resolve_video(path: str | Path, uploader: SourceUploader | None) -> str:
    src = Path(path)
    if not src.exists():
        raise MediaError(f"视频不存在：{src}")
    if uploader is None:
        raise MediaError(
            f"本地视频 {src.name} 需要先上传成公网 URL（官方视频不支持 Base64）。"
            "请配置 COS_* 环境变量，或改用已有 URL。"
        )
    validate_video(src)
    return uploader.upload(src)


def resolve_audio(path: str | Path, uploader: SourceUploader | None) -> str:
    src = Path(path)
    if not src.exists():
        raise MediaError(f"音频不存在：{src}")
    if uploader is None:
        raise MediaError(
            f"本地音频 {src.name} 需要先上传成公网 URL（官方音频不支持 Base64）。"
        )
    validate_audio(src)
    return uploader.upload(src)


def resolve_body(body: dict, uploader: SourceUploader | None, *,
                 cache: dict | None = None) -> dict:
    """把 body 里的本地路径就地换成 URL/Base64；同一文件只处理一次。"""
    cache = cache if cache is not None else {}

    def resolve_one(value: str, kind: str) -> str:
        if is_remote(value):
            return value
        key = f"{kind}:{Path(value).resolve()}"
        if key in cache:
            return cache[key]
        if kind == "image":
            url = resolve_image(value, uploader)
        elif kind == "video":
            url = resolve_video(value, uploader)
        else:
            url = resolve_audio(value, uploader)
        cache[key] = url
        return url

    for field_name, kind in (("imageUrls", "image"), ("videoUrls", "video"),
                             ("audioUrls", "audio")):
        values = body.get(field_name) or []
        if values:
            body[field_name] = [resolve_one(str(v), kind) for v in values]
    return body


def describe_media_source(url: str) -> str:
    """日志里只显示来源类型，不打印完整签名 URL。"""
    if url.startswith("data:"):
        return "base64"
    if url.startswith("oss://"):
        return "oss"
    if url.startswith(("http://", "https://")):
        return "url"
    return "unknown"
