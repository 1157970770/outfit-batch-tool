#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PS 助手（去紫）HTTP 端到端：上传 → 处理 → 取图 → 缓存 → 权限/参数错误码。

只跑本机回环，不必起 DashScope mock：去紫完全不碰 wan（`build_settings` 里的
`mock_base` 只写进清单 URL，离线不会被访问）。
"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import sys
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
from app import depurple  # noqa: E402
from app.web.server import WebApplication  # noqa: E402

MOCK_BASE = "http://127.0.0.1:1"        # 只做占位，本套件不发起任何 wan 请求


def casted_png(size=(320, 240)) -> bytes:
    import numpy as np

    width, height = size
    col = np.linspace(30, 215, width, dtype=np.float32)[None, :].repeat(height, axis=0)
    rgb = col[..., None] + np.array([4.0, -1.0, 11.0], dtype=np.float32)
    buf = io.BytesIO()
    Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def upload(opener, base, name="look.png") -> str:
    raw = casted_png()
    code, _, payload = call(opener, base, "/api/uploads", "POST", {
        "filename": name,
        "data": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    })
    assert code == 201, (code, payload)
    return body(payload)["upload_id"]


def login(base, *, users):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for name, password in users:
        code, _, raw = call(opener, base, "/api/login", "POST", {"username": name, "password": password})
        assert code == 200, (name, code, raw)
    return opener


def main() -> int:
    if not depurple.available():
        print("[SKIP] 当前解释器缺 numpy，PS 助手接口会 503；换成带 numpy 的解释器再跑")
        return 0
    tmp = tempdir("ps-")
    workbook = tmp / "标签清单.xlsx"
    write_workbook(workbook, build_rows())
    settings = build_settings(tmp, build_manifest(tmp, MOCK_BASE), workbook, MOCK_BASE)
    assert not settings.templates_error, settings.templates_error
    app = WebApplication(settings)
    server, base = start_server(app)
    try:
        alice = login(base, users=[("alice", "alice-pass-123")])
        bob = login(base, users=[("bob", "bob-pass-12345")])
        admin = login(base, users=[("admin", "admin-pass-123")])
        data_dir = Path(settings.data_dir)
        upload_id = upload(alice, base)

        # 1. 首次处理：全量强度
        code, _, raw = call(alice, base, "/api/ps/depurple", "POST",
                            {"upload_id": upload_id, "strength": 100})
        assert code == 200, (code, raw)
        payload = body(raw)
        assert payload["ok"] and payload["strength"] == 100 and payload["cached"] is False, payload
        assert payload["cast_after"] <= payload["cast_before"] * 0.2, payload
        assert payload["width"] == 320 and payload["height"] == 240, payload
        assert payload["elapsed_ms"] >= 0, payload
        png = data_dir / "ps" / f"{upload_id}_s100.png"
        assert png.is_file() and png.with_suffix(".json").is_file(), png
        assert payload["url"].startswith(f"/api/ps/result/{upload_id}?strength=100"), payload

        # 2. 取图：inline 可给 <img>，download=1 是附件
        code, headers, raw = call(alice, base, payload["url"])
        assert code == 200 and headers.get("Content-Type") == "image/png", (code, headers)
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:8]
        code, headers, raw = call(alice, base, payload["download_url"])
        assert code == 200 and "attachment" in (headers.get("Content-Disposition") or ""), headers
        assert "去紫" in urllib.parse.unquote(headers.get("Content-Disposition") or ""), headers
        print("  ✓ 处理落盘 + inline/下载两种取图")

        # 3. 同参数重复调用命中缓存（不再重算）
        code, _, raw = call(alice, base, "/api/ps/depurple", "POST",
                            {"upload_id": upload_id, "strength": 100})
        again = body(raw)
        assert code == 200 and again["cached"] is True, again
        assert again["cast_after"] == payload["cast_after"], again
        print("  ✓ 重复请求命中侧车缓存")

        # 4. 换强度是另一份结果；没算过的强度取图 404
        code, _, raw = call(alice, base, f"/api/ps/result/{upload_id}?strength=50")
        assert code == 404, (code, raw)
        code, _, raw = call(alice, base, "/api/ps/depurple", "POST",
                            {"upload_id": upload_id, "strength": 50})
        half = body(raw)
        assert code == 200 and half["cached"] is False, (code, half)
        assert half["cast_after"] > payload["cast_after"], (half, payload)
        assert (data_dir / "ps" / f"{upload_id}_s50.png").is_file()
        print("  ✓ 强度各自一份结果，未生成的强度 404")

        # 5. 权限：别人看不到也算不出来；admin 可看可算（与 /api/uploads 一致）
        code, _, raw = call(bob, base, "/api/ps/depurple", "POST", {"upload_id": upload_id})
        assert code == 404, (code, raw)
        code, _, _ = call(bob, base, payload["url"])
        assert code == 404
        code, _, _ = call(admin, base, payload["url"])
        assert code == 200
        code, _, raw = call(admin, base, "/api/ps/depurple", "POST",
                            {"upload_id": upload_id, "strength": 80})
        assert code == 200 and body(raw)["cached"] is False, (code, raw)
        print("  ✓ owner 隔离（他人 404）+ admin 放行")

        # 6. 参数与状态码
        for bad_body, expect in (({}, 400),
                                 ({"upload_id": "not-an-id"}, 400),
                                 ({"upload_id": upload_id, "strength": 101}, 400),
                                 ({"upload_id": upload_id, "strength": "abc"}, 400),
                                 ({"upload_id": "f" * 32}, 404)):
            code, _, raw = call(alice, base, "/api/ps/depurple", "POST", bad_body)
            assert code == expect, (bad_body, code, raw)
        code, _, raw = call(alice, base, f"/api/ps/result/{upload_id}?strength=999")
        assert code == 400, (code, raw)
        code, _, raw = call(alice, base, "/api/ps/result/" + "f" * 32)
        assert code == 404, (code, raw)
        print("  ✓ 400 参数 / 404 找不到")

        # 7. 未登录直接 401（与其它接口一致）
        anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        code, _, _ = call(anon, base, "/api/ps/depurple", "POST", {"upload_id": upload_id})
        assert code == 401, code
        code, _, _ = call(anon, base, payload["url"])
        assert code == 401, code
        print("  ✓ 未登录 401")

        # 8. 侧车里的统计能被管理端/排障直接读（纯文本）
        sidecar = json.loads((data_dir / "ps" / f"{upload_id}_s100.json").read_text(encoding="utf-8"))
        assert sidecar["strength"] == 100 and sidecar["created_at"] > 0, sidecar
        assert {"cast_before", "cast_after", "anchors", "width", "height"} <= set(sidecar), sidecar
        print("  ✓ 侧车统计完整")
    finally:
        server.shutdown()
        server.server_close()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
