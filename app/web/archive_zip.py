#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端归档的「打包下载 ZIP」。

为什么不在 POST 里同步打完：一批 20~30 条视频就是几百 MB，从 COS 拉回来要几十秒到
几分钟；同步响应期间浏览器只有一个转圈，用户会以为「点了没反应」（这正是加这个按钮
之前的老问题）。所以：POST 建任务 → 后台线程逐条拉流写 zip → 前端轮询进度 → 打包好了
再用普通 GET 下载（走浏览器自己的下载器，有进度、可断点续传、不占前端内存）。

几个刻意的选择：
  · ZIP_STORED：mp4 已经是压缩过的，deflate 只烧 CPU 不省体积；
  · 每条先落 `.part` 临时文件再 `zip.write()`：中途失败不会在包里留下半截条目，
    包永远是完整的（失败的那条单独记进 failed，其余照常打包）；
  · 单个任务最多 500 条 / 8GB，避免有人一次把整个季度拉下来；
  · 成品 zip 留 12 小时（同事可以重下），到期由下一次 start() 顺手清掉。
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_KEYS = 500
MAX_BYTES = 8 * 1024 ** 3          # 8GB：远超正常一批，只用来兜底防爆盘
TTL_SECONDS = 12 * 3600            # 成品 zip 与任务记录的保留时长


class ArchiveZipError(RuntimeError):
    """带 HTTP 状态码的业务错误（路由层直接转成 _error）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_keys(raw) -> list[str]:
    """去空、去重、挡路径穿越；保持用户勾选顺序。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip().lstrip("/")
        if not key or key in out:
            continue
        parts = key.split("/")
        if any(part in ("", ".", "..") for part in parts):
            continue
        out.append(key)
    return out


def arcname_for(key: str, used: set[str]) -> str:
    """zip 内文件名：先只留文件名，重名时逐级带上父目录，仍撞就加序号。

    归档 key 形如 `<prefix>/output/<款号>/<批次>/<模板>-<hash>.mp4`，视频文件名本身
    带 hash，正常不会撞；这里只是防止不同批次恰好同名把后一条覆盖掉。
    """
    parts = [part for part in key.split("/") if part]
    if not parts:
        return f"video-{uuid.uuid4().hex[:6]}.mp4"
    for depth in range(1, len(parts) + 1):
        name = "/".join(parts[-depth:])
        if name not in used:
            used.add(name)
            return name
    base = parts[-1]
    stem, dot, ext = base.rpartition(".")
    index = 2
    while True:
        candidate = f"{stem}-{index}.{ext}" if dot else f"{base}-{index}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


@dataclass
class ArchiveZipTask:
    token: str
    style: str
    keys: list[str]
    path: Path
    state: str = "building"        # building | ready | failed
    done: int = 0
    failed: list[dict] = field(default_factory=list)
    bytes: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    # 后台线程与轮询线程共享进度：读写都很短，用一把小锁，不拖慢 copytree 式的拷贝循环
    mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total(self) -> int:
        return len(self.keys)

    def public(self) -> dict:
        with self.mutex:
            return {
                "token": self.token,
                "style": self.style,
                "state": self.state,
                "total": self.total,
                "done": self.done,
                "failed": [dict(item) for item in self.failed],
                "bytes": self.bytes,
                "error": self.error,
                "download_url": (f"/api/admin/archive-zip/{self.token}"
                                 if self.state == "ready" else ""),
                "expires_at": (self.finished_at or self.created_at) + TTL_SECONDS,
            }


class ArchiveZipManager:
    """打包任务的登记表 + 后台线程。进程内单例（与其它 manager 一样不跨进程）。"""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tasks: dict[str, ArchiveZipTask] = {}

    # -- 对外 --
    def start(self, store, raw_keys, style: str = "") -> dict:
        if store is None:
            raise ArchiveZipError("云端归档未启用，无法打包下载。", 503)
        keys = normalize_keys(raw_keys)
        if not keys:
            raise ArchiveZipError("没有可打包的视频，请重新勾选。", 400)
        if len(keys) > MAX_KEYS:
            raise ArchiveZipError(f"一次最多打包 {MAX_KEYS} 条，请分批下载。", 400)
        self.sweep()
        with self._lock:
            # 同一批连点两下：复用正在跑的任务，别把 COS 再拉一遍
            for task in self._tasks.values():
                if task.state == "building" and task.keys == keys:
                    return task.public()
        token = uuid.uuid4().hex[:12]
        task = ArchiveZipTask(token=token, style=str(style or "").strip()[:60],
                              keys=keys, path=self.root / f"archive-{token}.zip")
        with self._lock:
            self._tasks[token] = task
        threading.Thread(target=self._build, args=(task, store), daemon=True,
                         name=f"archive-zip-{token}").start()
        return task.public()

    def status(self, token: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(str(token or ""))
        return task.public() if task else None

    def path_of(self, token: str) -> Path | None:
        with self._lock:
            task = self._tasks.get(str(token or ""))
        if task is None or task.state != "ready":
            return None
        return task.path if task.path.is_file() else None

    def zip_name(self, token: str) -> str:
        with self._lock:
            task = self._tasks.get(str(token or ""))
        if task is None:
            return "archive.zip"
        style = task.style or "archive"
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(task.created_at))
        return f"{style}-{task.total}条-{stamp}.zip"

    def sweep(self) -> None:
        """清掉过期的任务记录与 zip；顺手捞走上次进程崩掉留下的 .part。"""
        cutoff = time.time() - TTL_SECONDS
        with self._lock:
            for token, task in list(self._tasks.items()):
                if task.state != "building" and task.created_at < cutoff:
                    self._tasks.pop(token, None)
        for pattern in ("archive-*.zip", "archive-*.part"):
            for path in self.root.glob(pattern):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- 后台 --
    def _build(self, task: ArchiveZipTask, store) -> None:
        try:
            with zipfile.ZipFile(task.path, "w", zipfile.ZIP_STORED) as zf:
                used: set[str] = set()
                for index, key in enumerate(task.keys):
                    part = task.path.with_suffix(f".{index}.part")
                    try:
                        with store.read_stream(key) as src, part.open("wb") as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                        size = part.stat().st_size
                        if size <= 0:
                            raise ArchiveZipError(f"对象为空：{key}")
                        zf.write(part, arcname_for(key, used))
                    except Exception as exc:  # noqa: BLE001 - 单条失败不该毁掉整包
                        with task.mutex:
                            task.failed.append({"key": key, "error": str(exc)[:200]})
                        continue
                    finally:
                        part.unlink(missing_ok=True)
                    with task.mutex:
                        task.done += 1
                        task.bytes += size
                        too_big = task.bytes > MAX_BYTES
                    if too_big:
                        raise ArchiveZipError("打包体积超过 8GB，请分批下载。", 507)
            if task.done == 0:
                raise ArchiveZipError("选中的视频一个也没能取回来，请稍后重试或先生成链接。", 502)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要让前端看到原因
            with task.mutex:
                task.state = "failed"
                task.error = str(exc)[:300] or exc.__class__.__name__
                task.finished_at = time.time()
            task.path.unlink(missing_ok=True)
            return
        with task.mutex:
            task.state = "ready"
            task.finished_at = time.time()
