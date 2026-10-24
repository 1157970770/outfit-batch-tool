#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口：PyInstaller 从这里开始分析依赖。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
