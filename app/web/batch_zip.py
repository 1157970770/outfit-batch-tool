#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按批次「整批下载」的后台打包任务。

为什么不在 GET 里同步打完：一批几十条视频就是几个 GB，zip 写盘要几十秒；这期间
浏览器连响应头都没有，只有一个转圈 —— 用户会以为「点了没反应」（这正是加进度
显示之前的老问题）。所以：POST 建任务 → 后台线程逐条写 zip 并上报进度 → 前端
轮询 `已打包 N / 总数` → 打完再用普通 GET 下载（走浏览器自己的下载器，有进度、
可断点续传、不占前端内存）。

几个刻意的选择（与 archive_zip 同源，但这里是本机文件、那边是 COS 流）：
  · ZIP_STORED：mp4 已经压缩过，deflate 只烧 CPU 不省体积；改成 STORED 后打包
    基本是纯 I/O，几十条视频快很多；
  · 单条读不到（文件被删 / 权限）只记进 failed，其余照常打包；
  · 单个任务最多 2000 条 / 8GB，避免有人一次把整个季度拉下来；
  · 成品 zip 留 6 小时（可以重下），到期由下一次 start() 顺手清掉。
"""
from __future__ import annotations

import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.web.archive_zip import arcname_for

MAX_FILES = 2000
MAX_BYTES = 8 * 1024 ** 3          # 8GB：远超正常一批，只用来兜底防爆盘
TTL_SECONDS = 6 * 3600


class BatchZipError(RuntimeError):
    """带 HTTP 状态码的业务错误（路由层直接转成 _error）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class BatchZipTask:
    token: str
    batch_id: str
    label: str
    owner: str
    admin: bool
    entries: list[tuple[str, Path]]
    path: Path
    state: str = "building"        # building | ready | failed
    done: int = 0
    failed: list[dict] = field(default_factory=list)
    bytes: int = 0
    total_bytes: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    # 后台线程与轮询线程共享进度：读写都很短，用一把小锁，不拖慢拷贝循环
    mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total(self) -> int:
        return len(self.entries)

    def public(self) -> dict:
        with self.mutex:
            return {
                "token": self.token,
                "batch_id": self.batch_id,
                "label": self.label,
                "state": self.state,
                "total": self.total,
                "done": self.done,
                "failed": [dict(item) for item in self.failed],
                "bytes": self.bytes,
                "total_bytes": self.total_bytes,
                "error": self.error,
                "download_url": (f"/api/batches/{self.batch_id}/files.zip/jobs/"
                                 f"{self.token}" if self.state == "ready" else ""),
                "expires_at": (self.finished_at or self.created_at) + TTL_SECONDS,
            }


class BatchZipManager:
    """打包任务的登记表 + 后台线程。进程内单例（与其它 manager 一样不跨进程）。"""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tasks: dict[str, BatchZipTask] = {}

    # -- 对外 --
    def start(self, batch_id: str, label: str, files: list[tuple[str, Path]],
              owner: str = "", admin: bool = False) -> dict:
        files = [(str(name), Path(path)) for name, path in (files or [])]
        if not files:
            raise BatchZipError("暂时没有可下载的视频。", 404)
        if len(files) > MAX_FILES:
            raise BatchZipError(f"一次最多打包 {MAX_FILES} 条，请分批下载。", 400)
        self.sweep()
        with self._lock:
            # 同一批连点两下：复用正在跑（或已经打好）的任务，别把几个 GB 再写一遍。
            # 只复用同一个人的任务：别人的 token 他打不开，复用等于给他 404。
            for task in self._tasks.values():
                if task.batch_id != batch_id or task.owner != str(owner or ""):
                    continue
                if task.state == "building":
                    return task.public()
                if task.state == "ready" and task.path.is_file():
                    return task.public()
            token = uuid.uuid4().hex[:12]
            task = BatchZipTask(token=token, batch_id=str(batch_id),
                                label=str(label or batch_id)[:60],
                                owner=str(owner or ""), admin=bool(admin),
                                entries=files,
                                path=self.root / f"batch-{token}.zip")
            self._tasks[token] = task
        task.total_bytes = self._total_bytes(files)
        threading.Thread(target=self._build, args=(task,), daemon=True,
                         name=f"batch-zip-{token}").start()
        return task.public()

    def status(self, token: str, user: dict | None = None) -> dict | None:
        task = self._get(token)
        if task is None or not self._allowed(task, user):
            return None
        return task.public()

    def path_of(self, token: str, user: dict | None = None) -> Path | None:
        task = self._get(token)
        if task is None or task.state != "ready" or not self._allowed(task, user):
            return None
        return task.path if task.path.is_file() else None

    def zip_name(self, token: str) -> str:
        task = self._get(token)
        if task is None:
            return "batch.zip"
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(task.created_at))
        return f"{task.label}-{task.total}条-{stamp}.zip"

    def sweep(self) -> None:
        """清掉过期的任务记录与 zip；顺手捞走上次进程崩掉留下的半截包。"""
        cutoff = time.time() - TTL_SECONDS
        with self._lock:
            for token, task in list(self._tasks.items()):
                if task.state != "building" and task.created_at < cutoff:
                    self._tasks.pop(token, None)
        for path in self.root.glob("batch-*.zip"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    # -- 内部 --
    def _get(self, token: str) -> BatchZipTask | None:
        with self._lock:
            return self._tasks.get(str(token or ""))

    @staticmethod
    def _allowed(task: BatchZipTask, user: dict | None) -> bool:
        """任务 token 不可猜，但仍按归属复核一次：别人的批次包不能靠拦住 URL 拿到。"""
        if user is None:
            return True
        if task.admin or user.get("role") == "admin":
            return True
        return str(user.get("username") or "") == task.owner

    @staticmethod
    def _total_bytes(files: list[tuple[str, Path]]) -> int:
        total = 0
        for _, path in files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _build(self, task: BatchZipTask) -> None:
        try:
            used: set[str] = set()
            with zipfile.ZipFile(task.path, "w", zipfile.ZIP_STORED) as zf:
                for name, src in task.entries:
                    arcname = arcname_for(name, used)
                    try:
                        if not src.is_file():
                            raise BatchZipError(f"文件已不在磁盘上：{src.name}")
                        size = src.stat().st_size
                        zf.write(src, arcname)
                    except Exception as exc:  # noqa: BLE001 - 单条失败不该毁掉整包
                        with task.mutex:
                            task.failed.append({"name": name, "error": str(exc)[:200]})
                        continue
                    with task.mutex:
                        task.done += 1
                        task.bytes += size
                        too_big = task.bytes > MAX_BYTES
                    if too_big:
                        raise BatchZipError("打包体积超过 8GB，请分批下载。", 507)
            if task.done == 0:
                raise BatchZipError("包里的视频一个都没能读到，请确认成片还在后重试。", 502)
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
