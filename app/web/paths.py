#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行数据里的绝对路径重锚定（两区搬家兼容层）。

任务 / 批次 / 上传记录里保存的是产生那一刻的绝对路径，例如
``D:\\批量视频任务设计区\\web-data\\output\\<batch>\\<tid>.mp4``。
2026-11-06 把生产从设计区整体搬到运行区后，这些旧路径指向已经归档（或不存在）
的目录，历史任务的视频与图片下载会 404——文件其实一直都在，只是路径换了根。

读这些数据时统一用 :func:`relocate` 按当前 ``data_dir`` 重新锚定：
数据文件一个字都不用改，以后再换代码目录也不会再坏。

匹配规则刻意保守，避免误伤用户真的放在别处的外部文件：
1. 只处理绝对路径，且路径里确实有 ``web-data``（含 web-data-dev / 快照目录）段；
2. 已经落在当前 ``data_dir`` 下的，原样返回；
3. 重锚目标必须存在（读文件场景）；写新目录时可用 ``must_exist=False``。
"""
from __future__ import annotations

from pathlib import Path


def _tail_after_data_root(path: Path) -> Path | None:
    """取 ``web-data*`` 段之后的相对部分；找不到标记、或后面没有内容则返回 None。"""
    parts = path.parts
    for i, seg in enumerate(parts):
        low = seg.lower()
        if low == "web-data" or low.startswith("web-data-"):
            rest = parts[i + 1:]
            return Path(*rest) if rest else None
    return None


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def relocate(raw, data_dir: Path | str, must_exist: bool = True) -> Path:
    """把持久化的绝对路径 ``raw`` 重锚定到当前 ``data_dir``。

    ``must_exist=True``（默认，下载 / 读图）：只有重锚后的目标确实存在才替换，
    否则返回原路径，绝不把外部路径改到别处。
    ``must_exist=False``（接回旧任务、准备写新输出目录）：只要原路径不在当前
    数据根下就重锚，保证新视频不会再落到旧文件夹里。
    """
    text = str(raw or "")
    if not text:
        return Path()
    path = Path(text)
    if not path.is_absolute():
        return path                      # 相对路径保持旧语义（按进程工作目录解析）
    root = Path(data_dir)
    if _under(path, root):
        return path                      # 本来就是当前数据根，零改动
    tail = _tail_after_data_root(path)
    if tail is None or tail.is_absolute():
        return path                      # 不像本项目的数据路径，别动
    candidate = root / tail
    if must_exist and not candidate.exists():
        return path                      # 新根下没有这份数据，保持原样（dev 副本预期 404）
    return candidate
