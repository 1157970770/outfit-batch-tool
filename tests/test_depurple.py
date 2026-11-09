#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""去紫算法（app/depurple.py）离线套件：色彩正确性、强度语义、锚点退化、落盘与缓存。

判据用的是「性质」而不是像素快照：
  · 强度 100 应把人工合成的紫偏扣掉 ≥80%，同时 L 几乎不动（±2 以内）；
  · 强度 0 必须是恒等变换（差 ≤1 个灰阶，量化误差）；
  · 暖色（A>0 且 B>0，肤色/米色）只改 10%，否则会把人的脸改成冷白；
  · 没有中性像素 / 没有暗部时锚点退化为 0，输出等于输入（宁可不修，也不能乱扣）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from _tmpdir import tempdir  # noqa: E402
from PIL import Image  # noqa: E402

from app import depurple  # noqa: E402


def casted_image(size=(320, 240), tint=(4.0, -1.0, 11.0), lo=30.0, hi=215.0,
                 noise=3.0, seed=7) -> Image.Image:
    """合成「灰渐变 + 轻微紫偏」：偏色以 RGB 偏移叠加，最贴近线上跑偏的图。"""
    import numpy as np

    width, height = size
    rng = np.random.default_rng(seed)
    col = np.linspace(lo, hi, width, dtype=np.float32)[None, :].repeat(height, axis=0)
    col = col + rng.normal(0, noise, (height, width)).astype(np.float32)
    rgb = col[..., None] + np.array(tint, dtype=np.float32)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")


def median_ab(image: Image.Image) -> tuple[float, float]:
    import numpy as np

    _, a, b = depurple.rgb_to_lab(np.asarray(image, dtype=np.float32))
    return float(np.median(a)), float(np.median(b))


def maxdiff(before: Image.Image, after: Image.Image) -> int:
    import numpy as np

    return int(np.abs(np.asarray(after, np.int16) - np.asarray(before, np.int16)).max())


def check_color() -> None:
    import numpy as np

    src = casted_image()
    result = depurple.depurple_array(src, 100)
    out = result["image"]
    assert result["cast_before"] > 5, result["cast_before"]
    assert result["cast_after"] <= result["cast_before"] * 0.2, result
    am, bm = median_ab(out)
    assert abs(am) < 1.5 and abs(bm) < 1.5, (am, bm)

    L0, _, _ = depurple.rgb_to_lab(np.asarray(src, dtype=np.float32))
    L1, _, _ = depurple.rgb_to_lab(np.asarray(out, dtype=np.float32))
    drift = np.abs(L1 - L0)
    assert float(drift.mean()) < 1.0 and float(drift.max()) <= 2.0, (drift.mean(), drift.max())

    # 强度 0 = 恒等；强度 50 应落在两者之间
    zero = depurple.depurple_array(src, 0)
    assert maxdiff(src, zero["image"]) <= 1, maxdiff(src, zero["image"])
    assert zero["cast_after"] == zero["cast_before"], zero
    half = depurple.depurple_array(src, 50)
    assert half["cast_after"] < zero["cast_after"] * 0.6, half
    assert half["cast_after"] > result["cast_after"], (half, result)
    print("  ✓ 强度 100 扣掉 ≥80% 偏色、L 漂移 ≤2；0 恒等；50 居中")


def check_warm_guard() -> None:
    """一张图里同时有中性灰和肤色：灰按全量扣，肤色（A>0,B>0）只扣 10%。"""
    import numpy as np

    src = casted_image()
    # 中间贴一块肤色（模特脸/手），其余是中性灰 —— 这样锚点仍然能测出来
    src.paste((200, 160, 130), (40, 30, 200, 150))
    result = depurple.depurple_array(src, 100)
    diff = np.abs(np.asarray(result["image"], np.int16) - np.asarray(src, np.int16)).max(2)
    skin = diff[35:145, 45:195]
    neutral = np.concatenate([diff[5:25, 5:-5].ravel(), diff[-25:-5, 5:-5].ravel()])
    assert result["cast_before"] > 5, result["cast_before"]
    assert float(skin.mean()) <= 4.0, f"肤色被改了 {float(skin.mean()):.2f} 个灰阶"
    assert float(neutral.mean()) > 5.0, f"中性区只改了 {float(neutral.mean()):.2f} 个灰阶"
    print(f"  ✓ 暖色保护：肤色 {float(skin.mean()):.2f} vs 中性 {float(neutral.mean()):.2f} 个灰阶")


