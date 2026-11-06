#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两区搬家后历史数据路径重锚定（离线，不联网、不花钱）。

覆盖 2026-11-06 的故障：生产从「设计区」搬到「运行区」后，jobs / batches /
uploads meta 里持久化的旧绝对路径悬空，视频和图片下载 404。修复做法是读数据时
按当前 data_dir 重锚，不改任何历史 JSON。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from app.web.jobs import JobManager, image_slots  # noqa: E402
from app.web.paths import relocate  # noqa: E402


class RelocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir(prefix="t-reloc-")
        # 模拟「设计区 → 运行区」：两个不同根，各有自己的 web-data。
        self.old_root = self.tmp / "设计区" / "web-data"
        self.new_root = self.tmp / "运行区" / "web-data"
        (self.new_root / "output" / "b1").mkdir(parents=True)
        (self.new_root / "uploads" / "ab").mkdir(parents=True)
        self.video = self.new_root / "output" / "b1" / "044.mp4"
        self.video.write_bytes(b"fake-mp4")
        self.image = self.new_root / "uploads" / "ab" / ("a" * 32 + ".png")
        self.image.write_bytes(b"fake-png")

    def stale(self, rel: str) -> Path:
        return self.old_root / rel

    def test_stale_absolute_path_is_relocated(self):
        got = relocate(self.stale("output/b1/044.mp4"), self.new_root)
        self.assertEqual(got, self.video)
        self.assertTrue(got.is_file())

    def test_current_root_is_untouched(self):
        p = self.video
        self.assertEqual(relocate(p, self.new_root), p)

    def test_relative_path_is_untouched(self):
        self.assertEqual(relocate("output/b1/044.mp4", self.new_root),
                         Path("output/b1/044.mp4"))

    def test_empty_is_untouched(self):
        self.assertEqual(relocate("", self.new_root), Path())
        self.assertEqual(relocate(None, self.new_root), Path())

    def test_foreign_absolute_path_is_untouched(self):
        foreign = self.tmp / "somewhere-else" / "044.mp4"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_bytes(b"x")
        self.assertEqual(relocate(foreign, self.new_root), foreign)

    def test_missing_target_keeps_original_when_reading(self):
        stale = self.stale("output/b1/not-there.mp4")
        self.assertEqual(relocate(stale, self.new_root), stale)
        self.assertEqual(relocate(stale, self.new_root, must_exist=False),
                         self.new_root / "output" / "b1" / "not-there.mp4")

    def test_snapshot_dir_name_also_matches(self):
        snap = (self.tmp / "设计区" / "web-data-生产快照-20261106"
                / "output" / "b1" / "044.mp4")
        self.assertEqual(relocate(snap, self.new_root), self.video)

    def test_image_slots_relocates_when_data_dir_given(self):
        stale_img = self.stale("uploads/ab/" + "a" * 32 + ".png")
        record = {"images": [{"upload_id": "a" * 32, "name": "定妆图.png",
                              "path": str(stale_img)}]}
        fixed = image_slots(record, self.new_root)
        self.assertEqual(fixed[0]["path"], str(self.image))
        # 不传 data_dir → 完全保持旧行为（public() / 历史对比层不受影响）。
        self.assertEqual(image_slots(record)[0]["path"], str(stale_img))

    def test_image_slots_fallback_single_image_path(self):
        stale_img = self.stale("uploads/ab/" + "a" * 32 + ".png")
        record = {"image_path": str(stale_img), "image_name": "定妆图.png"}
        fixed = image_slots(record, self.new_root)
        self.assertEqual(len(fixed), 1)
        self.assertEqual(fixed[0]["path"], str(self.image))
        self.assertEqual(fixed[0]["upload_id"], "a" * 32)

    def test_job_manager_output_path_relocates(self):
        jm = JobManager.__new__(JobManager)
        jm.lock = threading.RLock()
        jm.jobs = {"j1": {"out_dir": str(self.stale("output/b1"))}}
        jm.root = self.new_root
        self.assertEqual(jm.output_path("j1"), self.new_root / "output" / "b1")
        self.assertIsNone(jm.output_path("missing"))


def main() -> int:
    result = unittest.main(verbosity=2, exit=False).result
    ok = result.wasSuccessful()
    print("OK 路径重锚定通过" if ok else "FAIL 路径重锚定")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
