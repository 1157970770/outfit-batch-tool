#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键跑全部离线测试（不联网、不花钱）。

用法：
    python tests/run_all.py            # 跑全部
    python tests/run_all.py test_web   # 只跑指定套件

约定：
  · 每个套件是独立脚本，退出码 0 即通过；
  · 桌面 exe 版已移出本仓库，PySide6 相关套件不再包含在内；
  · 任一必需套件失败，整体退出码为 1。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# 顺序有讲究：先底层（协议/存储/引擎），再网页，最后批次端到端。
REQUIRED = [
    "test_wan_api",
    "test_storage",
    "test_core",
    "test_template_catalog",
    "test_template_store",
    "test_tag_truth",
    "test_cos_rest",
    "test_tag_backup",
    "test_style_usage",
    "test_web_auth",
    "test_jobs_state",
    "test_resume",
    "test_billing",
    "test_web_hardening",
    "test_batch5",
    "test_web_tags",
    "test_web",
    "test_web_tag_api",
    "test_web_multi_images",
    "test_batches",
    "test_history_admin",
]
OPTIONAL: list[str] = []
PER_SUITE_TIMEOUT = 900                  # 单套 15 分钟


def _has_pyside() -> bool:
    return importlib.util.find_spec("PySide6") is not None


def run_one(name: str) -> tuple[str, float, str]:
    path = TESTS / f"{name}.py"
    if not path.is_file():
        return "MISSING", 0.0, f"找不到 {path}"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT), env=env, timeout=PER_SUITE_TIMEOUT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.time() - started, f"超过 {PER_SUITE_TIMEOUT} 秒"
    output = proc.stdout.decode("utf-8", errors="replace")
    status = "PASS" if proc.returncode == 0 else "FAIL"
    return status, time.time() - started, output


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if not a.startswith("-")] or REQUIRED
    pyside = _has_pyside()
    results: list[tuple[str, str, float]] = []
    failures: list[tuple[str, str]] = []

    print(f"仓库：{ROOT}")
    print(f"解释器：{sys.executable}")
    print(f"PySide6：{'已安装' if pyside else '未安装（桌面线套件将跳过）'}\n")

    for name in wanted:
        print(f"[..] {name}", flush=True)
        status, cost, output = run_one(name)
        tail = [line for line in output.splitlines() if line.strip()][-1:] or [""]
        print(f"[{status}] {name}  {cost:5.1f}s  {tail[0][:120]}")
        results.append((name, status, cost))
        if status not in ("PASS",):
            failures.append((name, output))

    if not wanted or wanted == REQUIRED:
        for name in OPTIONAL:
            if not (TESTS / f"{name}.py").is_file():
                continue
            if name in wanted:
                continue
            if not pyside:
                print(f"[SKIP] {name}  需要 PySide6（桌面线，已废弃）")
                results.append((name, "SKIP", 0.0))
                continue
            print(f"[..] {name}", flush=True)
            status, cost, output = run_one(name)
            print(f"[{status}] {name}  {cost:5.1f}s")
            results.append((name, status, cost))
            if status != "PASS":
                failures.append((name, output))

    passed = sum(1 for _, s, _ in results if s == "PASS")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    total = time.time()
    print("\n" + "=" * 56)
    print(f"通过 {passed} / {len(results)}，跳过 {skipped}，失败 {len(failures)}")

    if failures:
        for name, output in failures:
            print(f"\n----- {name} 末尾 40 行 -----")
            for line in output.splitlines()[-40:]:
                print(line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
