#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自由视频 tab（通用 wan3.0）：文生视频 / 图生视频 / 参考视频编辑 / 权限 / 校验。

真实的 wan3.0 一条 7 秒视频 0.6–1.2 元，这里把 WanClient 换成桩、把上传与下载换成本地假实现，
其余全是真代码：任务记录、素材复制、body 组装、HTTP 路由、owner 过滤、取消与删除。
"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from _tmpdir import tempdir  # noqa: E402
from PIL import Image  # noqa: E402
from test_batches import (body, build_manifest, build_rows, build_settings,  # noqa: E402
                          call, start_server, write_workbook)
from app import media as media_mod  # noqa: E402
from app.videogen import manager as manager_mod  # noqa: E402
from app.wan_api import SubmitResult, TaskResult, WaitStopped  # noqa: E402
from app.web.server import WebApplication  # noqa: E402

MOCK_BASE = "http://127.0.0.1:1"        # 占位地址：本套件不会真的发请求
FAKE_VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"fake-mp4-bytes"


def png_bytes(width: int = 320, height: int = 240) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (203, 208, 214)).save(buf, format="PNG")
    return buf.getvalue()


def upload_png(opener, base, name="look.png") -> str:
    code, _, payload = call(opener, base, "/api/uploads", "POST", {
        "filename": name,
        "data": "data:image/png;base64," + base64.b64encode(png_bytes()).decode("ascii"),
    })
    assert code == 201, (code, payload)
    return body(payload)["upload_id"]


