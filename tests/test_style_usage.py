#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""款号-视频模板记账本（style_usage.py）单元测试。

覆盖：
1. record → used 往返；同款号重复提交覆盖为最近一次；
2. 空 top / 空 template_ids 直接忽略；
3. 落盘持久化：新实例从磁盘读回；
4. 不同款号互不串账。
"""
import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpdir  # noqa: E402
from app.web.style_usage import StyleUsageLedger


def main() -> int:
    # 不用 tempfile.TemporaryDirectory：它在 Windows 上以 0o700 建目录（显式 DACL），
    # 沙箱/受限账户下清理时会 PermissionError；_tmpdir 用 0o777 + atexit 清理。
    with contextlib.nullcontext(_tmpdir.tempdir("styleusage-")) as tmp:
        data_dir = Path(tmp)

        ledger = StyleUsageLedger(data_dir)
        ledger.record("AX111", ["T001", "T002"], batch_no="B-01", batch_id="b1")
        used = ledger.used("AX111")
        assert set(used) == {"T001", "T002"}, used
        assert used["T001"]["batch_no"] == "B-01" and used["T001"]["batch_id"] == "b1", used

        # 重复提交同款号同模板：覆盖为最近一次（保留 batch_no 更新）
        time.sleep(0.01)
        ledger.record("AX111", ["T001"], batch_no="B-02", batch_id="b2")
        used = ledger.used("AX111")
        assert used["T001"]["batch_no"] == "B-02" and used["T001"]["batch_id"] == "b2", used
        assert used["T002"]["batch_no"] == "B-01", used            # 未重复提交的不动

        # 空 top / 空 ids：忽略，不建空账
        ledger.record("", ["T009"], batch_no="B-03")
        ledger.record("AX222", [], batch_no="B-03")
        assert ledger.used("") == {} and ledger.used("AX222") == {}

        # 款号之间互不串账
        ledger.record("BX111", ["T001"], batch_no="B-04", batch_id="b4")
        assert set(ledger.used("BX111")) == {"T001"}
        assert set(ledger.used("AX111")) == {"T001", "T002"}

        # 持久化：新实例（模拟重启）从磁盘读回
        again = StyleUsageLedger(data_dir)
        assert again.used("AX111")["T001"]["batch_no"] == "B-02"
        assert set(again.used("BX111")) == {"T001"}

        # 坏文件不炸：写一份垃圾内容后加载应为空账
        (data_dir / "style_template_usage.json").write_text("not json", encoding="utf-8")
        broken = StyleUsageLedger(data_dir)
        assert broken.used("AX111") == {}
        # 且后续写入能自愈
        broken.record("CX111", ["T001"], batch_no="B-05", batch_id="b5")
        assert StyleUsageLedger(data_dir).used("CX111")["T001"]["batch_no"] == "B-05"

    print("OK 款号-模板记账本：记账/覆盖/忽略空值/隔离/持久化/坏文件自愈")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
