#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远端链接 → 对象存储 的流式转存器。

解决的具体问题：RunningHub 的 results[0].url **24 小时后永久失效**。
以前的做法是"先下到本机、再想办法上传"，磁盘越跑越满、机器一关机就断档。
这里改成一次搬运：边下边传、校验长度、失败重试、重复执行命中同一个 Key。

对外只有两个函数：
    transfer_url(url, storage, key)        # 单个
    transfer_many(items, storage)          # 批量并发

零第三方依赖（只用 urllib），COS 的写入交给 app/storage.py。
"""

from __future__ import annotations

import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from app.storage import (
    Storage, StorageError, file_fingerprint, guess_content_type, object_key,
)

CHUNK = 1 << 20                     # 1MB 一块
USER_AGENT = "batch-video-archiver/1.0"
EXPIRED_HINTS = ("expired", "expire", "AccessDenied", "SignatureDoesNotMatch",
                 "NoSuchKey", "过期")


class TransferError(RuntimeError):
    """转存失败；message 面向用户（含"链接可能已过期"这类判断）。"""


@dataclass
class TransferResult:
    url: str
    key: str = ""
    size: int = 0
    signed_url: str = ""
    skipped: bool = False
    seconds: float = 0.0
    error: str = ""
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        return "skipped" if self.skipped else "ok"


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

def _request(url: str, method: str = "GET", timeout: int = 60) -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })


def head_remote(url: str, timeout: int = 30) -> dict | None:
    """拿远端文件大小；服务端不支持 HEAD 时返回 None（不视为错误）。"""
    try:
        with urllib.request.urlopen(_request(url, "HEAD", timeout), timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            return {
                "size": int(length) if length and length.isdigit() else None,
                "content_type": resp.headers.get("Content-Type", ""),
            }
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _looks_like_error_page(head: bytes, content_type: str) -> bool:
    """临时链接过期时，很多服务返回的是 XML/HTML 错误页而不是 404。"""
    sample = head[:400].lstrip()
    if content_type.startswith(("text/html", "application/xml", "text/xml")):
        return sample.startswith((b"<", b"{"))
    return sample.startswith((b"<!DOCTYPE", b"<html", b"<?xml"))


def _classify_http_error(exc: urllib.error.HTTPError, url: str) -> TransferError:
    try:
        body = exc.read(600).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        body = ""
    lowered = body.lower()
    reason = f"HTTP {exc.code}"
    if exc.code == 404:
        return TransferError(f"{reason}：远端文件不存在（可能已被清理）")
    if exc.code in (401, 403) or any(h.lower() in lowered for h in EXPIRED_HINTS):
        return TransferError(
            f"{reason}：链接已失效或无权限（生成结果 24 小时后永久过期，成功就要立刻转存）。"
            f"服务端原话：{body[:200] or exc.reason}"
        )
    return TransferError(f"{reason}：{body[:200] or exc.reason}")


# ---------------------------------------------------------------------------
# 单个转存
# ---------------------------------------------------------------------------

def transfer_url(url: str, storage: Storage, key: str = "", *,
                 expires: int = 7 * 24 * 3600,
                 timeout: int = 600,
                 retries: int = 3,
                 progress: Callable[[int, int], None] | None = None,
                 batch: str = "", prefix: str = "output",
                 content_type: str = "",
                 tmp_dir: str | Path | None = None) -> TransferResult:
    """把一个远端 URL 搬进存储。

    - `key` 为空时按内容指纹自动生成（同一份内容重复转存 → 同一个对象）
    - 已存在且 Key 带指纹 → 直接跳过，不重复下载
    - 失败重试 `retries` 次，只有 5xx/网络错误才重试，403/404 立即失败
    """
    started = time.time()
    result = TransferResult(url=url, key=key)

    if not url.startswith(("http://", "https://")):
        result.error = f"不是可下载的 http(s) 链接：{url[:80]}"
        result.seconds = time.time() - started
        return result

    remote = head_remote(url, timeout=min(30, timeout))
    if remote and remote.get("size"):
        result.size = int(remote["size"])
    if not content_type and remote:
        content_type = remote.get("content_type") or ""

    if key and storage.exists(key):
        stored = _safe_stat(storage, key)
        if stored is None or not result.size or stored.get("size") == result.size:
            result.key = key
            result.skipped = True
            result.seconds = time.time() - started
            result.signed_url = _safe_sign(storage, key, expires)
            return result

    last_error: str = ""
    for attempt in range(max(1, retries)):
        tmp_path: Path | None = None
        try:
            tmp_path, downloaded = _download(url, timeout=timeout, progress=progress,
                                             tmp_dir=tmp_dir)
            result.size = downloaded
            if not key:
                result.key = object_key(
                    "output", _basename_from_url(url), batch=batch, prefix=prefix,
                    digest=file_fingerprint(tmp_path),
                )
            ctype = guess_content_type(result.key)
            if ctype == "application/octet-stream" and content_type:
                ctype = content_type
            result.key = storage.put_path(tmp_path, result.key, content_type=ctype)
            result.content_type = ctype
            result.skipped = False
            result.seconds = time.time() - started
            result.signed_url = _safe_sign(storage, result.key, expires)
            return result
        except TransferError as exc:
            last_error = str(exc)
            if "已失效" in last_error or "不存在" in last_error:
                break                       # 链接过期，重试没有意义
        except StorageError as exc:
            last_error = f"入库失败：{exc}"
        except (urllib.error.URLError, OSError) as exc:
            last_error = f"网络错误：{exc}"
        except Exception as exc:  # noqa: BLE001 - 单条失败不能带崩整批
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        if attempt < retries - 1:
            time.sleep(1.0 * (2 ** attempt))

    result.error = last_error or "未知错误"
    result.seconds = time.time() - started
    return result


def _safe_stat(storage: Storage, key: str) -> dict | None:
    try:
        return storage.stat(key)
    except StorageError:
        return None


def _safe_sign(storage: Storage, key: str, expires: int) -> str:
    try:
        return storage.sign_url(key, expires)
    except StorageError:
        return ""


def _basename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path) or "download.bin"
    if "." not in name:
        name += ".mp4"
    return name


def _download(url: str, *, timeout: int,
              progress: Callable[[int, int], None] | None = None,
              tmp_dir: str | Path | None = None) -> tuple[Path, int]:
    """下载到临时目录（不落在输出目录，也不整段进内存）。

    `tmp_dir` 可指定到磁盘更宽松的位置；沙箱/受限环境下必须显式传。
    """
    if tmp_dir:
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="transfer-", suffix=".part", dir=tmp_dir)
    os.close(fd)
    tmp_path = Path(raw_path)
    done = 0
    total = 0
    try:
        try:
            resp = urllib.request.urlopen(_request(url, "GET", timeout), timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc, url) from None
        except urllib.error.URLError as exc:
            raise TransferError(f"下载失败：{exc.reason}") from None

        with resp, tmp_path.open("wb") as fh:
            content_type = resp.headers.get("Content-Type", "") or ""
            length = resp.headers.get("Content-Length") or ""
            total = int(length) if length.isdigit() else 0
            first = True
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                if first:
                    first = False
                    if _looks_like_error_page(chunk, content_type):
                        raise TransferError(
                            "远端返回的不是文件，而是错误页/XML（链接多半已过期）"
                        )
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    try:
                        progress(done, total)
                    except Exception:  # noqa: BLE001 - 回调出错不能影响搬运
                        pass
        if done == 0:
            raise TransferError("下载到 0 字节（链接可能返回了空内容）")
        if total and done != total:
            raise TransferError(f"下载不完整：{done}/{total} 字节")
        return tmp_path, done
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 批量转存
# ---------------------------------------------------------------------------

@dataclass
class TransferItem:
    url: str
    key: str = ""          # 留空则按内容指纹生成
    batch: str = ""
    prefix: str = "output"
    name: str = ""         # 仅用于日志


def transfer_many(items: Sequence[TransferItem], storage: Storage, *,
                  workers: int = 4, expires: int = 7 * 24 * 3600,
                  timeout: int = 600, retries: int = 3,
                  tmp_dir: str | Path | None = None,
                  progress: Callable[[TransferResult], None] | None = None
                  ) -> list[TransferResult]:
    """并发转存；顺序与入参一致，单条失败不影响其它条。"""
    results: list[TransferResult] = [TransferResult(url=item.url) for item in items]

    def work(index: int) -> None:
        item = items[index]
        res = transfer_url(
            item.url, storage, item.key, expires=expires, timeout=timeout,
            retries=retries, batch=item.batch, prefix=item.prefix, tmp_dir=tmp_dir,
        )
        results[index] = res
        if progress:
            try:
                progress(res)
            except Exception:  # noqa: BLE001
                pass

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, range(len(items))))
    return results


def summarize(results: Iterable[TransferResult]) -> dict:
    rows = list(results)
    return {
        "total": len(rows),
        "ok": sum(1 for r in rows if r.ok and not r.skipped),
        "skipped": sum(1 for r in rows if r.skipped),
        "failed": sum(1 for r in rows if r.error),
        "bytes": sum(r.size for r in rows if r.ok),
        "seconds": round(sum(r.seconds for r in rows), 1),
        "errors": [{"url": r.url, "error": r.error} for r in rows if r.error],
    }
