#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""去紫：在 Lab 空间只动 A/B、完全不动 L，修掉 AI 多轮生产累积的偏色。

算法来自 `D:\\定妆自动化区\\plugins\\dsh-ps-assistant\\bridge.py` 的
`_rgb2lab` / `_lab2rgb` / `op_depurple`（bridge.py:249-361），这里按网页需要重写成
自包含模块（只依赖 numpy + Pillow），不 import 任何 `app.web.*`。

口径（与插件一致）：
  1) 转 Lab，只动 A/B，L 完全不动；
  2) 两个锚点：暗部 Lg<60、背景（低饱和像素最多的亮部区间，32 档直方图取 Lg>=120 的峰）；
  3) 偏色量按 Lg 在「暗部 → 背景」之间线性插值，背景以上取背景值；
  4) 高光端 taper 收 25%（实测白料高光自带偏色小于背景，全量扣会发青）；
  5) 暖色保护：A>0 且 B>0（肤色/棕色/米色）只改 10%；
  6) 应用后 1/3 抽样量一次残留，残留 |A| 或 |B| > 1.0 就把锚点推一次（自校准）；
  7) PNG 无损输出，原子写（临时文件 + os.replace）。

已知限制：整图一次性 numpy 运算（不分块），峰值内存大约是「像素数 × 100 字节」——
2400 万像素约 2.4GB、1 亿像素约 10GB。不设像素上限，超出现在机器的可用内存时
会抛 MemoryError，由接口回一条「内存不足，请先缩图」；要支持任意大图需改成分块 + 代理锚点，
见 docs/ps-assistant.md。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PIL import Image, ImageOps

try:  # 运行区/设计区的 DSH 解释器都自带 numpy；缺了只让 PS 助手 503，不影响别的功能
    import numpy as np

    HAS_NUMPY = True
except Exception:  # pragma: no cover - 只在缺依赖的机器上走到
    np = None
    HAS_NUMPY = False


DEFAULT_STRENGTH = 100
MAX_STRENGTH = 100

