#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 PyInstaller 的版本信息资源（VSVersionInfo）。

为什么需要它：
    未签名的 exe 如果再没有任何版本信息（CompanyName / FileDescription / FileVersion），
    在 Windows 眼里就是"来路不明的未知应用"——SmartScreen 更容易直接拦，
    部分杀软也会按可疑样本处理。补上元数据不会让 SmartScreen 消失（那要代码签名），
    但能让文件属性面板显示正常的公司与版本，也少一类误报理由。

用法：
    python3 scripts/make_version_file.py --version 0.9 --out build/version_info.txt
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers},
    prodvers={prodvers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', '{company}'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{name}'),
         StringStruct('LegalCopyright', '{copyright}'),
         StringStruct('OriginalFilename', '{name}.exe'),
         StringStruct('ProductName', '{name}'),
         StringStruct('ProductVersion', '{version}'),
         StringStruct('Comments', '{comments}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def parse_version(version: str) -> tuple[int, int, int, int]:
    parts = [p for p in version.replace("-", ".").split(".") if p.strip()]
    nums: list[int] = []
    for part in parts[:4]:
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums[:4])  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 PyInstaller 版本信息资源")
    ap.add_argument("--version", required=True, help="例如 0.9")
    ap.add_argument("--out", default="build/version_info.txt")
    ap.add_argument("--name", default="换装批量生产器")
    ap.add_argument("--company", default=os.environ.get("APP_COMPANY", "换装工具"))
    ap.add_argument("--description", default="换装批量生产器 · 批量视频换装生成")
    ap.add_argument("--copyright", default="内部使用")
    ap.add_argument("--comments", default="")
    args = ap.parse_args()

    vers = parse_version(args.version)
    text = TEMPLATE.format(
        filevers=vers,
        prodvers=vers,
        company=args.company,
        description=args.description,
        version=args.version,
        name=args.name,
        copyright=args.copyright,
        comments=args.comments,
    )
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[version] 已写入 {out}（{args.name} {args.version} / {args.company}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
