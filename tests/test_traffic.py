#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""COS 流量账本（app/traffic.py）的离线测试。

只测机制，不测 HTTP 接线：
  · 账本按天分文件、字段白名单、坏行跳过；
  · 关掉时一行都不写；
  · CountingReader 数出来的字节 == 实际转发字节（206 分片也要准）；
  · collect() 的窗口、按来源汇总、模板去重、Range 计数。

临时目录一律用 tests/_tmpdir.tempdir：`tempfile.mkdtemp` 在 Windows 下以 0o700
建目录、剥掉继承 ACE，沙箱里随后写文件会 PermissionError。
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from _tmpdir import tempdir  # noqa: E402
from app import traffic  # noqa: E402


def today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def test_record_and_collect() -> None:
    base = tempdir("traffic-")
    traffic.configure(base)
    try:
        assert traffic.is_enabled()

        traffic.record("preview", 3145728, tid="T1", st=206, rng=1, m="GET")
        traffic.record("preview", 1024, tid="T1", st=200, rng=0)
        traffic.record("download", 2048, tid="T2")
        traffic.record("template", 12 * 1024 ** 2, tid="T3", job="WEB-x")
        traffic.record("image", 0, n=1)          # base64 内联：次数有、字节 0
        traffic.record("archive", 0, n=3, bogus="必须被白名单丢掉")

        path = base / f"{today()}.jsonl"
        assert path.is_file(), "账本文件没落盘"
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(lines) == 6, lines
        assert lines[0]["src"] == "preview" and lines[0]["b"] == 3145728
        assert lines[0]["rng"] == 1 and lines[0]["tid"] == "T1"
        assert "bogus" not in lines[-1], "白名单之外的字段被写进了账本"

        stats = traffic.collect(days=7, directory=base)
        assert stats["first"] == today() and stats["days"] == 1
        preview = stats["totals"]["preview"]
        assert preview["n"] == 2 and preview["b"] == 3145728 + 1024, preview
        assert stats["totals"]["image"] == {"n": 1, "b": 0}
        assert stats["templates"][today()] == {"T1", "T2", "T3"}, "模板去重不对"
        assert stats["ranges"][today()] == {"total": 2, "range": 1}
        print("  ✓ 记账 / 白名单 / 汇总 / 模板去重 / Range 计数")
    finally:
        traffic.configure(None)
        shutil.rmtree(base, ignore_errors=True)


def test_disabled_writes_nothing() -> None:
    base = tempdir("traffic-off-")
    try:
        traffic.configure(base, enabled=False)
        assert not traffic.is_enabled()
        traffic.record("preview", 999)
        assert not list(base.glob("*.jsonl")), "关掉后仍然写了账本"

        traffic.configure(base, enabled=True)
        traffic.record("preview", 1)
        assert (base / f"{today()}.jsonl").is_file(), "重新打开后应恢复写入"
        print("  ✓ 关闭时不写、重新打开后恢复")
    finally:
        traffic.configure(None)
        shutil.rmtree(base, ignore_errors=True)


def test_broken_lines_are_skipped() -> None:
    base = tempdir("traffic-bad-")
    try:
        (base / f"{today()}.jsonl").write_text(
            '{"t":1,"src":"preview","b":10}\n'
            "这不是 JSON\n"
            "\n"
            '{"t":2,"src":"preview","b":20}\n',
            encoding="utf-8")
        stats = traffic.collect(days=1, directory=base)
        assert stats["totals"]["preview"] == {"n": 2, "b": 30}, stats["totals"]
        print("  ✓ 坏行/空行跳过，好行照算")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_counting_reader_matches_bytes() -> None:
    """模拟 _preview 的转发路径：CountingReader 数的必须就是发出去的字节。"""
    body = b"x" * (300 * 1024 + 7)
    src = traffic.CountingReader(io.BytesIO(body))
    dst = io.BytesIO()
    shutil.copyfileobj(src, dst, length=64 * 1024)
    assert src.count == len(body) == len(dst.getvalue()), (src.count, len(body))
    print(f"  ✓ CountingReader 计数准确（{src.count} 字节，分块 64KB）")


def test_collect_window() -> None:
    base = tempdir("traffic-win-")
    try:
        for day, nbytes in (("2026-11-01", 100), ("2026-11-02", 200), ("2026-11-03", 300)):
            (base / f"{day}.jsonl").write_text(
                json.dumps({"t": 1, "src": "preview", "b": nbytes}) + "\n", encoding="utf-8")
        stats = traffic.collect(days=2, directory=base)
        assert stats["files"] == ["2026-11-02", "2026-11-03"], stats["files"]
        assert stats["totals"]["preview"]["b"] == 500
        empty = traffic.collect(days=7, directory=base / "nope")
        assert empty["days"] == 0 and empty["totals"] == {}
        print("  ✓ 只取最近 N 天；目录不存在时返回空而不是报错")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    print("test_traffic：COS 流量账本")
    test_record_and_collect()
    test_disabled_writes_nothing()
    test_broken_lines_are_skipped()
    test_counting_reader_matches_bytes()
    test_collect_window()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
