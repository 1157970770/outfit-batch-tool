#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布助手：把设计区已提交的代码发布到运行区（生产）。

对应 docs/dev-runtime-split.md 的「日常发布」：

  1. 检查运行区 git status 干净（运行区永不改码，脏了就是异常）
  2. 检查运行区没有 running/queued 任务（pull 会即时改静态文件，
     「新静态 + 旧后端」的混搭窗口要避开有人正在用的时候）
  3. git pull --ff-only（origin = 设计区本地仓库，只进不退）
  4. 运行区离线自检 run_web.py --selftest
  5. 提示下一步：右键运行区 启动器\\重启网页服务-管理员.bat（管理员）

不做的事：不重启服务（要管理员，由人执行 bat）；不 push GitHub（习惯不变）；
不碰设计区文件。发布后想反悔：git -C <运行区> reset --hard ORIG_HEAD 再重启一次。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_DESIGN = r"D:\批量视频任务设计区"
DEFAULT_RUNTIME = r"D:\批量视频任务运行区"
ACTIVE_STATES = ("queued", "running")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8")


def check_active_jobs(runtime: Path) -> list[str]:
    jobs_dir = runtime / "web-data" / "jobs"
    if not jobs_dir.is_dir():
        return []
    active = []
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                status = json.load(fh).get("status")
        except (OSError, ValueError):
            continue  # 坏文件交给服务端自己的口径，这里不拦截发布
        if status in ACTIVE_STATES:
            active.append(f"{path.stem} = {status}")
    return active


def main() -> int:
    parser = argparse.ArgumentParser(description="发布设计区已提交代码到运行区")
    parser.add_argument("--design", default=DEFAULT_DESIGN, help="设计区路径")
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME, help="运行区路径")
    args = parser.parse_args()
    design, runtime = Path(args.design), Path(args.runtime)

    if not (runtime / "run_web.py").is_file():
        print(f"[失败] 运行区不完整：缺 {runtime / 'run_web.py'}")
        return 1

    # 1. 运行区必须是干净的（只 pull 不改码的铁律）
    st = git(runtime, "status", "--porcelain")
    if st.returncode != 0:
        print(f"[失败] 运行区不是有效 git 仓库：{st.stderr.strip()}")
        return 1
    if st.stdout.strip():
        print("[中止] 运行区有本地改动（运行区永不改码）：")
        print(st.stdout.strip())
        print("先人工确认这些改动是否要保留；不要用 deploy 强行覆盖。")
        return 1

    # 2. 运行区不能有进行中任务
    active = check_active_jobs(runtime)
    if active:
        print("[中止] 运行区仍有进行中任务，等它们结束再发布：")
        print("\n".join(active))
        return 1

    # 3. 提醒：设计区未提交的改动不会发布（这是特性，不是遗漏）
    ds = git(design, "status", "--porcelain")
    if ds.returncode == 0 and ds.stdout.strip():
        print("[提醒] 设计区有未提交改动，本次不会发布（只发布已 commit 的状态）：")
        print("\n".join(ds.stdout.strip().splitlines()[:8]))
        if len(ds.stdout.strip().splitlines()) > 8:
            print("  ……（更多省略）")

    # 4. pull --ff-only
    before = git(runtime, "log", "-1", "--format=%h").stdout.strip()
    pull = git(runtime, "pull", "--ff-only")
    if pull.returncode != 0:
        print(f"[失败] git pull --ff-only 失败：{pull.stderr.strip()}")
        print("若运行区被分叉过，人工处理后重试；回退上次发布用：")
        print(f"  git -C \"{runtime}\" reset --hard ORIG_HEAD")
        return 1
    after = git(runtime, "log", "-1", "--format=%h %s").stdout.strip()
    if before in after:
        print(f"[OK] 运行区已是最新：{after}")
    else:
        print(f"[OK] 已发布：{after}")

    # 5. 运行区离线自检（用跑本脚本的同一个解释器）
    selftest = subprocess.run([sys.executable, "-B", str(runtime / "run_web.py"),
                               "--selftest"])
    if selftest.returncode != 0:
        print("[失败] 运行区自检未通过——先别重启服务，回到设计区排查。")
        print(f"回退本次发布：git -C \"{runtime}\" reset --hard ORIG_HEAD")
        return 1

    print()
    print("最后一步（需要管理员）：右键运行区")
    print(rf"  {runtime}\启动器\重启网页服务-管理员.bat  →  以管理员身份运行")
    print("重启会打断 running/queued 任务，重启前再看一眼管理页队列。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
