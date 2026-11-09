# -*- coding: utf-8 -*-
"""生图（老张 / laozhang.ai）子包：桌面工具的服务层移植 + 网页端任务管理器。

依赖约定：只用标准库 + Pillow（与仓库 requirements.txt 一致，不引入 requests）。
"""

from .manager import ImageGenError, ImageGenManager

__all__ = ["ImageGenManager", "ImageGenError"]