# sRGB → XYZ（D65），与插件逐位一致；别换成别的矩阵，否则前后对比会和插件对不上。
_M = [[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]
_XN, _YN, _ZN = 0.95047, 1.0, 1.08883
_NEUTRAL_SAT = 30          # 低饱和（饱和度 < 30）才算「可能是中性灰」
_MIN_BAND_PX = 300         # 锚点带里少于 300 px 就认为没背景/没暗部
_RESIDUAL_LIMIT = 1.0      # 残留超过这个才做第二次自校准


class DepurpleError(RuntimeError):
    """去紫失败的基类；status 直接用作 HTTP 状态码。"""

    status = 500


class DepurpleUnavailable(DepurpleError):
    status = 503


class DepurpleBadImage(DepurpleError):
    status = 422


def available() -> bool:
    return HAS_NUMPY


def _require_numpy() -> None:
    if not HAS_NUMPY:
        raise DepurpleUnavailable("缺少 numpy，装好依赖后重启服务即可使用 PS 助手。")


def parse_strength(value) -> int:
    """强度统一在这里校验：0–100 的整数，越界/非整数抛 ValueError（→ 400）。"""
    if value is None or value == "":
        return DEFAULT_STRENGTH
    if isinstance(value, bool):  # bool 是 int 的子类，别让 true/false 混过去
        raise ValueError("强度必须是 0–100 的整数。")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("强度必须是 0–100 的整数。")
    try:
        strength = int(value)
    except (TypeError, ValueError):
        raise ValueError("强度必须是 0–100 的整数。") from None
    if strength < 0 or strength > MAX_STRENGTH:
        raise ValueError(f"强度必须在 0–{MAX_STRENGTH} 之间。")
    return strength


# ---------------------------------------------------------------- Lab 换算
def rgb_to_lab(rgb):
    """uint8/float 的 (H,W,3) sRGB → L, A, B（float32）。"""
    a = rgb.astype(np.float32) if hasattr(rgb, "astype") else np.asarray(rgb, dtype=np.float32)
    x = a / 255.0
    x = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    xyz = x @ np.array(_M, dtype=np.float32).T
    f = lambda t: np.where(t > 0.008856, np.cbrt(np.maximum(t, 1e-9)), 7.787 * t + 16 / 116)  # noqa: E731
    fx, fy, fz = f(xyz[..., 0] / _XN), f(xyz[..., 1] / _YN), f(xyz[..., 2] / _ZN)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def lab_to_rgb(L, A, B):
    """L, A, B → float RGB（0–255，未取整）。

    插件里逆矩阵用 float64 算；这里统一 float32（3×3 常量求逆的误差 < 0.01 个灰阶），
    换来的是一半的内存峰值。大图（24MP 以上）先看机器的可用内存，见 docs/ps-assistant.md。
    """
    fy = (L + 16) / 116
    fx = fy + A / 500
    fz = fy - B / 200
    inv = lambda t: np.where(t ** 3 > 0.008856, t ** 3, (t - 16 / 116) / 7.787)  # noqa: E731
    xyz = np.stack([inv(fx) * _XN, inv(fy) * _YN, inv(fz) * _ZN], axis=-1)
    lin = xyz @ np.linalg.inv(np.array(_M, dtype=np.float32)).T
    lin = np.clip(lin, 0, 1)
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(srgb * 255, 0, 255)


# ---------------------------------------------------------------- 去紫
def _load_rgb(path: Path) -> Image.Image:
    try:
        with Image.open(path) as handle:
            # 浏览器按 EXIF 方向显示 /api/uploads 的原图；这里不转正的话前后对比会一张躺一张立。
            return ImageOps.exif_transpose(handle).convert("RGB")
    except (OSError, ValueError) as exc:
        raise DepurpleBadImage(f"无法读取这张图片：{exc}") from None


def depurple_array(img: Image.Image, strength: int = DEFAULT_STRENGTH) -> dict:
    """对 PIL 图片做去紫，返回 dict（含 image / 锚点 / 前后色偏 / 尺寸）。"""
    _require_numpy()
    strength = parse_strength(strength)
    src = img if img.mode == "RGB" else img.convert("RGB")
    width, height = src.size

    a = np.asarray(src, dtype=np.float32)
    L, A, B = rgb_to_lab(a)

    Lg = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    sat = a.max(2) - a.min(2)
    neutral = sat < _NEUTRAL_SAT
    del a, sat  # 只留 Lab 与 Lg：不设像素上限后，先把 12 字节/像素的原图放掉再往下算

    def cast_of(Ax, Bx):
        cx = np.sqrt(Ax * Ax + Bx * Bx)
        mx = np.clip(1.0 - cx / 60.0, 0.0, 1.0) > 0.5
        return float(np.mean(np.abs(Bx - Ax)[mx])) if np.any(mx) else 0.0

    cast_before = cast_of(A, B)

    hist, edges = np.histogram(Lg[neutral], bins=32, range=(0, 256))
    centers = (edges[:-1] + edges[1:]) / 2
    bright = np.where(centers >= 120)[0]
    bi = int(bright[np.argmax(hist[bright])]) if len(bright) else int(np.argmax(hist))
    lo_b, hi_b = float(edges[bi]), float(edges[bi + 1])

    # 暖色保护：A 与 B 同时为正 = 红/黄方向（肤色、棕色、米色）
    warm = np.clip((A - 2.0) / 10.0, 0, 1) * np.clip((B + 2.0) / 10.0, 0, 1)
    w_app = (1.0 - 0.9 * warm) * (strength / 100.0)

    def band_med(Ax, Bx, mask, neu):
        # 中性掩膜必须跟着一起传：自校准用的是抽样图自己的 neutral2，不是原图的。
        m = mask & neu
        if int(m.sum()) < _MIN_BAND_PX:
            return None
        return float(np.median(Ax[m])), float(np.median(Bx[m]))

    def anchors():
        dark = band_med(A, B, Lg < 60, neutral)
        bg = band_med(A, B, (Lg >= lo_b) & (Lg < hi_b), neutral)
        bg = bg or dark or (0.0, 0.0)
        dark = dark or bg
        return dark, bg

    # 高光端稍收一点（实测白料高光自带偏色小于背景，全量扣会发青）
    taper = 1.0 - 0.25 * np.clip((Lg - 215.0) / 40.0, 0, 1)

    def apply(dark, bg):
        def off(x, lo_v, hi_v):
            t = np.clip((x - 50.0) / max(1.0, hi_b - 50.0), 0, 1)
            return lo_v + (hi_v - lo_v) * t

        ca = off(Lg, dark[0], bg[0]) * taper
        cb = off(Lg, dark[1], bg[1]) * taper
        A_new = A - ca * w_app
        B_new = B - cb * w_app
        return lab_to_rgb(L, A_new, B_new), A_new, B_new

    dark, bg = anchors()
    out, A_out, B_out = apply(dark, bg)

    # 自校准（只在残留明显时做一次；用 1/3 抽样测残留，省 2/3 时间）
    small = out[::3, ::3]
    L2, A2, B2 = rgb_to_lab(small)
    Lg2 = 0.299 * small[..., 0] + 0.587 * small[..., 1] + 0.114 * small[..., 2]
    neutral2 = (small.max(2) - small.min(2)) < _NEUTRAL_SAT
    rb = band_med(A2, B2, (Lg2 >= lo_b) & (Lg2 < hi_b), neutral2)
    rd = band_med(A2, B2, Lg2 < 60, neutral2)
    if rb and (abs(rb[0]) > _RESIDUAL_LIMIT or abs(rb[1]) > _RESIDUAL_LIMIT):
        bg2 = (bg[0] + rb[0], bg[1] + rb[1])
        dark2 = (dark[0] + (rd[0] if rd else 0.0), dark[1] + (rd[1] if rd else 0.0))
        out, A_out, B_out = apply(dark2, bg2)

    def cast_of(Ax, Bx):
        cx = np.sqrt(Ax * Ax + Bx * Bx)
        mx = np.clip(1.0 - cx / 60.0, 0.0, 1.0) > 0.5
        return float(np.mean(np.abs(Bx - Ax)[mx])) if np.any(mx) else 0.0

    cast_after = cast_of(A_out, B_out)
    del A_out, B_out  # 生成 uint8 结果前先放掉两张 float 图，省一次大数组拷贝的峰值

    image = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGB")
    del out
    return {
        "image": image,
        "width": width,
        "height": height,
        "strength": strength,
        "anchors": {"dark": [round(v, 2) for v in dark], "bg": [round(v, 2) for v in bg]},
        "cast_before": round(cast_before, 2),
        "cast_after": round(cast_after, 2),
    }


# ---------------------------------------------------------------- 落盘 / 缓存
def read_cached(out: Path, src: Path) -> dict | None:
    """结果图 + 侧车都在、且不旧于原图 → 返回侧车（重复点同一强度秒回）。"""
    sidecar = out.with_suffix(".json")
    try:
        if not out.is_file() or not sidecar.is_file():
            return None
        if out.stat().st_mtime < src.stat().st_mtime:
            return None
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_save_png(image: Image.Image, out: Path) -> None:
    tmp = out.parent / (out.name + ".tmp")
    try:
        image.save(tmp, format="PNG")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _atomic_write_json(meta: dict, sidecar: Path) -> None:
    tmp = sidecar.parent / (sidecar.name + ".tmp")
    try:
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, sidecar)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def process_file(src: Path, out: Path, strength: int = DEFAULT_STRENGTH) -> dict:
    """读原图 → 去紫 → 原子写 `out`(PNG) + 同名 `.json` 侧车，返回统计。"""
    _require_numpy()
    started = time.monotonic()
    image = _load_rgb(src)
    result = depurple_array(image, strength)
    out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_save_png(result.pop("image"), out)
    meta = {
        "ok": True,
        "strength": result["strength"],
        "width": result["width"],
        "height": result["height"],
        "anchors": result["anchors"],
        "cast_before": result["cast_before"],
        "cast_after": result["cast_after"],
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "created_at": int(time.time()),
    }
    _atomic_write_json(meta, out.with_suffix(".json"))
    return meta