def upload_raw(opener, base, data: bytes, *, kind: str, name: str, ctype: str = "application/octet-stream"):
    request = urllib.request.Request(
        base + "/api/videogen/upload", data=data, method="POST",
        headers={"Content-Type": ctype, "X-Kind": kind,
                 "X-File-Name": urllib.parse.quote(name)})
    try:
        with opener.open(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def login(base: str, *, users) -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for name, password in users:
        code, _, raw = call(opener, base, "/api/login", "POST", {"username": name, "password": password})
        assert code == 200, (name, code, raw)
    return opener


def wait_done(opener, base: str, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _, raw = call(opener, base, f"/api/videogen/tasks/{task_id}")
        assert code == 200, (code, raw)
        task = body(raw)["task"]
        if not task["active"]:
            return task
        time.sleep(0.1)
    raise AssertionError("视频任务 15 秒内没跑完（桩不联网，正常应 <1 秒）")


class Recorder:
    """桩客户端收到的调用顺序与参数。"""

    def __init__(self):
        self.builds: list[dict] = []
        self.bodies: list[dict] = []
        self.submits = 0
        self.gate: threading.Event | None = None


class FakeClient:
    def __init__(self, recorder: Recorder):
        self.rec = recorder

    def build_body(self, task, defaults=None, *, default_mode=None, probe=None, video_durations=None):
        self.rec.builds.append({
            "task": json.loads(json.dumps(task)),
            "default_mode": default_mode,
            "durations": list(video_durations or []),
        })
        return json.loads(json.dumps(task)), [], []

    def wire_body(self, payload):
        self.rec.bodies.append(json.loads(json.dumps(payload)))
        return payload

    def submit(self, wire):
        self.rec.submits += 1
        return SubmitResult(task_id=f"wan-task-{self.rec.submits}", status="pending")

    def wait_task(self, task_id, *, timeout=None, interval=None, on_status=None, should_stop=None):
        deadline = time.time() + 10
        while time.time() < deadline:
            if should_stop and should_stop():
                raise WaitStopped(task_id)
            if self.rec.gate is not None and not self.rec.gate.is_set():
                time.sleep(0.05)
                continue
            return TaskResult(task_id=task_id, status="succeeded",
                              video_url="https://cdn.example/fake.mp4",
                              usage={"duration": 5, "SR": "720"})
        raise AssertionError("桩 wait_task 卡住了")


class FakeUploader:
    """把本地路径「上传」成确定的公网 URL，不联网。"""

    def upload(self, path, name=None):
        return "https://cos.example/" + Path(path).name


def main() -> int:
    tmp = tempdir("videogen-")
    workbook = tmp / "标签清单.xlsx"
    write_workbook(workbook, build_rows())
    settings = build_settings(tmp, build_manifest(tmp, MOCK_BASE), workbook, MOCK_BASE)
    assert not settings.templates_error, settings.templates_error

    app = WebApplication(settings)
    rec = Recorder()
    app.videogen._client_factory = lambda: FakeClient(rec)
    app.videogen._uploader = lambda: FakeUploader()
    downloads: list[tuple] = []

    def fake_download(url, dest):
        downloads.append((url, Path(dest).name))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(FAKE_VIDEO)

    app.videogen._download = fake_download

    # 媒体验证依赖 ffprobe，测试里不跑真实探测：只验「代码路径与参数」，不验编码。
    def fake_validate_video(path):
        return {"path": str(path), "size_mb": 0.1, "format": "mp4", "duration": 4.0,
                "width": 720, "height": 1280, "fps": 24.0}

    def fake_validate_audio(path):
        return {"path": str(path), "size_mb": 0.05}

    media_mod.validate_video = fake_validate_video
    media_mod.validate_audio = fake_validate_audio

    server, base = start_server(app)
    data_dir = Path(settings.data_dir)
    try:
        alice = login(base, users=[("alice", "alice-pass-123")])
        bob = login(base, users=[("bob", "bob-pass-12345")])
        admin = login(base, users=[("admin", "admin-pass-123")])

        # 1. 选项由后端下发：模式/分辨率/比例/时长/上限都不在前端复制规则
        code, _, raw = call(alice, base, "/api/bootstrap")
        assert code == 200
        options = body(raw)["service"]["videogen"]
        assert options["configured"] is True, options
        assert [item["value"] for item in options["modes"]] == ["auto", "reference", "edit"], options
        assert len(options["resolutions"]) == 3 and len(options["ratios"]) == 7, options
        assert -1 in [item["value"] for item in options["durations"]], options
        assert options["limits"]["max_count"] == 4, options
        assert options["defaults"]["mode"] == "auto", options
        print("  ✓ bootstrap 下发模式/分辨率/比例/时长与上限")

        # 2. 文生视频：没有参考素材 → auto 落到 reference，2 条约等于 2 次提交
        code, _, raw = call(alice, base, "/api/videogen/tasks", "POST", {
            "prompt": "夜晚的霓虹街道，雨后地面积水反光，镜头缓慢推进",
            "mode": "auto", "resolution": "720P", "ratio": "9:16",
            "duration": 5, "count": 2,
        })
        assert code == 201, (code, raw)
        task = body(raw)["task"]
        task_id = task["id"]
        assert task["mode"] == "reference" and task["mode_choice"] == "auto", task
        assert task["count"] == 2 and len(task["shots"]) == 2 and task["active"] is True, task

        task = wait_done(alice, base, task_id)
        assert task["status"] == "已完成" and task["progress"] == "2/2", task
        build = rec.builds[0]
        assert build["default_mode"] == "reference", build
        sent = build["task"]
        assert sent["prompt"].startswith("夜晚的霓虹街道"), sent
        assert "imageUrls" not in sent and "videoUrls" not in sent, sent
        assert sent["resolution"] == "720P" and sent["aspectRatio"] == "9:16", sent
        assert sent["duration"] == 5 and sent["promptExtend"] is True and sent["watermark"] is False, sent
        assert rec.submits == 2, rec.submits
        task_dir = data_dir / "videogen" / "files" / task_id
        assert (task_dir / "shot-1.mp4").read_bytes() == FAKE_VIDEO, task_dir
        assert (task_dir / "shot-2.mp4").is_file(), task_dir
        assert downloads[0][1] == "shot-1.mp4", downloads
        print("  ✓ 文生视频：auto → reference，逐条提交、落盘、计费秒数")

        # 3. 图生视频：参考图复用 /api/uploads，提交前复制进任务目录
        upload_id = upload_png(alice, base)
        code, _, raw = call(alice, base, "/api/videogen/tasks", "POST", {
            "prompt": "让图1里的人物走向镜头，微笑", "mode": "reference",
            "resolution": "1080P", "ratio": "16:9", "duration": 7, "count": 1,
            "upload_ids": [upload_id],
        })
        assert code == 201, (code, raw)
        img_task = body(raw)["task"]
        img_task = wait_done(alice, base, img_task["id"])
        assert img_task["status"] == "已完成", img_task
        assert img_task["refs"]["images"][0]["name"] == "look.png", img_task["refs"]
        sent = rec.bodies[-1]
        assert sent["imageUrls"] == ["https://cos.example/image1.png"], sent
        assert (data_dir / "videogen" / "files" / img_task["id"] / "image1.png").is_file()
        print("  ✓ 图生视频：参考图上传 → 任务内复制 → 解析成 URL")

        # 4. 参考视频编辑：原始字节流上传，auto + 视频 → edit，可 adaptive / 时长自动
        code, payload = upload_raw(alice, base, FAKE_VIDEO, kind="video", name="clip.mp4")
        assert code == 201, (code, payload)
        clip = payload["upload"]
        assert clip["kind"] == "video" and clip["duration"] == 4.0 and clip["name"] == "clip.mp4", clip
        code, payload = upload_raw(alice, base, b"RIFF....WAVE", kind="audio", name="voice.mp3")
        assert code == 201, (code, payload)
        voice = payload["upload"]

        code, _, raw = call(alice, base, "/api/videogen/tasks", "POST", {
            "prompt": "把视频1里的衣服换成图1的红色针织衫", "mode": "auto",
            "resolution": "1080P", "ratio": "adaptive", "duration": -1, "count": 1,
            "upload_ids": [upload_id], "video_ids": [clip["upload_id"]],
            "audio_ids": [voice["upload_id"]], "audio": True,
        })
        assert code == 201, (code, raw)
        edit_task = body(raw)["task"]
        assert edit_task["mode"] == "edit" and edit_task["duration"] == -1, edit_task
        edit_task = wait_done(alice, base, edit_task["id"])
        send = rec.builds[-1]
        assert send["default_mode"] == "edit", send
        assert send["durations"] == [4.0], send
        assert send["task"]["duration"] == -1 and send["task"]["aspectRatio"] == "adaptive", send
        assert send["task"]["imageUrls"][0].endswith("image1.png"), send
        assert send["task"]["videoUrls"][0].endswith("video1.mp4"), send
        assert send["task"]["audioUrls"][0].endswith("audio1.mp3"), send
        assert send["task"]["audio"] is True, send
        wire = rec.bodies[-1]
        assert wire["imageUrls"] == ["https://cos.example/image1.png"], wire
        assert wire["videoUrls"] == ["https://cos.example/video1.mp4"], wire
        assert wire["audioUrls"] == ["https://cos.example/audio1.mp3"], wire
        assert edit_task["status"] == "已完成", edit_task
        print("  ✓ 参考视频编辑：视频/音频原始上传 → edit + adaptive + 时长自动")

        # 5. 取片：inline 给 <video>，download=1 是附件；参考素材也能回看
        shot_url = edit_task["shots"][0]["url"]
        code, headers, raw = call(alice, base, shot_url)
        assert code == 200 and headers.get("Content-Type") == "video/mp4", (code, headers)
        assert raw == FAKE_VIDEO, raw[:20]
        code, headers, _ = call(alice, base, edit_task["shots"][0]["download_url"])
        assert code == 200 and "attachment" in (headers.get("Content-Disposition") or ""), headers
        assert ".mp4" in urllib.parse.unquote(headers.get("Content-Disposition") or "")
        code, headers, raw = call(alice, base, edit_task["refs"]["videos"][0]["url"])
        assert code == 200 and raw == FAKE_VIDEO, (code, raw[:20])
        print("  ✓ inline / 附件下载 / 参考素材回看")

        # 6. 参数校验与状态码
        bad_cases = [
            ({"prompt": ""}, 400),
            ({"prompt": "x", "mode": "edit"}, 400),                       # edit 必须带参考视频
            ({"prompt": "x", "mode": "reference", "ratio": "adaptive"}, 400),
            ({"prompt": "x", "duration": 1}, 400),                        # 只允许 -1 或 2–30
            ({"prompt": "x", "duration": 60}, 400),
            ({"prompt": "x", "count": 9}, 400),                           # max_count=4
            ({"prompt": "x" * 20001}, 400),
            ({"prompt": "x", "video_ids": ["f" * 32]}, 404),
            ({"prompt": "x", "audio_ids": ["0" * 32]}, 404),
        ]
        for payload, expect in bad_cases:
            code, _, raw = call(alice, base, "/api/videogen/tasks", "POST", payload)
            assert code == expect, (payload, code, raw)
        status, rejected = upload_raw(alice, base, b"x", kind="video", name="clip.avi")
        assert status == 400 and rejected.get("error"), (status, rejected)
        status, rejected = upload_raw(alice, base, b"x", kind="image", name="a.png")
        assert status == 400 and rejected.get("error"), (status, rejected)
        print("  ✓ 400 参数 / 404 找不到 / 条数超上限 / 上传类型校验")

        # 7. owner 隔离：bob 列表为空、详情/视频/删除全 404；admin 能看到
        code, _, raw = call(bob, base, "/api/videogen/tasks")
        assert code == 200 and body(raw)["items"] == [], body(raw)
        code, _, _ = call(bob, base, f"/api/videogen/tasks/{task_id}")
        assert code == 404, code
        code, _, _ = call(bob, base, shot_url)
        assert code == 404, code
        code, _, _ = call(bob, base, f"/api/videogen/tasks/{task_id}", "DELETE")
        assert code == 404, code
        code, _, raw = call(admin, base, "/api/videogen/tasks")
        assert code == 200 and task_id in [item["id"] for item in body(raw)["items"]], body(raw)
        print("  ✓ owner 隔离（他人 404）+ admin 全量")

        # 8. 未登录 401
        anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        for method, path in (("GET", "/api/videogen/tasks"), ("POST", "/api/videogen/tasks")):
            code, _, _ = call(anon, base, path, method, {"prompt": "x"})
            assert code == 401, (method, path, code)
        print("  ✓ 未登录 401")

        # 9. 取消：生成中请求取消 → 分镜与任务都落「已取消」，不会变成失败
        rec.gate = threading.Event()
        code, _, raw = call(alice, base, "/api/videogen/tasks", "POST", {
            "prompt": "缓慢推进的航拍镜头", "mode": "reference", "count": 1,
        })
        assert code == 201, (code, raw)
        cancel_id = body(raw)["task"]["id"]
        code, _, raw = call(alice, base, f"/api/videogen/tasks/{cancel_id}/cancel", "POST")
        assert code == 200, (code, raw)
        canceled = wait_done(alice, base, cancel_id, timeout=20)
        assert canceled["status"] == "已取消", canceled
        assert canceled["shots"][0]["status"] == "已取消", canceled["shots"]
        rec.gate = None
        print("  ✓ 取消：进行中任务 → 已取消")

        # 10. 删除：记录与本地成品一起走
        code, _, _ = call(alice, base, f"/api/videogen/tasks/{task_id}", "DELETE")
        assert code == 200, code
        assert not (data_dir / "videogen" / "files" / task_id).exists()
        assert not (data_dir / "videogen" / "tasks" / f"{task_id}.json").exists()
        code, _, _ = call(alice, base, task["shots"][0]["url"])
        assert code == 404, code
        print("  ✓ 删除记录同时删本地成品")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"  ✗ 断言失败：{exc}")
        raise
