#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心引擎冒烟测试：不联网、不花钱，用本地假服务跑 3 条。"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import BatchRunner, BatchSpec, load_manifest  # noqa: E402
from app.storage import StorageConfig, open_storage  # noqa: E402

MOCK = ROOT / "tests" / "mock_dashscope.py"


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


def main() -> int:
    port = free_port()
    mock = subprocess.Popen([sys.executable, str(MOCK), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_port(port):
        print("假服务没起来")
        return 1

    tmp = ROOT / ".tmp" / f"core-{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        image = tmp / "outfit.png"
        Image.new("RGB", (720, 900), (230, 228, 224)).save(image)

        manifest = tmp / "manifest.json"
        manifest.write_text(json.dumps([
            {"id": "001", "url": f"http://127.0.0.1:{port}/tpl/001.mp4", "duration": 7},
            {"id": "002", "url": f"http://127.0.0.1:{port}/tpl/002.mp4", "duration": 7},
            {"id": "003", "url": f"http://127.0.0.1:{port}/tpl/003.mp4", "duration": 7},
        ], ensure_ascii=False), encoding="utf-8")

        templates = load_manifest(str(manifest))
        assert len(templates) == 3, templates

        events = []
        logs = []
        spec = BatchSpec(
            batch_id="TEST01",
            image_path=str(image),
            prompt="视频1中的人物，保留面部与动作，把服装换成图1这套",
            out_dir=tmp / "out" / "TEST01",
            templates=templates,
            api_key="demo-key-0123456789abcdef",
            base=f"http://127.0.0.1:{port}/api/v1",
            provider="dashscope",
            model="wan3.0-video",
            mode="edit",
            duration="-1",
            concurrency=3,
            poll_interval=0.2,
            poll_timeout=30,
        )
        runner = BatchRunner(spec, on_event=events.append, log=logs.append)
        summary = runner.run()

        print("summary:", summary)
        assert summary["total"] == 3, summary
        assert summary["success"] == 3, summary
        for tpl in templates:
            assert (spec.out_dir / f"{tpl['id']}.mp4").exists(), tpl
        assert (spec.out_dir / "report.csv").exists()
        state = json.loads((spec.out_dir / "batch.json").read_text(encoding="utf-8"))
        assert len(state["records"]) == 3, state
        assert any(e["type"] == "progress" for e in events)
        print("csv 行数:", len((spec.out_dir / "report.csv").read_text(encoding="utf-8-sig").strip().splitlines()))

        # ---- 第二遍：开启归档，验证成品真进了"对象存储" ----
        store = open_storage(StorageConfig(provider="local", root=str(tmp / "cos")))
        assert store is not None
        spec2 = BatchSpec(
            batch_id="TEST02",
            image_path=str(image),
            prompt="视频1中的人物，保留面部与动作，把服装换成图1这套",
            out_dir=tmp / "out" / "TEST02",
            templates=templates,
            api_key="demo-key-0123456789abcdef",
            base=f"http://127.0.0.1:{port}/api/v1",
            provider="dashscope",
            model="wan3.0-video",
            mode="edit",
            duration="-1",
            concurrency=2,
            poll_interval=0.2,
            poll_timeout=30,
            storage=store,
        )
        summary2 = BatchRunner(spec2, on_event=events.append, log=logs.append).run()
        print("summary2:", summary2)
        assert summary2["success"] == 3, summary2
        assert summary2["archived"] == 3, summary2
        assert summary2["archive_failed"] == 0, summary2
        state2 = json.loads((spec2.out_dir / "batch.json").read_text(encoding="utf-8"))
        for rec in state2["records"]:
            assert rec.get("cos_key"), rec
            assert store.exists(rec["cos_key"]), rec["cos_key"]
        keys = sorted(store.list_prefix("output"))
        assert len(keys) == 3, keys
        assert all(k.endswith(".mp4") for k in keys), keys
        print("归档 Key:", keys[0])

        print("OK 核心引擎通过")
        return 0
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
