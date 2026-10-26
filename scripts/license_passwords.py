# -*- coding: utf-8 -*-
"""密码表增删改，改完自动同步到云函数并验证线上是否生效。

默认行为：**一条命令搞定「改本地表 → 上传云函数 → 验证线上」**，不需要收尾再跑一条。
    python scripts/license_passwords.py list
    python scripts/license_passwords.py add --note "张三 / 设计部" --expires 2027-12-31
    python scripts/license_passwords.py revoke   RH-2026-001-xxxx
    python scripts/license_passwords.py unrevoke RH-2026-001-xxxx
    python scripts/license_passwords.py expire   RH-2026-001-xxxx 2026-12-31
    python scripts/license_passwords.py remove   RH-2026-003-xxxx
    python scripts/license_passwords.py deploy              # 只部署，不改表

加 --no-deploy 只改本地、不推云（没网 / 只想先存着）：
    python scripts/license_passwords.py add --no-deploy --note "测试"

同步原理：SCF 的代码目录 /var/user 只读，改不了单个文件，所以「同步」= 把
license_api.py + passwords.json 重新打成一个包整体上传，等函数回到 Active 才生效
（约 5~15 秒），随后用 /activate 真打一次，确认线上确实认得这个密码。
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import secrets
import shutil
import subprocess
import sys
import time


def _detect_root() -> str:
    """项目根。源码运行=脚本的上一级；打包成 exe 后=exe 附近能找到 server/ 的目录。"""
    env = os.environ.get("RH_PROJECT_ROOT", "").strip()
    if env and os.path.isdir(os.path.join(env, "server")):
        return env
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    here = os.path.dirname(os.path.abspath(sys.executable))
    for cand in (here, os.path.dirname(here), r"D:\批量视频任务设计区"):
        if os.path.isdir(os.path.join(cand, "server")):
            return cand
    return here


ROOT = _detect_root()
DEFAULT_FILE = os.path.join(ROOT, "server", "passwords.json")
DEPLOY_SCRIPT = os.path.join(ROOT, "scripts", "license_deploy.py")


def python_exe() -> str:
    """真正用来跑部署脚本的 Python。

    打包后 sys.executable 是 exe 自己，拿它去跑 license_deploy.py 只会把界面再开一次，
    所以要另找一个 Python（可用环境变量 RH_PYTHON 指定）。
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    cands = [os.environ.get("RH_PYTHON", "").strip()]
    for name in ("python3", "py", "python"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    for cand in cands:
        if not cand or "WindowsApps" in cand:  # 跳过微软商店的假 python
            continue
        if os.path.isfile(cand) or shutil.which(cand):
            return cand
    raise RuntimeError("找不到可用的 Python，无法部署云函数；请设置环境变量 RH_PYTHON")

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉 0/O/1/l/I
DEFAULT_DAYS = 31  # 新密码默认有效期
NEVER_WORDS = ("never", "none", "-", "永久", "无限")


def default_expires(days: int = DEFAULT_DAYS) -> str:
    """默认到期时间：今天 + 31 天，且保留到当天 23:59（只写日期会按 00:00 失效）。"""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400)) + " 23:59"


def load(path: str) -> dict:
    if not os.path.isfile(path):
        return {"_说明": "密码表", "passwords": {}}
    with open(path, encoding="utf-8-sig") as f:
        doc = json.load(f)
    doc.setdefault("passwords", {})
    return doc


def save(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)  # 原子写，避免写一半把表弄坏


def next_code(doc: dict) -> str:
    year = time.strftime("%Y")
    used = 0
    for code in doc["passwords"]:
        parts = code.split("-")
        if len(parts) >= 3 and parts[1] == year and parts[2].isdigit():
            used = max(used, int(parts[2]))
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(4))
    return f"RH-{year}-{used + 1:03d}-{suffix}"


def show(doc: dict) -> None:
    rows = doc["passwords"]
    if not rows:
        print("（空表）")
        return
    print(f"{'密码':<22}{'状态':<8}{'到期':<18}备注")
    print("-" * 74)
    for code, v in rows.items():
        if v.get("revoked"):
            state = "已停用"
        else:
            dl = _deadline(v.get("expires"))
            state = "已到期" if (dl is not None and dl < time.time()) else "可用"
        print(f"{code:<22}{state:<8}{(v.get('expires') or '永久'):<18}{v.get('note') or ''}")
    print(f"-- 共 {len(rows)} 条")