def check_degenerate() -> None:
    """整幅高饱和 = 没有中性参考，锚点必须退化，输出等于输入。"""
    src = Image.new("RGB", (200, 150), (200, 40, 40))
    src.paste((40, 40, 210), (0, 0, 100, 150))
    result = depurple.depurple_array(src, 100)
    assert result["anchors"] == {"dark": [0.0, 0.0], "bg": [0.0, 0.0]}, result["anchors"]
    assert maxdiff(src, result["image"]) <= 1, maxdiff(src, result["image"])
    print("  ✓ 无中性参考时锚点退化、图不动")


def check_strength_and_size() -> None:
    for good in (0, 1, 50, 100, "60"):
        assert 0 <= depurple.parse_strength(good) <= 100
    assert depurple.parse_strength(None) == 100
    for bad in (-1, 101, "abc", True, "1e3", 50.5):
        try:
            depurple.parse_strength(bad)
        except ValueError:
            continue
        raise AssertionError(f"强度 {bad!r} 应被拒绝")

    assert not hasattr(depurple, "check_size") and not hasattr(depurple, "DepurpleTooLarge"), \
        "像素上限已按需求去掉：不该再留 check_size / DepurpleTooLarge，也不该有 413 分支"
    print("  ✓ 强度校验（像素上限已移除）")


def check_process_file() -> None:
    tmp = tempdir("depurple-")
    src = tmp / "look.png"
    casted_image().save(src, format="PNG")
    out = tmp / "ps" / "abc_s100.png"

    meta = depurple.process_file(src, out, 100)
    assert out.is_file() and out.with_suffix(".json").is_file()
    assert meta["elapsed_ms"] >= 0 and meta["created_at"] > 0
    assert meta["cast_after"] <= meta["cast_before"] * 0.2, meta
    with Image.open(out) as saved:
        assert saved.format == "PNG" and saved.mode == "RGB"
        assert saved.size == image_size(src), (saved.size, image_size(src))
    sidecar = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["strength"] == 100 and "cast_before" in sidecar and "anchors" in sidecar, sidecar

    hit = depurple.read_cached(out, src)
    assert hit and hit["created_at"] == meta["created_at"], hit
    # 原图比结果新（用户重新覆盖了同名文件）→ 缓存失效，必须重算
    future = time.time() + 10
    os.utime(src, (future, future))
    assert depurple.read_cached(out, src) is None
    print("  ✓ process_file 写 PNG + 侧车；缓存命中/失效按 mtime")


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as handle:
        return handle.size


def check_exif_rotate() -> None:
    """手机竖拍（EXIF orientation=6）：输出必须转正，否则前后对比一张躺一张立。"""
    tmp = tempdir("depurple-exif-")
    src = tmp / "portrait.jpg"
    img = Image.new("RGB", (100, 200), (150, 150, 150))
    exif = img.getexif()
    exif[0x0112] = 6
    img.save(src, format="JPEG", exif=exif)
    out = tmp / "ps" / "rot_s100.png"
    depurple.process_file(src, out, 100)
    assert image_size(out) == (200, 100), image_size(out)
    print("  ✓ EXIF 方向按浏览器口径转正")


def check_bad_and_missing() -> None:
    tmp = tempdir("depurple-bad-")
    broken = tmp / "broken.png"
    broken.write_bytes(b"not an image")
    try:
        depurple.process_file(broken, tmp / "ps" / "x_s100.png", 100)
    except depurple.DepurpleBadImage as exc:
        assert exc.status == 422
    else:
        raise AssertionError("坏图应抛 DepurpleBadImage")

    saved = depurple.HAS_NUMPY
    try:
        depurple.HAS_NUMPY = False
        assert not depurple.available()
        try:
            depurple.depurple_array(casted_image())
        except depurple.DepurpleUnavailable as exc:
            assert exc.status == 503
        else:
            raise AssertionError("缺 numpy 应抛 DepurpleUnavailable")
    finally:
        depurple.HAS_NUMPY = saved
    print("  ✓ 坏图 422 / 缺 numpy 503")


def main() -> int:
    if not depurple.available():
        print("[SKIP] 当前解释器缺 numpy（换成带 numpy 的解释器，或 pip install numpy）")
        return 0
    print("去紫算法套件：")
    check_color()
    check_warm_guard()
    check_degenerate()
    check_strength_and_size()
    check_process_file()
    check_exif_rotate()
    check_bad_and_missing()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
