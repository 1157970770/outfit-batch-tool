#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产数据的只读镜像（SQLite 派生库）。

JSON 是唯一写入真源，本包把它同步成一份结构稳定的 SQLite 库，供外部程序查询：
AI / LLM agent、Excel、Power BI、报表脚本都连这份库，而不是各自去解析 JSON。

入口：

* 服务内：`app/web/server.py` 启动时调用 `start_mirror(settings)`（配置见
  `server/web.json` 的 `data_mirror` 段），同步线程每 5 秒增量更新一次；
* 命令行：`python -m app.mirror --verify`（详见 `app/mirror/__main__.py`）；
* 查询示例：`scripts/query_production.py`。

文档：`docs/production-data.md`。
"""
from __future__ import annotations

from app.mirror.core import Mirror, start_mirror

__all__ = ["Mirror", "start_mirror"]
