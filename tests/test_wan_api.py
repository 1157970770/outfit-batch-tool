#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方 Wan 3.0 适配层离线测试：字段映射、约束、错误归类、计费、素材上传。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tmpdir import tempdir  # noqa: E402

from app.wan_api import (  # noqa: E402
    PROVIDER_DASHSCOPE, PROVIDER_RUNNINGHUB, WanConfig, classify_error,
    estimate_cost, to_wire_body, validate_task,
)
from app.media import SourceUploader, resolve_image  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


class FakeStorage:
    class cfg:
        provider = "cos"

        @staticmethod
        def clean_prefix():
            return "prod"

    def __init__(self):
        self.objects = {}
        self.signed = []

    def exists(self, key):
        return key in self.objects

    def put_path(self, path, key, *, content_type="", meta=None):
        self.objects[key] = Path(path).read_bytes()
        return key

    def sign_url(self, key, expires=3600):
        self.signed.append((key, expires))
        return f"https://cdn.example.com/{key}?expires={expires}"


def main() -> int:
    print("官方请求体映射：")
    edit, errors, warnings = validate_task(
        {
            "id": "001",
            "mode": "edit",
            "prompt": "编辑视频1，把服装替换为图1这套，面部和动作保持不变",
            "videoUrls": ["https://cdn/tpl/001.mp4"],
            "imageUrls": ["https://cdn/outfit.png"],
            "resolution": "720P",
            "duration": "7",
            "seed": -1,
        },
        {},
        default_mode="edit",
        video_durations=[7.0],
    )
    check(not errors, f"edit 校验通过（warnings={warnings}）")
    wire = to_wire_body(edit, PROVIDER_DASHSCOPE, "wan3.0-video")
    check(wire["model"] == "wan3.0-video", "model 为 wan3.0-video")
    check([m["type"] for m in wire["input"]["media"]] == ["reference_video", "reference_image"],
          "edit 媒体顺序：视频在前、图片在后")
    check(isinstance(wire["parameters"]["duration"], int) and wire["parameters"]["duration"] == 7,
          "duration 转为整数")
    check(wire["parameters"]["ratio"] == "adaptive", "edit 默认 ratio=adaptive")
    check("aspectRatio" not in wire and "videoUrls" not in wire and "promptExtend" not in wire,
          "官方体不含旧字段")
    check(wire["parameters"]["prompt_extend"] is False, "prompt_extend 为 false")

    ref, errors, _ = validate_task(
        {
            "id": "002",
            "prompt": "图1中的女性，保持面部不变，只换服装",
            "imageUrls": ["https://cdn/outfit.png"],
            "videoUrls": ["https://cdn/tpl/001.mp4"],
        },
        {"resolution": "720P", "duration": "7", "aspectRatio": "9:16"},
        default_mode="reference",
        video_durations=[7.0],
    )
    check(not errors, "reference 校验通过")
    ref_wire = to_wire_body(ref, PROVIDER_DASHSCOPE, "wan3.0-video")
    check([m["type"] for m in ref_wire["input"]["media"]] == ["reference_image", "reference_video"],
          "reference 媒体顺序：图片在前")
    check(ref_wire["parameters"]["ratio"] == "9:16" and ref_wire["parameters"]["duration"] == 7,
          "reference 默认 9:16 / 7s")

    auto, errors, warnings = validate_task(
        {"prompt": "x", "duration": "auto"}, {}, default_mode="reference")
    check(not errors and auto["duration"] == -1, 'duration="auto" 映射为 -1')
    check(any("auto" in w for w in warnings), "auto 转换有 warning")

    _, errors, _ = validate_task(
        {"prompt": "x", "duration": 1}, {}, default_mode="reference")
    check(bool(errors), "duration=1 被拒（官方 2–30）")

    _, errors, _ = validate_task(
        {"prompt": "x", "mode": "edit", "imageUrls": ["https://cdn/a.png"]}, {},
        default_mode="edit")
    check(any("参考视频" in e for e in errors), "edit 缺视频被拒")

    _, errors, _ = validate_task(
        {"prompt": "x", "videoUrls": ["https://cdn/a.mp4", "https://cdn/b.mp4"],
         "duration": 17},
        {}, default_mode="reference", video_durations=[7.0, 7.0])
    check(any("超过 30" in e for e in errors), "输入+输出 >30s 被拒")

    _, errors, _ = validate_task(
        {"prompt": "x", "unexpected": 1}, {}, default_mode="reference")
    check(any("未知字段" in e for e in errors), "未知字段被拒")

    print("错误归类：")
    check(classify_error("InvalidApiKey", "bad key", 401).category == "auth", "401 InvalidApiKey → auth")
    check(classify_error("Throttling.BurstRate", "too quick", 429).category == "throttle", "429 → throttle")
    check(classify_error("DataInspectionFailed", "content", 200).category == "moderation",
          "DataInspectionFailed → moderation")
    check(classify_error("", "Failed to download multimodal content.", 500).category == "media_fetch",
          "下载素材失败 → media_fetch")
    check(classify_error("InvalidParameter", "bad", 400).category == "invalid", "400 → invalid")

    print("计费：")
    seconds, cost = estimate_cost(
        {"duration": 14.0, "output_video_duration": 7.0, "SR": 720}, "720P",
        {"720P": 0.5},
    )
    check(seconds == 14.0 and cost == 7.0, "usage.duration × 单价 = 7.0 元")

    print("配置：")
    cfg = WanConfig.from_mapping({
        "wan": {"api_key": "sk-x", "model": "wan3.0-video", "base_url": "https://dashscope.aliyuncs.com/api/v1",
                "price_per_second": {"720P": 0.5}},
    })
    check(cfg.api_key == "sk-x" and cfg.price_per_second == {"720P": 0.5}, "from_mapping 读取 wan 块")
    try:
        WanConfig(provider=PROVIDER_DASHSCOPE, api_key="", base_url="https://x/api/v1").validate()
    except Exception as exc:
        check("API Key" in str(exc), "缺 Key 在 validate 时报错")
    else:
        raise AssertionError("缺 Key 没有报错")

    print("素材上传：")
    tmp = tempdir("wan-media-")
    try:
        from PIL import Image
        rgb = tmp / "outfit.png"
        Image.new("RGB", (720, 900), (230, 228, 224)).save(rgb)
        store = FakeStorage()
        uploader = SourceUploader(store)
        url = resolve_image(rgb, uploader)
        check(url.startswith("https://cdn.example.com/prod/output/source/"), f"源图 URL：{url[:70]}")
        check(any(key.startswith("prod/output/source/") for key in store.objects), "源图 Key 在可写前缀内")
        check(store.signed and store.signed[0][1] == 7 * 24 * 3600, "签名 7 天")

        rgba = tmp / "alpha.png"
        Image.new("RGBA", (720, 900), (230, 228, 224, 128)).save(rgba)
        url2 = resolve_image(rgba, uploader)
        check(url2.endswith(".jpg?expires=604800") or ".jpg" in url2, "透明图转 JPEG 后上传")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("OK 官方适配层通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
