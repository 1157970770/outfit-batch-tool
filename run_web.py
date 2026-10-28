#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换装批量生产器局域网网页服务入口。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.web.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
