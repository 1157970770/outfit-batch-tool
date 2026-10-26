# -*- coding: utf-8 -*-
"""测试专用临时目录（不要直接用 tempfile.mkdtemp）。

为什么不用 `tempfile.mkdtemp`：
    在 Windows 上它以 0o700 建目录，会写入**显式 DACL**，从而剥掉从父目录继承来的
    权限。受限账户 / 受控沙箱（例如公司的 DSH 沙箱、AppContainer 类环境）下，进程
    随后往这个目录里写文件就会 `PermissionError: [Errno 13]`——测试还没跑就挂了。

做法：
    用 0o777 建目录（保留继承 ACE），进程退出时 atexit 统一清理。
    `tests/test_storage.py` 早就手工规避了这个坑，这里是它的通用版。
"""
from __future__ import annotations

import atexit
import os
import secrets
import shutil
import tempfile
from pathlib import Path

_created: list[Path] = []


def tempdir(prefix: str = "t-") -> Path:
    """在系统临时目录下建一个**可写**的临时目录并返回。"""
    base = Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)  # 父目录若不存在，用默认权限建（可写）
    for _ in range(50):
        path = base / (prefix + secrets.token_hex(6))
        try:
            os.mkdir(path, 0o777)  # 关键：0o777，不要 0o700
        except FileExistsError:
            continue
        _created.append(path)
        return path
    raise RuntimeError(f"在 {base} 下建不出临时目录")


@atexit.register
def _cleanup() -> None:
    for path in _created:
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    d = tempdir("probe-")
    (d / "x.txt").write_text("ok", encoding="utf-8")
    print("可写临时目录:", d, "->", (d / "x.txt").read_text(encoding="utf-8"))
