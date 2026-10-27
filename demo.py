#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打通生产链路（离线版，不联网、不需要 API Key、不花钱）。

    python3 demo.py              # 默认跑 5 条，验证整条流水线
    python3 demo.py --count 20   # 想看更大批量
    python3 demo.py --real       # 换成真接口（需要 DASHSCOPE_API_KEY，会真实扣费，只跑 1 条）

它做的事：造一张占位定妆图 → 起本地假服务 → 走完 上传/提交/轮询/下载/记账 → 出结果表 → 收工。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO_DIR = ROOT / "out" / "demo"
PLACEHOLDER = ROOT / "assets" / "_placeholder.png"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def make_placeholder() -> Path:
    """生成一张 1080×1350 的占位定妆图，仅用于打通链路。"""
    from PIL import Image, ImageDraw

    PLACEHOLDER.parent.mkdir(parents=True, exist_ok=True)
    if PLACEHOLDER.exists():
        return PLACEHOLDER

    width, height = 1080, 1350
    img = Image.new("RGB", (width, height), (238, 236, 232))
    draw = ImageDraw.Draw(img)
    # 人形色块，方便肉眼确认「图片确实到了服务端」
    draw.ellipse((410, 190, 670, 450), fill=(206, 186, 170))          # 头
    draw.polygon([(360, 700), (720, 700), (790, 1330), (290, 1330)],  # 身体
                 fill=(176, 190, 205))
    draw.rectangle((0, height - 90, width, height), fill=(60, 64, 70))
    draw.text((40, height - 64), "PLACEHOLDER / 定妆图占位 - 正式生产请替换", fill=(255, 255, 255))
    draw.rectangle((2, 2, width - 3, height - 3), outline=(120, 124, 130), width=4)
    img.save(PLACEHOLDER)
    return PLACEHOLDER


def build_demo_tasks(count: int) -> Path:
    src = ROOT / "batch" / "tasks.batch.json"
    if not src.exists():
        print("缺少 batch/tasks.batch.json，先执行：")
        print("  python3 make_batch.py --seed-outfits 100 --out batch/outfits.tsv")
        print("  python3 make_batch.py --spec batch/spec.example.json --out batch/tasks.batch.json")
        raise SystemExit(1)

    data = json.loads(src.read_text(encoding="utf-8"))
    tasks = []
    for task in data["tasks"][:count]:
        task = {k: v for k, v in task.items() if k != "output"}
        task["imageUrls"] = [str(PLACEHOLDER.relative_to(ROOT)).replace("\\", "/")]
        task["output"] = f"out/demo/{task['id']}.mp4"
        tasks.append(task)
    out = DEMO_DIR / "tasks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"defaults": data["defaults"], "tasks": tasks}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def run_client(tasks_file: Path, count: int, base_url: str, real: bool = False) -> int:
    env = {
        "WAN_PROVIDER": "dashscope",
        "WAN_BASE_URL": base_url,
        "WAN_API_KEY": "demo-key-0123456789abcdef",
        "PYTHONIOENCODING": "utf-8",
    }
    if real:
        env["WAN_API_KEY"] = os.environ.get("DASHSCOPE_API_KEY", "")
    cmd = [sys.executable, str(ROOT / "wan3_r2v.py"), "run",
           "--tasks", str(tasks_file),
           "--concurrency", "2",
           "--interval", "0.3" if not real else "15",
           "--timeout", "60" if not real else "1800",
           "--fresh",
           "--state-file", str(DEMO_DIR / "state.json"),
           "--limit", str(count)]
    print("$ " + " ".join(cmd[1:]) + "\n")
    return subprocess.run(cmd, cwd=str(ROOT), env={**os.environ, **env}).returncode


def report(count: int) -> None:
    state_file = DEMO_DIR / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    print("\n" + "=" * 78)
    print(f"{'任务':<8}{'状态':<10}{'文件大小':>10}   产物")
    print("-" * 78)
    ok = 0
    for tid, rec in list(state.items())[:count]:
        path = Path(rec.get("file", ""))
        size = f"{path.stat().st_size / 1024:.0f} KB" if path.exists() else "-"
        if rec.get("status") == "success" and path.exists():
            ok += 1
        print(f"{tid:<8}{rec.get('status', '-'):<10}{size:>10}   {path.name if path.name else '-'}")
    print("-" * 78)
    billable = sum(float(r.get("billable_seconds") or 0) for r in state.values())
    estimated = sum(float(r.get("estimated_cost") or 0) for r in state.values()
                    if r.get("estimated_cost") is not None)
    has_estimate = any(r.get("estimated_cost") is not None for r in state.values())
    tail = f" · 预估 ¥{estimated:.2f}" if has_estimate else " · 未配置单价，只记秒数"
    print(f"成功 {ok} 条 | 计费 {billable:g}s{tail}（假服务/按官方 usage 计）")
    print(f"产物目录：{DEMO_DIR}")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="一键打通 wan3 生产链路")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--real", action="store_true", help="走真实接口（需 Key，会扣费，强制只跑 1 条）")
    args = parser.parse_args()

    try:  # 让父进程与子进程输出按真实顺序交错
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("① 准备占位定妆图")
    image = make_placeholder()
    print(f"   {image.relative_to(ROOT)}  ({image.stat().st_size / 1024:.0f} KB)")

    print("\n② 生成演示任务（前 %d 条）" % args.count)
    tasks_file = build_demo_tasks(1 if args.real else args.count)
    print(f"   {tasks_file.relative_to(ROOT)}")

    server = None
    if args.real and not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        print("\n③ 缺少 DASHSCOPE_API_KEY，真实模式无法继续。")
        print("   set DASHSCOPE_API_KEY=sk-你的Key")
        print("   或先去掉 --real 跑离线演练： python3 demo.py")
        return 1

    try:
        if args.real:
            print("\n③ 真实模式：确认已设置 DASHSCOPE_API_KEY，将真实提交 1 条（会扣费）")
            base = (os.environ.get("WAN_BASE_URL") or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
        else:
            port = free_port()
            print(f"\n③ 启动本地假服务 :{port}")
            server = subprocess.Popen(
                [sys.executable, str(ROOT / "tests" / "mock_dashscope.py"), str(port)],
                cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not wait_port(port):
                print("   假服务启动失败")
                return 1
            base = f"http://127.0.0.1:{port}/api/v1"
            print("   就绪")

        print("\n④ 跑生产流程：准备服装图 → 提交 → 轮询 → 下载 → 记账")
        code = run_client(tasks_file, 1 if args.real else args.count, base, real=args.real)
    finally:
        if server:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()

    report(1 if args.real else args.count)

    if args.real:
        if code == 0:
            print("\n真实链路已跑通（1 条）。去掉 --real 可离线重复演练。")
        else:
            print("\n真实调用未成功，原因见上面的错误信息（常见：Key 无效、余额不足、素材不可达）。")
    else:
        print("\n链路已打通（离线）。素材准备好后，去掉 WAN_BASE_URL 直接跑真实任务：")
        print("  set DASHSCOPE_API_KEY=sk-你的Key")
        print("  python3 wan3_r2v.py run --tasks batch/tasks.batch.json --limit 1")
    return code


if __name__ == "__main__":
    sys.exit(main())
