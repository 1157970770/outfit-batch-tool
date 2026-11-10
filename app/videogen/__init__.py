# -*- coding: utf-8 -*-
"""自由视频（通用 wan3.0 生产）子包。

与「小批量试跑 / 大批量生产」完全独立：不读模板清单、不打标签、不写
template_tags，也不参与 job 排队；提交的是用户自己写的提示词与参考素材
（参考图 / 参考视频 / 参考音频），产物落 `web-data/videogen/`。
"""

from .manager import VideoGenManager
from .models import VideoGenError

__all__ = ["VideoGenManager", "VideoGenError"]
