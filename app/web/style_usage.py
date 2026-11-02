"""款号-视频模板记账本（v0.11.0）。

需求：同一个款号跨批次生成时不重复使用视频模板（软拦截：界面标记 + 可强制）。
- 记账时机：**批次生产提交时**（`BatchManager._start_run`）；小批量试跑不计入。
- 记账键：上衣款号（tags.top，与 COS 归档路径同源；老批次回退 style_no）。
- 存储：`data_dir/style_template_usage.json`，原子写，重启不丢。

文件结构::

    {
      "AX111": {
        "T001": {"batch_no": "2026-11-秋装-01", "batch_id": "b_xxx", "at": 1730540000.0}
      }
    }

同款号同模板重复提交时覆盖旧记录（保留最近一次），保证查询语义是
「这个款号最近一次用这个模板是哪一批」。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class StyleUsageLedger:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "style_template_usage.json"
        self.lock = threading.RLock()
        self.data: dict[str, dict[str, dict]] = self._load()

    # ------------------------------------------------------------------ 存储
    def _load(self) -> dict[str, dict[str, dict]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ 读写
    def record(self, top: str, template_ids, batch_no: str = "", batch_id: str = "") -> None:
        """把一次批次生产用到的模板记到款号名下。top 为空直接忽略。"""
        top = str(top or "").strip()
        ids = [str(value).strip() for value in (template_ids or []) if str(value).strip()]
        if not top or not ids:
            return
        now = time.time()
        with self.lock:
            styles = self.data.setdefault(top, {})
            for template_id in ids:
                styles[template_id] = {
                    "batch_no": str(batch_no or ""),
                    "batch_id": str(batch_id or ""),
                    "at": now,
                }
            self._save_locked()

    def used(self, top: str) -> dict[str, dict]:
        """查某款号已用过的模板：{template_id: {batch_no, batch_id, at}}。top 为空返回空表。"""
        top = str(top or "").strip()
        if not top:
            return {}
        with self.lock:
            styles = self.data.get(top)
            return dict(styles) if isinstance(styles, dict) else {}
