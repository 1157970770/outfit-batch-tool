#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地假阿里云百炼（万相3.0），仅用于离线验证官方协议链路。

    python3 tests/mock_dashscope.py 18090
    WAN_BASE_URL=http://127.0.0.1:18090/api/v1 ...
    WAN_API_KEY=demo-key

环境变量：
  MOCK_RUNNING_POLLS  前 N 次查询返回 RUNNING（默认 2）
  MOCK_TASK_MODE      success（默认）/ failed / unknown / canceled
  MOCK_FAIL_SUBMIT    401 / 400 / 429，模拟提交失败
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18090
BASE = f"http://127.0.0.1:{PORT}"
API = "/api/v1"
RUNNING_POLLS = int(os.environ.get("MOCK_RUNNING_POLLS", "2"))
TASK_MODE = os.environ.get("MOCK_TASK_MODE", "success").lower()
FAIL_SUBMIT = os.environ.get("MOCK_FAIL_SUBMIT", "").strip()

state = {
    "submitted": 0,
    "polls": 0,
    "last_body": None,
    "last_headers": {},
    "last_query": None,
}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bearer(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        if self.path == f"{API}/services/aigc/video-generation/video-synthesis":
            body = json.loads(raw or b"{}")
            with lock:
                state["submitted"] += 1
                state["last_body"] = body
                state["last_headers"] = {
                    "Authorization": self.headers.get("Authorization", ""),
                    "X-DashScope-Async": self.headers.get("X-DashScope-Async", ""),
                    "Content-Type": self.headers.get("Content-Type", ""),
                }
                n = state["submitted"]

            if FAIL_SUBMIT == "401":
                return self._json({"code": "InvalidApiKey", "message": "No API-key provided.",
                                   "request_id": "req-auth-fail"}, 401)
            if FAIL_SUBMIT == "429":
                return self._json({"code": "Throttling.BurstRate",
                                   "message": "Request rate increased too quickly.",
                                   "request_id": "req-throttle"}, 429)
            if FAIL_SUBMIT == "400":
                return self._json({"code": "InvalidParameter", "message": "duration invalid",
                                   "request_id": "req-bad-param"}, 400)

            if not self._bearer():
                return self._json({"code": "InvalidApiKey", "message": "No API-key provided.",
                                   "request_id": "req-no-key"}, 401)
            if self.headers.get("X-DashScope-Async") != "enable":
                return self._json({
                    "code": "InvalidParameter",
                    "message": "current user api does not support synchronous calls",
                    "request_id": "req-no-async",
                }, 400)
            if not isinstance(body.get("input"), dict) or not body.get("model"):
                return self._json({"code": "InvalidParameter", "message": "input/model missing",
                                   "request_id": "req-missing"}, 400)

            task_id = f"task-{n}"
            if TASK_MODE == "failed":
                task_id = "task-fail"
            elif TASK_MODE == "unknown":
                task_id = "task-unknown"
            elif TASK_MODE == "canceled":
                task_id = "task-canceled"
            return self._json({
                "output": {"task_id": task_id, "task_status": "PENDING"},
                "request_id": f"req-submit-{n}",
            })

        if self.path == "/stats":
            return self._json(state)
        return self._json({"code": "NotFound", "message": "not found"}, 404)

    def do_GET(self):
        if self.path == "/stats":
            return self._json(state)
        if self.path == "/file.mp4":
            payload = b"FAKE-MP4-" + b"0" * (120 * 1024)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith(f"{API}/tasks/"):
            task_id = self.path.rsplit("/", 1)[-1]
            with lock:
                state["polls"] += 1
                state["last_query"] = task_id
                n = state["polls"]

            if task_id == "task-unknown":
                return self._json({"output": {"task_id": task_id, "task_status": "UNKNOWN"},
                                   "request_id": "req-unknown"})
            if task_id == "task-canceled":
                return self._json({"output": {"task_id": task_id, "task_status": "CANCELED"},
                                   "request_id": "req-canceled"})
            if task_id == "task-fail":
                return self._json({
                    "output": {"task_id": task_id, "task_status": "FAILED",
                               "code": "DataInspectionFailed",
                               "message": "Input data may contain inappropriate content."},
                    "request_id": "req-failed",
                })
            if n <= RUNNING_POLLS:
                return self._json({"output": {"task_id": task_id, "task_status": "RUNNING"},
                                   "request_id": f"req-poll-{n}"})

            return self._json({
                "request_id": "req-success",
                "output": {
                    "task_id": task_id,
                    "task_status": "SUCCEEDED",
                    "submit_time": "2026-10-27 18:00:00.000",
                    "end_time": "2026-10-27 18:03:00.000",
                    "video_url": f"{BASE}/file.mp4",
                },
                "usage": {
                    "video_count": 1,
                    "duration": 14.0,
                    "input_video_duration": 7.0,
                    "output_video_duration": 7.0,
                    "fps": 30,
                    "SR": 720,
                    "ratio": "9:16",
                },
            })
        return self._json({"code": "NotFound", "message": "not found"}, 404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock dashscope on {BASE} ({API})", flush=True)
    srv.serve_forever()
