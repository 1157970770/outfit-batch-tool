#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地假 RunningHub，仅用于离线验证 wan3_r2v.py 的 提交→轮询→下载→上传 全链路。"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
BASE = f"http://127.0.0.1:{PORT}"
PREFIX = "/openapi/v2"
# 前 N 次轮询返回 RUNNING，用来模拟慢任务 / 超时续查
RUNNING_POLLS = int(os.environ.get("MOCK_RUNNING_POLLS", "2"))

state = {"submitted": 0, "polls": 0, "uploads": 0, "last_body": None}
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

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        if self.path == f"{PREFIX}/alibaba/wan-3.0/reference-to-video":
            body = json.loads(raw or b"{}")
            with lock:
                state["submitted"] += 1
                state["last_body"] = body
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or len(auth) < 20:
                return self._json({"code": 401, "msg": "bad token"}, 401)
            return self._json({"taskId": f"mock-{state['submitted']}", "status": "RUNNING",
                               "errorCode": "", "errorMessage": "", "results": None})

        if self.path == f"{PREFIX}/query":
            with lock:
                state["polls"] += 1
                n = state["polls"]
            if n <= RUNNING_POLLS:
                return self._json({"taskId": "mock-1", "status": "RUNNING", "results": None})
            return self._json({
                "taskId": "mock-1", "status": "SUCCESS",
                "results": [{"url": f"{BASE}/file.mp4", "nodeId": "2", "outputType": "mp4", "text": None}],
                "usage": {"consumeMoney": "0.42", "consumeCoins": "42", "taskCostTime": "96"},
            })

        if self.path == f"{PREFIX}/media/upload/binary":
            with lock:
                state["uploads"] += 1
                n = state["uploads"]
                state["upload_bytes"] = len(raw)
                state["upload_ct"] = self.headers.get("Content-Type", "")[:60]
            return self._json({"code": 0, "message": "success",
                               "data": {"type": "file", "download_url": f"{BASE}/uploaded-{n}.mp4",
                                        "fileName": f"openapi/x-{n}.mp4", "size": str(len(raw))}})

        if self.path == "/stats":
            return self._json(state)

        return self._json({"code": 404, "msg": "not found"}, 404)

    def do_GET(self):
        if self.path == "/stats":
            return self._json(state)
        if self.path == "/file.mp4":
            payload = b"FAKE-MP4-" + b"0" * 2048
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        return self._json({"code": 404}, 404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock runninghub on {BASE}", flush=True)
    srv.serve_forever()
