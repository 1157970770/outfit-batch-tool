# -*- coding: utf-8 -*-
"""往打包目录写一份「干净」的 配置.ini（从 app/config.py 的 TEMPLATE 生成）。

为什么不能直接用 dist 里现成那份：那是上次跑 --selftest 留下的，可能已经被人填过
RH_API_KEY / MANIFEST / OUTPUT_ROOT。直接打包等于把开发者自己的 Key 发给客户。

用法：
    python scripts/make_config.py --dir "dist/换装批量生产器"
    python scripts/make_config.py --dir "dist/换装批量生产器" --license-server https://xxx
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


def template() -> str:
    spec = importlib.util.spec_from_file_location("app_config", ROOT / "app" / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TEMPLATE


def _old_has_secret(path: pathlib.Path) -> bool:
    """旧配置里是否填过 RH_API_KEY（只看有没有值，绝不打印值）。"""
    if not path.exists():
        return False
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RH_API_KEY=") and line.partition("=")[2].strip():
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="生成干净的发布用 配置.ini")
    ap.add_argument("--dir", required=True, help="打包目录，如 dist/换装批量生产器")
    ap.add_argument("--license-server", default="", help="填了则写进 LICENSE_SERVER")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "配置.ini"

    # 旧配置若含真实 Key，先备份（否则会被人误以为还能用）
    if _old_has_secret(target):
        bak = out_dir.parent / f"配置.ini.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.move(str(target), str(bak))
            print(f"[config] ⚠ 旧配置里填过 RH_API_KEY，已备份到 {bak}")
        except OSError as exc:
            print(f"[config] ⚠ 旧配置含 Key 但备份失败：{exc}")
    elif target.exists():
        target.unlink()

    text = template()
    if args.license_server:
        text = text.replace("LICENSE_SERVER=\n", f"LICENSE_SERVER={args.license_server}\n")

    target.write_text(text, encoding="utf-8")
    print(f"[config] 已生成干净模板：{target}（{len(text.encode('utf-8'))} 字节，RH_API_KEY 为空）")
    if args.license_server:
        print(f"[config] LICENSE_SERVER={args.license_server}")
    else:
        print("[config] LICENSE_SERVER 为空 → 这个包不校验密码")
    return 0


if __name__ == "__main__":
    sys.exit(main())
