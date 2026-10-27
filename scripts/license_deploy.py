# -*- coding: utf-8 -*-
"""授权服务运维脚本（腾讯云函数 SCF + 函数 URL）。

替代手工点控制台。常用：
    python scripts/license_deploy.py url         # 打印客户端要填的 LICENSE_SERVER
    python scripts/license_deploy.py pack        # 把 server/ 两个文件打成 dist/license-deploy.zip
    python scripts/license_deploy.py deploy      # 打包 + 上传 + 等函数变 Active（函数不存在则创建）
    python scripts/license_deploy.py check CODE  # 验某个密码在线上是否可用（--expect reject 验是否被拒）
    python scripts/license_deploy.py config      # 看运行配置（wan.api_key / 清单）填全了没，线上有没有下发
    python scripts/license_deploy.py test        # 线上验收：激活/复核/换机/错密码

依赖：本机已安装 tccli 并完成登录（tccli auth login --profile agent1）。
不打印任何密钥。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")
ZIP_PATH = os.path.join(ROOT, "dist", "license-deploy.zip")

REGION = os.environ.get("LICENSE_REGION", "ap-guangzhou")
FUNC = os.environ.get("LICENSE_FUNC", "outfit-license")
PROFILE = os.environ.get("LICENSE_PROFILE", "agent1")
TCCLI = os.environ.get("TCCLI") or "tccli"

# tccli 不在 PATH 时，回退到本机隔离环境里的可执行文件
if not __import__("shutil").which(TCCLI):
    for _cand in (r"C:/Users/EDY/.workbuddy/binaries/python/envs/default/Scripts/tccli.exe",):
        if os.path.isfile(_cand):
            TCCLI = _cand
            break

# 部署包必须包含的文件。缺任何一个都直接失败——宁可不部署，也别部署半套。
# config.json 是运行必需（新版靠它下发 百炼 Key / 模板清单 / COS）。
ZIP_FILES = ["license_api.py", "passwords.json", "config.json"]

MISSING_HINT = {
    "config.json": "先复制模板：copy server\\config.example.json server\\config.json，再填 rh_api_key",
}


# ---------------------------------------------------------------- tccli 调用
def tccli(args: list[str]) -> dict:
    cmd = [TCCLI, "--profile", PROFILE] + args
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    i, j = out.find("{"), out.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(out[i:j + 1])
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"tccli 调用失败\nargs={args[:3]}...\nstderr={(p.stderr or '')[:400]}")


# ---------------------------------------------------------------- pack
def pack() -> str:
    os.makedirs(os.path.dirname(ZIP_PATH), exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ZIP_FILES:
            src = os.path.join(SERVER_DIR, name)
            if not os.path.isfile(src):
                hint = MISSING_HINT.get(name, "")
                raise SystemExit(f"缺少文件：{src}" + (f"\n  提示：{hint}" if hint else ""))
            z.write(src, arcname=name)  # 必须放 zip 根目录，不套文件夹
            print(f"[pack] 打入 {name}")
    size = os.path.getsize(ZIP_PATH)
    print(f"[pack] {ZIP_PATH}  ({size} 字节，{len(ZIP_FILES)} 个文件)")
    return ZIP_PATH


# ---------------------------------------------------------------- deploy
def function_status() -> str | None:
    data = tccli(["scf", "ListFunctions", "--region", REGION, "--Limit", "100"])
    for f in data.get("Functions", []):
        if f.get("FunctionName") == FUNC:
            return f.get("Status")
    return None


def wait_active(timeout=60):
    """等函数回到 Active，代码更新才算真正生效。"""
    import time
    time.sleep(2)  # 刚提交时状态可能还是旧的 Active，先让后端反应一下
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st = function_status()
        if st != last:
            print(f"[deploy] 函数状态 {st}")
            last = st
        if st == "Active":
            return True
        if st in ("CreateFailed", "UpdateFailed"):
            print(f"[deploy] ✗ 失败状态 {st}，去控制台看日志")
            return False
        time.sleep(2)
    print(f"[deploy] ⚠ 等 {timeout}s 仍未 Active（当前 {last}），稍后用 check 再验")
    return False


def deploy(runtime="Python3.9", memory=128, timeout=5):
    pack()
    with open(ZIP_PATH, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    status = function_status()
    if status is None:
        import secrets
        new_secret = secrets.token_urlsafe(36)
        print("[deploy] 函数不存在，创建中…")
        tccli(["scf", "CreateFunction", "--region", REGION,
               "--FunctionName", FUNC,
               "--Runtime", runtime,
               "--Handler", "license_api.main_handler",
               "--MemorySize", str(memory),
               "--Timeout", str(timeout),
               "--Role", "SCF_QcsRole",
               "--Description", "outfit-license 授权服务",
               "--Code", json.dumps({"ZipFile": b64}),
               "--Environment", json.dumps({"Variables": [
                   {"Key": "LICENSE_SECRET", "Value": new_secret},
                   {"Key": "LICENSE_TTL_DAYS", "Value": "14"},
                   {"Key": "LICENSE_PASSWORDS", "Value": "/var/user/passwords.json"},
               ]})])
        print("[deploy] 已提交创建，LICENSE_SECRET 已自动生成（48 位，不在此打印）")
        print("[deploy] 去控制台 → 函数配置 → 环境变量 可查看；改它=所有客户端需重新激活")
    else:
        print(f"[deploy] 函数已存在（{status}），更新代码中…")
        tccli(["scf", "UpdateFunctionCode", "--region", REGION,
               "--FunctionName", FUNC, "--ZipFile", b64])
    ok = wait_active()
    print("[deploy] 完成。" if ok else "[deploy] 已提交，状态未确认。")
    print("[deploy] 若触发器未绑，跑一次 `trigger` 子命令。")
    return 0 if ok else 1


# ---------------------------------------------------------------- trigger
def function_url() -> str | None:
    data = tccli(["scf", "ListTriggers", "--region", REGION, "--FunctionName", FUNC])
    for t in data.get("Triggers", []):
        if t.get("Type") == "http":
            desc = t.get("TriggerDesc") or "{}"
            if isinstance(desc, str):
                try:
                    desc = json.loads(desc)
                except json.JSONDecodeError:
                    continue
            url = (desc.get("NetConfig") or {}).get("ExtranetUrl")
            if url:
                return url.rstrip("/")
    return None


def ensure_trigger():
    if function_url():
        print("[trigger] 已存在 HTTP 触发器")
        return
    print("[trigger] 创建函数 URL…")
    tccli(["scf", "CreateTrigger", "--region", REGION,
           "--FunctionName", FUNC, "--TriggerName", "licenseUrl",
           "--Type", "http",
           "--TriggerDesc", json.dumps({
               "authType": "NONE",
               "netConfig": {"enableIntranet": True, "enableExtranet": True},
           })])
    print("[trigger] 完成")


# ---------------------------------------------------------------- test
def _post(base: str, path: str, body: dict, timeout=15):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test():
    base = function_url()
    if not base:
        raise SystemExit("没有 HTTP 触发器，先跑 `trigger`")
    with open(os.path.join(SERVER_DIR, "passwords.json"), encoding="utf-8-sig") as f:
        table = json.load(f).get("passwords", {})
    code = next((k for k, v in table.items() if not v.get("revoked")), None)
    if not code:
        raise SystemExit("密码表里没有可用密码")

    print(f"base = {base}")
    ok = True

    s, d = _post(base, "/health", {})
    print(f"  [{'PASS' if s == 200 and d.get('ok') else 'FAIL'}] /health           -> {s}")
    ok &= s == 200

    s, d = _post(base, "/activate", {"password": code, "machine": "selftest-A"})
    tok = d.get("token", "")
    good = s == 200 and d.get("ok") and len(tok) > 100
    print(f"  [{'PASS' if good else 'FAIL'}] /activate 正确密码 -> {s} token={len(tok)}字符")
    ok &= bool(good)

    s, d = _post(base, "/activate", {"password": "WRONG", "machine": "selftest-A"})
    good = s == 401
    print(f"  [{'PASS' if good else 'FAIL'}] /activate 错误密码 -> {s} {d.get('error')}")
    ok &= good

    s, d = _post(base, "/verify", {"token": tok, "machine": "selftest-A"})
    good = s == 200 and d.get("ok")
    print(f"  [{'PASS' if good else 'FAIL'}] /verify   同机器   -> {s}")
    ok &= bool(good)

    s, d = _post(base, "/verify", {"token": tok, "machine": "selftest-B"})
    good = s == 403
    print(f"  [{'PASS' if good else 'FAIL'}] /verify   换机器   -> {s} {d.get('error')}")
    ok &= good

    print("\n结论:", "全部通过" if ok else "有失败项，看上面 FAIL")
    return 0 if ok else 1


def check(code: str, expect: str = "ok") -> int:
    """验某个密码在「线上」的实际状态，而不是看本地表。"""
    base = function_url()
    if not base:
        raise SystemExit("没有 HTTP 触发器，先跑 `trigger`")
    s, d = _post(base, "/activate", {"password": code, "machine": "selftest-check"})
    err = d.get("error", "") or ""
    live_ok = s == 200 and d.get("ok")
    bound = "另一台机器" in err  # 密码在线上存在，但已锁到别的机器
    if expect == "reject":
        good = not live_ok and not bound
        detail = f"{s} {err}" if err else f"{s}"
        print(f"  [{'PASS' if good else 'FAIL'}] {code} 线上应被拒  -> {detail}")
    else:
        good = bool(live_ok or bound)
        detail = f"{s} token {len(d.get('token', ''))} 字符" if live_ok else f"{s} {err}"
        print(f"  [{'PASS' if good else 'FAIL'}] {code} 线上应可用  -> {detail}")
    return 0 if good else 1


def config_status() -> int:
    """看运行配置填全了没（只报是否已填 / 长度，绝不打印 Key 的值）。"""
    path = os.path.join(SERVER_DIR, "config.json")
    ok = True
    if not os.path.isfile(path):
        print("[config] 缺少 server/config.json")
        return 1
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    key = str(data.get("rh_api_key") or "")
    wan = data.get("wan") or {}
    if not isinstance(wan, dict):
        wan = {}
    wan_key = str(wan.get("api_key") or "")
    wan_model = str(wan.get("model") or "")
    wan_base = str(wan.get("base_url") or "")
    manifest = str(data.get("manifest_url") or "")
    cos = data.get("cos") or {}
    print(f"[config] 本地 wan.api_key : {'已填（长度 ' + str(len(wan_key)) + '）' if wan_key else '未填 ← 新客户端会显示「配置：未下发」'}")
    print(f"[config] 本地 wan         : provider={wan.get('provider') or 'dashscope'} model={wan_model or '未填'} base={wan_base or '未填'}")
    print(f"[config] 本地 rh_api_key   : {'已填（长度 ' + str(len(key)) + '）' if key else '未填（旧客户端不可用；新客户端用 wan）'}")
    print(f"[config] 本地 manifest_url : {manifest or '未填 ← 客户端显示「模板清单：未下发」'}")
    print(f"[config] 本地 cos          : enabled={cos.get('enabled')} bucket={cos.get('bucket')} region={cos.get('region')} prefix={cos.get('prefix')}")
    tpl_b, out_b = (cos.get("template_bucket") or ""), (cos.get("output_bucket") or "")
    if not (tpl_b and out_b):
        print("[config] 本地 双桶          : 缺失 ← 客户端会回退 cos.bucket，成品可能写进模板桶（归档静默失败）")
    elif tpl_b == out_b:
        print(f"[config] 本地 双桶          : 两桶相同（{tpl_b}）← 双桶未生效，确认是否有意为之")
    else:
        print(f"[config] 本地 双桶          : 模板={tpl_b} | 成品={out_b}")
    ok &= bool(wan_key) and bool(wan_model) and bool(wan_base) and bool(manifest) and bool(tpl_b) and bool(out_b)

    base = function_url()
    if not base:
        print("[config] 没有 HTTP 触发器，跳过线上检查")
        return 1
    with open(os.path.join(SERVER_DIR, "passwords.json"), encoding="utf-8-sig") as f:
        table = json.load(f).get("passwords", {})
    code = next((k for k, v in table.items() if not v.get("revoked")), None)
    if not code:
        print("[config] 密码表里没有可用密码，跳过线上检查")
        return 1
    _, d = _post(base, "/activate", {"password": code, "machine": "config-check"})
    live = d.get("config")
    if not d.get("ok") or live is None:
        print("[config] 线上 /activate 没有返回 config ← 服务端还是旧版，或 config.json 没传上去")
        return 1
    lkey = str(live.get("rh_api_key") or "")
    lwan = live.get("wan") or {}
    if not isinstance(lwan, dict):
        lwan = {}
    lwan_key = str(lwan.get("api_key") or "")
    print(f"[config] 线上 wan.api_key : {'已下发（长度 ' + str(len(lwan_key)) + '）' if lwan_key else '未下发（空）'}")
    print(f"[config] 线上 wan         : model={lwan.get('model') or '未下发'} base={lwan.get('base_url') or '未下发'}")
    print(f"[config] 线上 rh_api_key   : {'已下发（长度 ' + str(len(lkey)) + '）' if lkey else '未下发（空）'}")
    print(f"[config] 线上 manifest_url : {live.get('manifest_url') or '未下发（空）'}")
    print(f"[config] 线上多余字段      : {[k for k in live if k.startswith('_')] or '无'}")
    ok &= bool(lwan_key) and bool(lwan.get("model")) and bool(lwan.get("base_url"))
    print("[config] 结论:", "配置完整，客户端可用" if ok else "还缺东西，见上面标 ← 的行")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="授权服务运维")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pack")
    sub.add_parser("deploy")
    sub.add_parser("trigger")
    sub.add_parser("url")
    sub.add_parser("test")
    sub.add_parser("status")
    sub.add_parser("config")
    c = sub.add_parser("check")
    c.add_argument("code")
    c.add_argument("--expect", choices=["ok", "reject"], default="ok")
    args = p.parse_args()

    if args.cmd == "pack":
        pack()
    elif args.cmd == "deploy":
        sys.exit(deploy())
    elif args.cmd == "trigger":
        ensure_trigger()
    elif args.cmd == "url":
        u = function_url()
        print(f"LICENSE_SERVER={u}" if u else "没有 HTTP 触发器")
    elif args.cmd == "status":
        print("函数状态:", function_status())
        print("函数 URL:", function_url())
    elif args.cmd == "check":
        sys.exit(check(args.code, args.expect))
    elif args.cmd == "config":
        sys.exit(config_status())
    elif args.cmd == "test":
        sys.exit(test())


if __name__ == "__main__":
    main()
