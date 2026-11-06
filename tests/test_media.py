#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频预检（app/media.py）回归测试：离线跑，不依赖本机装没装 ffprobe。

背景（2026-11-06 线上事故）：
    「打标器 → 上传视频」四条全部 400，响应体是
    "视频 xxx 内容不是有效的 mp4/mov 容器，请检查文件"。
    根因是 probe_video 用 `-show_entries format=duration` 只取时长，
    从未把容器名写回 info["format"]；而 validate_video 拿
    `not info.get("warn") and not info.get("format")` 当"ffprobe 能解析但没容器"
    的判据。于是**只要本机装了 ffprobe**（生产机 D:\\ffmpeg\\... 在 PATH 上），
    任何正常视频都会被判成无效容器；没装 ffprobe 的机器反而能上传。

本套件用假 ffprobe 输出覆盖三种环境：
  1. ffprobe 正常 → 必须拿到 format 并放行有效视频（回归点）；
  2. 没有 ffprobe → 只给 warn，校验放行（历史约定，不误伤没装 ffmpeg 的机器）；
  3. ffprobe 报错 → 同样只 warn 放行。
另测官方硬约束（时长/尺寸/帧率/宽高比/扩展名）的拒绝路径。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from app import media  # noqa: E402
from app.media import MediaError, probe_video, validate_video  # noqa: E402

CONTAINER = "mov,mp4,m4a,3gp,3g2,mj2"

PROBE_JSON = {
    "streams": [{"width": 480, "height": 854, "r_frame_rate": "30/1"}],
    "format": {"duration": "5.000000", "format_name": CONTAINER},
}


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_ffprobe(payload: dict | None = None, returncode: int = 0, stderr: str = ""):
    """返回可直接传给 patch 的「假 ffprobe」：which 找得到，run 返回固定 JSON。"""
    body = json.dumps(payload if payload is not None else PROBE_JSON)

    def _run(*_args, **_kwargs):
        return _Result(returncode, body if returncode == 0 else "", stderr)

    return _run


def sample(tmp: Path, name: str = "sample.mp4", size: int = 1024) -> Path:
    path = tmp / name
    path.write_bytes(b"\0" * size)
    return path


def main() -> int:
    tmp = tempdir("media-")
    video = sample(tmp)

    # 1. ffprobe 正常：format 必须回填，且有效视频放行（事故点）
    with patch.object(media.shutil, "which", return_value="ffprobe"), \
         patch.object(media.subprocess, "run", fake_ffprobe()):
        info = probe_video(video)
        assert info.get("format") == CONTAINER, info          # 回归：以前这里恒为缺失
        assert info.get("duration") == 5.0, info
        assert (info.get("width"), info.get("height")) == (480, 854), info
        assert info.get("fps") == 30.0, info
        assert "warn" not in info, info
        checked = validate_video(video)                        # 以前必抛 MediaError
        assert checked["format"] == CONTAINER, checked

    # 2. 没有 ffprobe：只 warn 放行
    with patch.object(media.shutil, "which", return_value=None):
        info = probe_video(video)
        assert info.get("warn") and "format" not in info, info
        assert validate_video(video)["size_mb"] > 0

    # 3. ffprobe 报错（例如扩展名被改过的假视频）：warn 放行，不误伤
    with patch.object(media.shutil, "which", return_value="ffprobe"), \
         patch.object(media.subprocess, "run",
                      fake_ffprobe(returncode=1, stderr="Invalid data found")):
        info = probe_video(video)
        assert info.get("warn"), info
        assert validate_video(video)["size_mb"] > 0

    # 4. 官方硬约束：拒绝时错误里要带原因，不能是"容器无效"
    cases = [
        ({"duration": "30.0"}, "时长"),
        ({"fps": 10.0}, "帧率"),
        ({"width": 5000, "height": 800}, "尺寸"),
        ({"width": 4000, "height": 400}, "宽高比"),
    ]
    for override, keyword in cases:
        payload = json.loads(json.dumps(PROBE_JSON))
        if "duration" in override:
            payload["format"]["duration"] = override["duration"]
        stream = payload["streams"][0]
        if "fps" in override:
            stream["r_frame_rate"] = "10/1"
        for key in ("width", "height"):
            if key in override:
                stream[key] = override[key]
        with patch.object(media.shutil, "which", return_value="ffprobe"), \
             patch.object(media.subprocess, "run", fake_ffprobe(payload)):
            try:
                validate_video(video)
            except MediaError as exc:
                assert keyword in str(exc), (keyword, str(exc))
                assert "容器" not in str(exc), str(exc)
            else:
                raise AssertionError(f"{keyword} 越界却放行了：{override}")

    # 5. 扩展名白名单
    try:
        validate_video(sample(tmp, "clip.avi"))
    except MediaError as exc:
        assert "扩展名" in str(exc), str(exc)
    else:
        raise AssertionError(".avi 应该被拒绝")

    print("视频预检：format 回填 / ffprobe 缺失与报错放行 / 硬约束拒绝 —— 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
