#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯云 COS 连通性自检：跑通 写 → 签链接 → 读 → 列举 → 删 五个动作。

先配环境变量（PowerShell 用 $env:XXX="..."，cmd 用 set XXX=...）：

    COS_BUCKET=my-bucket-1250000000
    COS_REGION=ap-guangzhou
    COS_SECRET_ID=AKIDxxxxxxxxxxxxxxxx
    COS_SECRET_KEY=xxxxxxxxxxxxxxxx
    COS_PREFIX=test          # 可选
    COS_DOMAIN=cdn.xxx.com   # 可选

然后：
    pip install cos-python-sdk-v5
    python3 scripts/cos_check.py

全部打 ✓ 说明这份密钥、桶名、地域、权限都对，可以开跑了。
任何一步 ✗ 都会打印 HTTP 状态 / 错误码 / 中文排查提示。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.storage import (  # noqa: E402
    StorageConfig, StorageError, describe_error, object_key, open_storage,
)

PROBE_BODY = b"cos-check " + str(time.time()).encode()


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(空)"
    if len(value) <= keep * 2:
        return value[:keep] + "***"
    return f"{value[:keep]}***{value[-keep:]}"


def step(title: str, fn) -> bool:
    started = time.time()
    try:
        value = fn()
    except StorageError as exc:
        print(f"✗ {title}：{exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - SDK 的异常类型很杂，统一展示
        print(f"✗ {title}：{describe_error(exc)}")
        return False
    cost = (time.time() - started) * 1000
    print(f"✓ {title}（{cost:.0f} ms）{'' if value is None else '：' + str(value)[:120]}")
    return True


def main() -> int:
    try:
        # 与桌面版一致：环境变量 → ~/.cos/credentials.json → ~/.tccli/<profile>.credential
        config = StorageConfig.from_local_profile()
        config.validate()
    except StorageError as exc:
        print(f"✗ 配置不完整：{exc}")
        print("\n三种来源任选：环境变量 COS_* / ~/.cos/credentials.json / tccli 登录后的 ~/.tccli/*.credential")
        return 2

    print("配置：")
    print(f"  存储桶   {config.bucket}")
    print(f"  地域     {config.region}")
    print(f"  SecretId {mask(config.secret_id)}")
    print(f"  前缀     {config.clean_prefix() or '(根目录)'}")
    print(f"  CDN      {config.domain or '(未配置，用源站域名)'}")
    print()

    try:
        storage = open_storage(config)
    except StorageError as exc:
        print(f"✗ 初始化失败：{exc}")
        return 2
    assert storage is not None

    key = object_key("_healthcheck", "ping.txt", prefix=config.clean_prefix())
    ok = True
    ok &= step("写入对象（put）",
               lambda: storage.put_bytes(PROBE_BODY, key, content_type="text/plain"))
    ok &= step("生成签名链接（sign）", lambda: storage.sign_url(key, 3600))
    ok &= step("读取对象信息（head）", lambda: storage.stat(key))
    ok &= step("对象存在（exists）", lambda: storage.exists(key))
    ok &= step("列举前缀（list）", lambda: len(storage.list_prefix(key.rsplit("/", 1)[0], 10)))
    ok &= step("删除对象（delete）", lambda: storage.delete(key) or "已删除")

    print()
    if ok:
        print("全部通过。提醒三件事：")
        print("  1) 桶保持「私有读写」，访问一律走签名链接；")
        print("  2) 生命周期加一条：未完成的分片上传 3 天清理；")
        print("  3) 别把 SecretKey 提交进 Git，用环境变量或本机 config.json。")
        return 0
    print("有步骤失败。常见原因：桶名漏了 APPID、地域写错、子账号没有该桶权限、本机时钟偏差。")
    print("详细对照见 docs/cos-setup.md 的「报错对照表」。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
