# -*- coding: utf-8 -*-
"""兼容入口：真正的实现在 password_manager.py。

保留这个中文名文件，是为了老的快捷方式 / 命令行还能用：
    python scripts/密码管理器.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from password_manager import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
