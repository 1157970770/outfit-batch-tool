#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""授权客户端：密码 → token → 本机缓存。

规则（刻意做得简单）：
  · 没配 `license_server` 就完全放行（不影响本地开发和你自己用）；
  · 配了就必须有有效 token 才能进主界面；
  · token 绑定机器码，拷到别的电脑上无效；
  · 启动时联网校验一次：服务端说停用 / 到期就立刻要求重输；
  · 断网不会立刻锁死——只要上次成功核验在 GRACE_DAYS 天内就放行，
    超过这个窗口就要求联网核一次（防"拔网线躲吊销"）。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home()) / "RHOutfitTool"
CACHE_PATH = CONFIG_DIR / "license.json"

# 断网救援窗口：软件反正要联网才能干活，这个只用来挡住服务端抖动导致的误锁
GRACE_DAYS = 7


# ---------------------------------------------------------------------------
# 缓存位置：%APPDATA% 可能在受控机器上不可写，按顺序找第一个能用的
# ---------------------------------------------------------------------------

def _cache_candidates() -> list[Path]:
    """候选缓存目录，按优先级排列。"""
    cands: list[Path] = []
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        base = os.getenv(env_name)
        if base:
            cands.append(Path(base) / "RHOutfitTool")
    cands.append(CONFIG_DIR)
    try:
        cands.append(Path(os.path.dirname(os.path.abspath(sys.executable))) / "_授权")
    except Exception:  # noqa: BLE001
        pass
    cands.append(Path(tempfile.gettempdir()) / "RHOutfitTool")

    seen: set[str] = set()
    unique: list[Path] = []
    for path in cands:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def cache_path() -> Path:
    """返回可写的缓存文件路径；已有缓存的优先复用它。"""
    candidates = _cache_candidates()
    for folder in candidates:
        target = folder / "license.json"
        if target.exists():
            return target
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".writable"
            probe.write_text("1", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return folder / "license.json"
        except OSError:
            continue
    # 全部不可写也返回第一个，让上层报出明确错误
    return candidates[0] / "license.json"


def machine_id() -> str:
    """稳定但不泄露硬件信息的机器指纹。"""
    parts: list[str] = []
    try:
        import winreg  # 仅 Windows 有，取 MachineGuid 最稳

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            parts.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
    except Exception:  # noqa: BLE001
        parts.append(str(uuid.getnode()))
    parts.append(socket.gethostname())
    raw = "|".join(parts).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


def _post(server: str, path: str, payload: dict, timeout: int = 8) -> tuple[int, dict]:
    url = server.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from None


def _call(server: str, action: str, payload: dict, timeout: int = 8) -> dict:
    """兼容两种 API 网关配法：

    · 单路由（资源就是 /license）：直接 POST 配置地址，动作放 body.action；
    · 多路由（资源是 /license/activate、/license/verify）：先试上面那种，
      网关会回 404，再补上 /action 重试。
    """
    body = {**payload, "action": action}
    status, result = _post(server, "", body, timeout)
    if status == 404 or (not result.get("ok") and "未知接口" in str(result.get("error", ""))):
        status, result = _post(server, f"/{action}", body, timeout)
    return result


def load_cache() -> dict:
    for folder in _cache_candidates():
        target = folder / "license.json"
        if not target.exists():
            continue
        try:
            return json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def save_cache(data: dict) -> None:
    """写缓存；某个目录不可写就换下一个，全都不行才抛错（带可操作提示）。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    errors: list[str] = []
    for folder in _cache_candidates():
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "license.json").write_text(payload, encoding="utf-8")
            return
        except OSError as exc:
            errors.append(f"{folder}: {exc.strerror or exc}")
            continue
    raise PermissionError(
        "无法保存授权缓存（这几处都写不进去）：\n  "
        + "\n  ".join(errors[:3])
        + "\n\n请把软件解压到可写目录（如 D:\\工具\\），或以管理员身份运行一次。"
    )


def clear_cache() -> None:
    for folder in _cache_candidates():
        try:
            (folder / "license.json").unlink(missing_ok=True)
        except OSError:
            continue


def activate(server: str, password: str) -> dict:
    """用密码换 token，成功即写入本机缓存。"""
    machine = machine_id()
    result = _call(server, "activate", {"password": password.strip(), "machine": machine})
    if not result.get("ok"):
        raise ValueError(result.get("error") or "激活失败")
    save_cache({
        "server": server.rstrip("/"),
        "machine": machine,
        "token": result["token"],
        "expire_at": result.get("expire_at"),
        "note": result.get("note", ""),
        "activated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_ok": time.time(),
    })
    return result


def status(server: str) -> tuple[bool, str]:
    """(能否使用, 说明)。不改缓存以外的任何东西。"""
    cache = load_cache()
    if not cache.get("token") or cache.get("server", "").rstrip("/") != server.rstrip("/"):
        return False, "本机还没有授权，请输入分配的密码"

    expire_at = int(cache.get("expire_at") or 0)
    if expire_at and expire_at < time.time():
        clear_cache()
        return False, "授权已到期，请联系管理员续期"

    # 联网复核：服务端说到期/停用就立刻失效
    try:
        result = _call(server, "verify",
                       {"token": cache["token"], "machine": machine_id()}, timeout=6)
    except ConnectionError:
        # 服务端连不上：看上次成功核验是什么时候，窗口内放行
        last_ok = float(cache.get("last_ok") or 0)
        waited = time.time() - last_ok
        if last_ok and waited <= GRACE_DAYS * 86400:
            left = max(0, int((GRACE_DAYS * 86400 - waited) / 86400))
            return True, f"当前离线，按上次核验放行（还剩约 {left} 天）"
        return False, f"需要联网核验一次（已超过 {GRACE_DAYS} 天没能连上授权服务器）"

    if result.get("ok"):
        # 管理员续期后服务端会顺手换新票，这里直接收下，用户不用重输密码
        if result.get("renewed_token"):
            cache["token"] = result["renewed_token"]
            if result.get("expire_at"):
                cache["expire_at"] = result["expire_at"]
        cache["last_ok"] = time.time()
        save_cache(cache)
        when = time.strftime("%Y-%m-%d", time.localtime(int(result.get("expire_at") or expire_at)))
        return True, f"授权有效（到期 {when}）"

    clear_cache()
    return False, result.get("error") or "授权校验失败"
