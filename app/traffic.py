#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""COS 外网下行流量的本地计账：**只观测，不改变任何业务行为**。

为什么需要这个模块
------------------
腾讯云 COS 只在「数据从 COS 经公网送出去」时收费（外网上行、同地域内网都免费，
见 https://cloud.tencent.com/document/buy-guide/436/53863 ）。而本项目的出口散在
五个地方，任何一个都不打日志：

  1. ``preview``   ``/api/templates/<id>/preview``   我们自己代理视频字节给浏览器
  2. ``download``  ``/api/templates/<id>/download``  同上，落成附件
  3. ``template``  万相服务端按我们签的 ``videoUrls`` 去拉模板视频
  4. ``image``     万相服务端按我们签的 ``imageUrls`` 去拉定妆图 / 细节图
  5. ``archive``   管理端给同事签的成品归档直链

想按来源对账，只能在这五处各记一笔，再和 COS 控制台的外网下行日曲线比。
没有这一层，就只能在账单上看到一个总数，猜不出钱花在哪。

两类事件的精度不同，报表里必须分开看
------------------------------------
* **实测**（``preview`` / ``download``）：字节真的流过本进程，是数出来的。
* **推定**（``template`` / ``image`` / ``archive``）：我们只交出了签名 URL，
  真正的拉取发生在阿里云或同事的电脑上，只能按对象的真实大小估。

所以报表里不要把「实测」和「推定」加成一个数就当账；对账时用实测那一半去
校准推定那一半的比例（例如万相是否真的每条都拉、有没有 CDN 缓存）。

写入约定
--------
* 一天一个文件：``<dir>/YYYY-MM-DD.jsonl``，一行一个事件，UTF-8。
* 单行尽量 < 512 字节，配合 ``O_APPEND`` 的追加写，多线程 / 多进程追加不会互相截断。
* **任何异常都必须吞掉**：这是观测代码，绝不能让一次统计失败把视频请求打挂。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# 允许写进账本的字段白名单：只留对账真正用得上的，避免行变胖。
_FIELDS = ("tid", "job", "n", "st", "rng", "m")

# 来源标签 → 中文名（报表用）
SOURCE_LABELS = {
    "preview": "模板预览代理",
    "download": "候选视频下载",
    "template": "万相拉模板视频",
    "image": "万相拉定妆图",
    "archive": "归档直链签名",
}

# 实测来源 vs 推定来源
MEASURED = frozenset({"preview", "download"})

_lock = threading.Lock()
_dir: Path | None = None
_enabled = False


def configure(directory: str | Path | None, enabled: bool = True) -> None:
    """设定账本目录（通常是 ``<data_dir>/traffic``）。

    ``OUTFIT_TRAFFIC=0`` 环境变量可整体关掉，用于排障时排除干扰。
    """
    global _dir, _enabled
    _dir = Path(directory) if directory else None
    _enabled = bool(enabled) and _dir is not None and os.environ.get("OUTFIT_TRAFFIC") != "0"


def is_enabled() -> bool:
    return _enabled


def directory() -> Path | None:
    return _dir


def record(source: str, nbytes: int = 0, **fields) -> None:
    """记一笔出口。``source`` 见 ``SOURCE_LABELS``；``nbytes`` 是本次送出的字节。

    ``fields`` 只接受白名单里的键：``tid``（模板 id）、``job``（批次/任务名）、
    ``n``（张数）、``st``（上游 HTTP 状态）、``rng``（是否 Range 请求）、``m``（方法）。
    """
    if not _enabled:
        return
    try:
        now = time.time()
        line: dict = {"t": round(now, 3), "src": str(source), "b": int(nbytes or 0)}
        for key, value in fields.items():
            if key not in _FIELDS or value is None or value == "":
                continue
            line[key] = value
        payload = json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        target = _dir / f"{day}.jsonl"  # type: ignore[operator]
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(payload)
    except Exception:  # noqa: BLE001 - 观测失败绝不能影响业务
        pass


def read_file(path: Path):
    """逐行读一个账本文件；坏行跳过，不抛异常。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def list_days(directory: Path | None = None) -> list[str]:
    """账本里出现过的日期（升序）。"""
    base = Path(directory) if directory else _dir
    if base is None or not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.jsonl") if len(p.stem) == 10)


def collect(days: int = 7, directory: str | Path | None = None) -> dict:
    """聚合最近 ``days`` 天的账本。

    返回::

        {"first": "2026-11-09", "last": "2026-11-15", "days": 7,
         "files": [...],
         "by_day": {"2026-11-09": {"preview": {"n": 12, "b": 3145728}, ...}, ...},
         "totals": {"preview": {"n": 12, "b": ...}, ...},
         "templates": {"2026-11-09": <当天账本里出现过的模板 id 集合>, ...},
         "ranges": {"2026-11-09": {"total": 20, "range": 14}, ...}}

    缺失的日期不出现在 ``by_day`` 里；``totals`` 只统计实际读到的文件。
    """
    base = Path(directory) if directory else _dir
    empty = {"first": "", "last": "", "days": 0, "files": [], "by_day": {},
             "totals": {}, "templates": {}, "ranges": {}}
    if base is None:
        return empty
    available = list_days(base)
    if not available:
        return empty
    wanted = available[-max(1, int(days)):] if days else available
    by_day: dict[str, dict[str, dict]] = {}
    totals: dict[str, dict] = {}
    templates: dict[str, set] = {}
    ranges: dict[str, dict] = {}
    for day in wanted:
        day_src: dict[str, dict] = {}
        tids: set = set()
        rng_total = rng_hit = 0
        for item in read_file(base / f"{day}.jsonl"):
            src = str(item.get("src") or "?")
            nbytes = int(item.get("b") or 0)
            slot = day_src.setdefault(src, {"n": 0, "b": 0})
            slot["n"] += 1
            slot["b"] += max(0, nbytes)
            agg = totals.setdefault(src, {"n": 0, "b": 0})
            agg["n"] += 1
            agg["b"] += max(0, nbytes)
            tid = str(item.get("tid") or "")
            if tid:
                tids.add(tid)
            if src == "preview":
                rng_total += 1
                if item.get("rng"):
                    rng_hit += 1
        by_day[day] = day_src
        templates[day] = tids
        ranges[day] = {"total": rng_total, "range": rng_hit}
    return {"first": wanted[0], "last": wanted[-1], "days": len(wanted),
            "files": wanted, "by_day": by_day, "totals": totals,
            "templates": templates, "ranges": ranges}


class CountingReader:
    """包一层文件对象，数出真正被转发出去的字节。

    ``shutil.copyfileobj(fsrc, fdst, length)`` 只调用 ``fsrc.read(length)``，
    所以这个壳对它是透明的：既不改传输逻辑，也能拿到准确的字节数
    （包括 206 分片、连接中断时已经送出去的那半截）。
    """

    __slots__ = ("_fp", "count")

    def __init__(self, fp):
        self._fp = fp
        self.count = 0

    def read(self, size: int = -1):
        chunk = self._fp.read(size)
        if chunk:
            self.count += len(chunk)
        return chunk

    def __getattr__(self, name):
        return getattr(self._fp, name)
