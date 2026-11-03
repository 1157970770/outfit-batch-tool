"""桌面快捷方式入口：用工作区里的 uv venv 打开打标器（状态/清单副本都在工作区）。

直接双击或由 打标器.lnk 指向 .tmp\\tag-venv\\Scripts\\pythonw.exe 运行，
不需要在快捷方式里设环境变量，也不弹控制台黑框。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("OUTFIT_TAG_LIBRARY", str(BASE / "_state" / "tagger" / "tags.json"))
# 网页与桌面共用同一份标签真源；没有这个文件时自动退回纯本机模式。
os.environ.setdefault("OUTFIT_TAG_STORE", str(BASE / "web-data" / "template_tags.json"))

from tools.template_tag_manager import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
