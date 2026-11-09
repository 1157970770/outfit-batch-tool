#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生图 tab（老张 API）：队列 / 落盘 / 4:5 裁切 / owner 隔离 / 校验 / 重启恢复。

真实的 gpt-image VIP 一次 $0.03，这里把 `ImageGenManager._generate/_edit` 换成桩，
其余全是真代码：任务记录、参考图复制、最终尺寸处理、HTTP 路由、权限过滤。
"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import sys
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
from app.imggen import ImageGenError, ImageGenManager  # noqa: E402
from app.imggen import gemini  # noqa: E402
from app.imggen.processing import api_size_for_output, finalize_output_image  # noqa: E402
from app.web.server import WebApplication  # noqa: E402

MOCK_BASE = "http://127.0.0.1:1"        # 占位地址：本套件不会真的发请求
NB_MODEL = "gemini-nano-banana-2.1"     # Gemini 原生协议，尺寸口径与 Images 完全不同


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


def login(base: str, *, users) -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for name, password in users:
        code, _, raw = call(opener, base, "/api/login", "POST", {"username": name, "password": password})
        assert code == 200, (name, code, raw)
    return opener


def wait_done(opener, base: str, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _, raw = call(opener, base, f"/api/imagegen/tasks/{task_id}")
        assert code == 200, (code, raw)
        task = body(raw)["task"]
        if not task["active"]:
            return task
        time.sleep(0.1)
    raise AssertionError("生图任务 30 秒内没跑完（桩不联网，正常应 <1 秒）")


class Shim:
    """ImageGenManager 只用 settings.data_dir 与 settings.imagegen。"""

    def __init__(self, data_dir, imagegen):
        self.data_dir = data_dir
        self.imagegen = imagegen


def main() -> int:
    tmp = tempdir("imagegen-")
    workbook = tmp / "标签清单.xlsx"
    write_workbook(workbook, build_rows())
    # 生图配置与仓库同路径：base_dir/server/imagegen.json，顺便验证 settings 的接线。
    (tmp / "server").mkdir(parents=True, exist_ok=True)
    (tmp / "server" / "imagegen.json").write_text(json.dumps({
        "enabled": True, "base_url": MOCK_BASE, "api_key": "sk-test-imagegen-key",
        "model": "gpt-image-2-vip", "default_quality": "high", "timeout_seconds": 60,
        "max_count": 3, "max_references": 4, "max_inflight": 2, "price_per_image": "0.03",
    }, ensure_ascii=False), encoding="utf-8")

    settings = build_settings(tmp, build_manifest(tmp, MOCK_BASE), workbook, MOCK_BASE)
    assert not settings.templates_error, settings.templates_error
    assert settings.imagegen_error == "", settings.imagegen_error
    assert settings.imagegen.get("api_key") == "sk-test-imagegen-key", settings.imagegen
    assert str(settings.imagegen_file or "").endswith("imagegen.json"), settings.imagegen_file

    app = WebApplication(settings)
    calls: list[tuple] = []

    def fake_edit(prompt, size, paths, api_key, **kwargs):
        calls.append(("edit", prompt, size, [Path(p).name for p in paths],
                      kwargs.get("model"), kwargs.get("quality")))
        return png_bytes(2048, 2048)

    def fake_generate(prompt, size, api_key, **kwargs):
        calls.append(("generate", prompt, size))
        return png_bytes(1024, 1024)

    def fake_gemini_generate(prompt, size, api_key, **kwargs):
        calls.append(("gemini-generate", prompt, size, kwargs.get("model"),
                      kwargs.get("base_url"), kwargs.get("timeout")))
        return png_bytes(640, 800)

    def fake_gemini_edit(prompt, size, paths, api_key, **kwargs):
        calls.append(("gemini-edit", prompt, size, [Path(p).name for p in paths],
                      kwargs.get("model")))
        return png_bytes(640, 800)

    app.imagegen._edit = fake_edit
    app.imagegen._generate = fake_generate
    app.imagegen._gemini_generate = fake_gemini_generate
    app.imagegen._gemini_edit = fake_gemini_edit

    server, base = start_server(app)
    data_dir = Path(settings.data_dir)
    try:
        alice = login(base, users=[("alice", "alice-pass-123")])
        bob = login(base, users=[("bob", "bob-pass-12345")])
        admin = login(base, users=[("admin", "admin-pass-123")])

        # 1. 选项由后端下发：型号/尺寸/质量/上限都在这里，前端不重复写规则
        code, _, raw = call(alice, base, "/api/bootstrap")
        assert code == 200
        options = body(raw)["service"]["imagegen"]
        assert options["configured"] is True, options
        assert len(options["models"]) == 6, options["models"]
        assert options["max_count"] == 3 and options["max_references"] == 4, options
        assert options["default_model"] == "gpt-image-2-vip"
        assert options["prompt_limit"] == 4000
        model_25 = next(item for item in options["models"] if item["id"] == "gpt-image-2.5-flare-vip")
        assert len(model_25["qualities"]) == 6, model_25
        nano = next(item for item in options["models"] if item["id"] == NB_MODEL)
        assert nano["provider"] == "gemini", nano
        assert len(nano["sizes"]) == 19 and nano["qualities"] == [
            {"value": "auto", "label": "自动｜由模型决定"}], nano
        assert "2048x2048" not in [item["value"] for item in nano["sizes"]], nano
        print("  ✓ bootstrap 下发型号/尺寸/质量与上限")

        # 2. 提交带参考图的任务：2 张、4:5
        upload_id = upload_png(alice, base)
        code, _, raw = call(alice, base, "/api/imagegen/tasks", "POST", {
            "prompt": "白色棉质短袖衬衫，正面站立", "model": "gpt-image-2-vip",
            "size": "4:5", "quality": "high", "count": 2, "upload_ids": [upload_id],
        })
        assert code == 201, (code, raw)
        task = body(raw)["task"]
        task_id = task["id"]
        assert task["count"] == 2 and task["active"] is True, task
        assert task["api_size"] == "2048x2048", task
        assert task["references"] and task["references"][0]["index"] == 1, task

        task = wait_done(alice, base, task_id)
        assert task["status"] == "已完成", task
        assert task["progress"] == 2 and len(task["images"]) == 2, task
        assert task["elapsed_ms"] >= 0
        for image in task["images"]:
            ratio = image["width"] / image["height"]
            assert abs(ratio - 0.8) < 0.002, image      # 4:5 精确到像素取整
        print("  ✓ 任务排队 → 逐张生成 → 4:5 裁切落盘")

        # 3. 桩收到的调用：两次编辑、参考图是复制到任务目录的那份、参数直传
        edits = [item for item in calls if item[0] == "edit"]
        assert len(edits) == 2, calls
        assert all(item[1] == "白色棉质短袖衬衫，正面站立" for item in edits)
        assert all(item[2] == "2048x2048" for item in edits)
        assert all(item[3] == ["ref1.png"] for item in edits), edits
        assert all(item[4] == "gpt-image-2-vip" and item[5] == "high" for item in edits)
        task_dir = data_dir / "imagegen" / "files" / task_id
        assert (task_dir / "ref1.png").is_file(), task_dir
        assert (task_dir / "1.meta.json").is_file() and (task_dir / "2.png").is_file()
        sidecar = json.loads((task_dir / "1.meta.json").read_text(encoding="utf-8"))
        assert sidecar["output_size"][0] / sidecar["output_size"][1] > 0.79, sidecar
        assert sidecar["reference_images"] == ["ref1.png"] and sidecar["operator"] == "alice", sidecar
        print("  ✓ 参考图随任务复制，侧车记录尺寸/参考图/操作人")

        # 4. 取图：inline 给 <img>，download=1 是附件；参考图也能回看
        code, headers, raw = call(alice, base, task["images"][0]["url"])
        assert code == 200 and headers.get("Content-Type") == "image/png", (code, headers)
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:8]
        code, headers, raw = call(alice, base, task["images"][0]["download_url"])
        assert code == 200 and "attachment" in (headers.get("Content-Disposition") or ""), headers
        assert "png" in urllib.parse.unquote(headers.get("Content-Disposition") or "").lower()
        code, _, raw = call(alice, base, task["references"][0]["url"])
        assert code == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n"
        print("  ✓ inline / 附件下载 / 参考图回看")

        # 5. owner 隔离：bob 列表为空、看图和删除都 404；admin 能看到
        code, _, raw = call(bob, base, "/api/imagegen/tasks")
        assert code == 200 and body(raw)["items"] == [], body(raw)
        code, _, _ = call(bob, base, task["images"][0]["url"])
        assert code == 404, code
        code, _, _ = call(bob, base, f"/api/imagegen/tasks/{task_id}", "DELETE")
        assert code == 404, code
        code, _, raw = call(admin, base, "/api/imagegen/tasks")
        assert code == 200 and [item["id"] for item in body(raw)["items"]] == [task_id], body(raw)
        print("  ✓ owner 隔离（他人 404）+ admin 全量")

        # 6. 校验与状态码
        bad_cases = [
            ({"prompt": ""}, 400),
            ({"prompt": "x", "model": "dall-e-3"}, 400),
            ({"prompt": "x", "model": "gpt-image-2-vip", "size": "512x512"}, 400),
            ({"prompt": "x", "model": "gpt-image-2-vip", "quality": "xhigh"}, 400),
            ({"prompt": "x" * 4001}, 400),
            ({"prompt": "x", "upload_ids": ["f" * 32]}, 404),
            ({"prompt": "x", "count": 0}, 201),
        ]
        for payload, expect in bad_cases:
            code, _, raw = call(alice, base, "/api/imagegen/tasks", "POST", payload)
            assert code == expect, (payload, code, raw)
            if code == 201:
                # count 会被夹到 1；顺手删掉，别留脏记录
                created = body(raw)["task"]
                assert created["count"] == 1, created
                call(alice, base, f"/api/imagegen/tasks/{created['id']}", "DELETE")
        code, _, raw = call(alice, base, "/api/imagegen/tasks", "POST",
                            {"prompt": "x", "count": 99})
        assert code == 201, (code, raw)
        clamped = body(raw)["task"]
        assert clamped["count"] == 3, clamped        # max_count=3
        wait_done(alice, base, clamped["id"])
        call(alice, base, f"/api/imagegen/tasks/{clamped['id']}", "DELETE")
        print("  ✓ 400 参数 / 404 找不到 / 张数夹到上限")

        # 7. 未登录 401
        anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        code, _, _ = call(anon, base, "/api/imagegen/tasks")
        assert code == 401, code
        code, _, _ = call(anon, base, "/api/imagegen/tasks", "POST", {"prompt": "x"})
        assert code == 401, code
        print("  ✓ 未登录 401")

        # 8. 删除：连本地文件一起走
        code, _, _ = call(alice, base, f"/api/imagegen/tasks/{task_id}", "DELETE")
        assert code == 200, code
        assert not task_dir.exists(), task_dir
        assert not (data_dir / "imagegen" / "tasks" / f"{task_id}.json").exists()
        code, _, _ = call(alice, base, task["images"][0]["url"])
        assert code == 404, code
        print("  ✓ 删除记录同时删本地文件")

        # 9. Nano Banana 2.1：Gemini 原生协议，尺寸是「比例@档位」，模型直出比例、不本地裁切
        code, _, raw = call(alice, base, "/api/imagegen/tasks", "POST", {
            "prompt": "生成一张图片：白色棉质短袖衬衫，正面站立", "model": NB_MODEL,
            "size": "4:5@2K", "count": 1,
        })
        assert code == 201, (code, raw)
        nb_task = body(raw)["task"]
        assert nb_task["api_size"] == "2K（4:5）", nb_task
        assert nb_task["size_label"] == "竖版 4:5｜2K", nb_task
        assert nb_task["quality"] == "auto", nb_task       # 没传 quality：退到该型号唯一档
        nb_done = wait_done(alice, base, nb_task["id"])
        assert nb_done["status"] == "已完成", nb_done
        first = nb_done["images"][0]
        # 比例由模型保证，本地不该再裁：宽高应等于桩返回的 640x800
        assert (first["width"], first["height"]) == (640, 800), first
        nb_calls = [item for item in calls if item[0] == "gemini-generate"]
        assert nb_calls and nb_calls[-1][2] == "4:5@2K", nb_calls
        assert nb_calls[-1][3] == NB_MODEL and nb_calls[-1][4] == MOCK_BASE, nb_calls
        assert nb_calls[-1][5] == 60, nb_calls              # timeout_seconds 原样传下去

        # 带参考图 → gemini-edit，参考图顺序就是模型眼里的「图1、图2…」
        ref_upload = upload_png(alice, base, name="nb-ref.png")
        code, _, raw = call(alice, base, "/api/imagegen/tasks", "POST", {
            "prompt": "换成这件衬衫", "model": NB_MODEL, "size": "1:1@4K",
            "count": 1, "upload_ids": [ref_upload],
        })
        assert code == 201, (code, raw)
        nb_edit_task = body(raw)["task"]
        assert nb_edit_task["api_size"] == "4K（1:1）", nb_edit_task
        nb_edit_done = wait_done(alice, base, nb_edit_task["id"])
        assert nb_edit_done["status"] == "已完成", nb_edit_done
        nb_edits = [item for item in calls if item[0] == "gemini-edit"]
        assert nb_edits and nb_edits[-1][2] == "1:1@4K", nb_edits
        assert nb_edits[-1][3] == ["ref1.png"], nb_edits

        # 两套尺寸口径不能串台：像素规格只属于 Images 协议，比例档位只属于 Gemini
        mismatched = [
            {"prompt": "x", "model": NB_MODEL, "size": "2048x2048"},
            {"prompt": "x", "model": NB_MODEL, "quality": "high"},
            {"prompt": "x", "model": "gpt-image-2-vip", "size": "4:5@2K"},
        ]
        for payload in mismatched:
            code, _, raw = call(alice, base, "/api/imagegen/tasks", "POST", payload)
            assert code == 400, (payload, code, raw)
        for task_id_ in (nb_task["id"], nb_edit_task["id"]):
            call(alice, base, f"/api/imagegen/tasks/{task_id_}", "DELETE")
        print("  ✓ Nano Banana 走 Gemini 原生协议：比例@档位、不裁切、口径不串台")

        # 10. 未配置时：manager 直接 503，options 里带原因
        empty = ImageGenManager(Shim(settings.data_dir, {}), root=tmp / "imagegen-empty")
        assert empty.configured is False
        assert "api_key" in empty.unconfigured_reason(), empty.unconfigured_reason()
        assert empty.options()["configured"] is False
        try:
            empty.create_task({"username": "alice"}, {"prompt": "x"})
        except ImageGenError as exc:
            assert exc.status == 503, exc
        else:
            raise AssertionError("未配置时应当拒绝创建任务")
        print("  ✓ 未配置 → 503 + 可读原因")

        # 11. 重启恢复：磁盘上「生成中」的旧任务不能永远显示进行中
        recover_root = tmp / "imagegen-recover"
        (recover_root / "tasks").mkdir(parents=True)
        (recover_root / "files").mkdir()
        stale_id = "a" * 32
        (recover_root / "tasks" / f"{stale_id}.json").write_text(json.dumps({
            "id": stale_id, "owner": "alice", "created_at": time.time(), "prompt": "旧任务",
            "model": "gpt-image-2-vip", "size": "auto", "count": 1, "status": "生成中",
            "progress": 0, "images": [], "references": [], "error": "",
        }, ensure_ascii=False), encoding="utf-8")
        recovered = ImageGenManager(
            Shim(settings.data_dir, {"api_key": "k", "base_url": MOCK_BASE}), root=recover_root)
        records = recovered.list_tasks({"username": "alice", "role": "user"})
        assert len(records) == 1 and records[0]["status"] == "生成失败", records
        assert "服务重启" in records[0]["error"], records[0]
        print("  ✓ 重启后残留的进行中任务标记为失败")

        # 12. 纯函数：2.5 系列 4:5 直出，其他 VIP 走 2048x2048 再裁
        assert api_size_for_output("4:5", "gpt-image-2.5-flare-vip") == "2048x2560"
        assert api_size_for_output("4:5", "gpt-image-2-vip") == "2048x2048"
        final_bytes, (width, height) = finalize_output_image(png_bytes(2048, 2048), "4:5")
        assert abs(width / height - 0.8) < 0.002 and final_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        print("  ✓ 4:5 尺寸映射与裁切")

        # 13. Gemini 原生协议的纯函数：端点推导 / 尺寸拆分 / 跳过思考中间图
        assert gemini.native_endpoint(MOCK_BASE, NB_MODEL) == (
            f"{MOCK_BASE}/v1beta/models/{NB_MODEL}:generateContent"), \
            gemini.native_endpoint(MOCK_BASE, NB_MODEL)
        assert gemini.split_size("4:5@2K") == ("4:5", "2K")
        assert gemini.split_size("auto") == ("", "")
        final_png = png_bytes(64, 80)
        thought = png_bytes(32, 32)
        response = {"candidates": [{"content": {"parts": [
            {"thought": True, "inlineData": {
                "mimeType": "image/png", "data": base64.b64encode(thought).decode()}},
            {"inlineData": {"mimeType": "image/png",
                            "data": base64.b64encode(final_png).decode()}},
        ]}}]}
        assert gemini._extract_image(response) == final_png     # 只取最后一张非思考图
        refused = {"candidates": [{"finishReason": "SAFETY", "content": {"parts": [
            {"text": "I can't help with that."}]}}]}
        try:
            gemini._extract_image(refused)
        except Exception as exc:                                # noqa: BLE001
            assert "SAFETY" in str(exc) and "计费" in str(exc), exc
        else:
            raise AssertionError("没有图片时必须报错（且提示可能已计费）")
        print("  ✓ Gemini 端点/尺寸/思考图跳过/拦截报错")
    finally:
        server.shutdown()
        server.server_close()

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