# ------------------------------------------------------------------ 自动同步
def _deadline(value) -> float | None:
    """把表里的 expires 换算成时间戳，与服务端同规则：按北京时间解析，只写日期=当天 00:00。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = time.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
        return calendar.timegm(dt) - 8 * 3600
    return None


def _sync(code: str | None, expect: str) -> bool:
    """上传云函数 + 验证线上。expect: ok=该密码应可用, reject=应被拒。"""
    print("\n[同步] 上传到云函数（约 5~15 秒）…")
    try:
        py = python_exe()
    except RuntimeError as exc:
        print(f"[同步] ✗ {exc}")
        return False
    rc = subprocess.call([py, DEPLOY_SCRIPT, "deploy"])
    if rc != 0:
        print("[同步] ✗ 上传没成功，云端还是旧表。本地已保存，可重跑：")
        print("       python scripts/license_passwords.py deploy")
        return False
    if not code:
        return True

    print("[同步] 验证线上是否生效…")
    rc = subprocess.call([python_exe(), DEPLOY_SCRIPT, "check", code, "--expect", expect])
    if rc == 0:
        print("[同步] ✓ 线上已生效")
        return True
    print("[同步] ⚠ 线上验证没通过（可能还在切流量，或密码表内容不对）。稍后重跑：")
    print(f"       python scripts/license_deploy.py check {code} --expect {expect}")
    return False


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # 让本脚本输出与子进程输出按顺序显示
    except Exception:
        pass
    argv = sys.argv[1:]
    no_deploy = "--no-deploy" in argv
    if no_deploy:
        argv = [a for a in argv if a != "--no-deploy"]

    p = argparse.ArgumentParser(description="密码表增删改（默认自动同步云函数）")
    p.add_argument("--file", default=DEFAULT_FILE, help="默认 server/passwords.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    a = sub.add_parser("add")
    a.add_argument("--note", default="")
    a.add_argument("--expires", default=None,
                   help=f"默认 {DEFAULT_DAYS} 天后 23:59 失效；写 never 表示永久")
    a.add_argument("--machine", default="")
    a.add_argument("--code", default=None, help="不填则自动生成")

    for name in ("revoke", "unrevoke", "remove"):
        q = sub.add_parser(name)
        q.add_argument("code")

    e = sub.add_parser("expire", help="改进到期日；date 写 never/永久 表示不限")
    e.add_argument("code")
    e.add_argument("date", help="2027-12-31 或 '2027-12-31 23:59' 或 never")

    sub.add_parser("deploy")

    args = p.parse_args(argv)

    if args.cmd == "deploy":
        sys.exit(subprocess.call([python_exe(), DEPLOY_SCRIPT, "deploy"]))

    path = args.file
    doc = load(path)
    rows = doc["passwords"]

    if args.cmd == "list":
        show(doc)
        return

    if args.cmd == "add":
        code = args.code or next_code(doc)
        if code in rows:
            sys.exit(f"密码已存在：{code}")
        expires = (args.expires or default_expires()).strip()
        if expires.lower() in NEVER_WORDS:
            expires = ""
        rows[code] = {"note": args.note, "machine": args.machine,
                      "expires": expires, "revoked": False}
        save(path, doc)
        print(f"已新增：{code}")
        print(f"  备注={args.note or '-'}  到期={expires or '永久'}")
        show(doc)
        if no_deploy:
            print("\n[同步] 已跳过（--no-deploy）。云端暂时不认这个密码。")
            print("       要推上去：python scripts/license_passwords.py deploy")
        else:
            _sync(code, "ok")
        return

    if args.cmd not in ("revoke", "unrevoke", "expire", "remove"):
        return

    code = args.code
    if code not in rows:
        near = [c for c in rows if code.lower() in c.lower()]
        msg = f"没有这个密码：{code}"
        if near:
            msg += "\n你是不是想说：\n  " + "\n  ".join(near)
        sys.exit(msg)

    if args.cmd == "remove":
        del rows[code]
        print(f"已删除：{code}（该用户下次启动会被拒）")
    elif args.cmd == "revoke":
        rows[code]["revoked"] = True
        print(f"已停用：{code}")
    elif args.cmd == "unrevoke":
        rows[code]["revoked"] = False
        print(f"已恢复：{code}")
    elif args.cmd == "expire":
        raw = args.date.strip()
        if raw.lower() in NEVER_WORDS or raw == "":
            rows[code]["expires"] = ""
            print(f"已改为永久：{code}（不再到期）")
        else:
            rows[code]["expires"] = raw
            print(f"已改到期：{code} -> {raw}")
            if ":" not in raw:
                print("   注意：只写日期时，服务端按**当天 00:00** 判定失效（即最后有效时刻是前一天 23:59）。")
                print(f"   想有效期到当天结束，请改写成：{raw} 23:59")

    save(path, doc)
    show(doc)

    if no_deploy:
        print("\n[同步] 已跳过（--no-deploy）。云端仍是旧的。")
        print("       要推上去：python scripts/license_passwords.py deploy")
        return

    # 停用 / 删除 → 线上应被拒；恢复 / 改期 → 线上应可用；
    # 但如果改成一个已过去的日期，线上本来就该拒，用 reject 验才对
    if args.cmd in ("revoke", "remove"):
        expect = "reject"
    elif args.cmd == "expire":
        dl = _deadline(rows[code].get("expires"))
        expect = "reject" if (dl is not None and dl < time.time()) else "ok"
    else:
        expect = "ok"
    _sync(code, expect)


if __name__ == "__main__":
    main()
