#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把标签库真源定期导出到腾讯云 COS。

为什么需要这个脚本：
    `web-data/` 被 .gitignore 排除，`web-data/template_tags.json` 是标签库唯一真源，
    却只存在于本机磁盘——机器坏了、目录被误删，就什么都不剩。
    本脚本把真源（以及版本快照）压成 gzip 传到 COS，每次上传带 revision 与内容指纹，
    内容没变就不重复传，跑多少次都幂等。

用法：
    python scripts/backup_tag_store.py                  # 备份一次（内容未变则跳过）
    python scripts/backup_tag_store.py --dry-run        # 只打印计划，不真传
    python scripts/backup_tag_store.py --no-versions    # 只传真源，不传版本快照
    python scripts/backup_tag_store.py --keep 30        # 云端只保留最近 30 份全量
    python scripts/backup_tag_store.py --bucket video-xxx-1463907213

配置来源（优先级从高到低）：
    1) 命令行 --bucket/--region
    2) 环境变量 COS_BUCKET / COS_REGION / COS_SECRET_ID / COS_SECRET_KEY
    3) server/config.json 的 cos 段（本机现成配置，默认用 output_bucket）
    4) ~/.cos/credentials.json 或 tccli 凭证（只补密钥，桶名仍需 2/3 提供）

退出码：0 成功（含"内容未变跳过"）/ 1 有失败 / 2 配置或参数问题。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.storage import (  # noqa: E402
    StorageConfig, StorageError, open_storage,
)

DEFAULT_STORE = ROOT / "web-data" / "template_tags.json"
DEFAULT_CONFIG = ROOT / "server" / "config.json"
LOCK_STALE_SECONDS = 6 * 3600


def state_dir() -> Path:
    """日志与锁的落点；测试用 OUTFIT_BACKUP_STATE 指到临时目录，避免污染真项目。"""
    raw = (os.environ.get("OUTFIT_BACKUP_STATE") or "").strip()
    return Path(raw).expanduser() if raw else ROOT / "web-data" / "logs"


def log_path() -> Path:
    return state_dir() / "tag_backup.log"


def lock_path() -> Path:
    return state_dir() / ".tag_backup.lock"


# ---------------------------------------------------------------------------
# 参数与配置
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="标签库真源 → 腾讯云 COS 定期备份")
    p.add_argument("--store", default="", help=f"真源路径（默认 {DEFAULT_STORE.name}，也认 OUTFIT_TAG_STORE）")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="读 COS 配置的 JSON（默认 server/config.json）")
    p.add_argument("--bucket", default="", help="覆盖存储桶（默认取 cos.output_bucket）")
    p.add_argument("--region", default="", help="覆盖地域")
    p.add_argument("--root-prefix", default="backup/tags", help="对象根前缀（默认 backup/tags）")
    p.add_argument("--keep", type=int, default=60, help="云端保留最近 N 份全量备份，0=不清理（默认 60）")
    p.add_argument("--keep-versions", type=int, default=0, help="云端保留最近 N 个版本快照，0=不清理（默认 0）")
    p.add_argument("--no-versions", action="store_true", help="不传 tag_versions 快照")
    p.add_argument("--versions-dir", default="", help="版本快照目录（默认 web-data/tag_versions）")
    p.add_argument("--force", action="store_true", help="内容未变也重新上传")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不真传、不清理")
    p.add_argument("--restore", action="store_true", help="从云端恢复真源（会覆盖本机，需 --yes）")
    p.add_argument("--restore-key", default="", help="指定恢复的对象 Key（默认 latest/）")
    p.add_argument("--yes", action="store_true", help="恢复时跳过确认")
    p.add_argument("--quiet", action="store_true", help="只写日志，不打印（计划任务用）")
    return p.parse_args(argv)


def resolve_store(args: argparse.Namespace) -> Path:
    raw = (args.store or os.environ.get("OUTFIT_TAG_STORE") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_STORE


def build_config(args: argparse.Namespace) -> StorageConfig:
    """env → server/config.json → 本机凭证，逐级补齐。"""
    cfg = StorageConfig.from_env()
    if cfg.provider == "local":
        # 离线演练 / 测试：COS_PROVIDER=local + COS_LOCAL_ROOT=<目录>
        cfg.enabled = True
        return cfg

    local = read_cos_section(args.config)

    cfg.bucket = (args.bucket or cfg.bucket
                  or str(local.get("output_bucket") or local.get("bucket") or ""))
    cfg.region = args.region or cfg.region or str(local.get("region") or "")
    cfg.secret_id = cfg.secret_id or str(local.get("secret_id") or "")
    cfg.secret_key = cfg.secret_key or str(local.get("secret_key") or "")

    if not (cfg.secret_id and cfg.secret_key):
        # 本机可能已经 tccli auth login 或放了 ~/.cos/credentials.json
        cfg = StorageConfig.from_local_profile(
            bucket=cfg.bucket, region=cfg.region,
            secret_id=cfg.secret_id, secret_key=cfg.secret_key,
        )
    cfg.provider = "cos"
    cfg.enabled = True
    return cfg


def read_cos_section(config_path: str) -> dict:
    path = Path(config_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"读不了 {path}：{exc}") from None
    cos = data.get("cos")
    return cos if isinstance(cos, dict) else {}


# ---------------------------------------------------------------------------
# 真源读取（写侧是原子写，这里前后 stat 比对，避免读到写一半的文件）
# ---------------------------------------------------------------------------

def read_store(path: Path) -> tuple[bytes, dict, dict]:
    if not path.is_file():
        raise StorageError(f"真源不存在：{path}")
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        time.sleep(0.2)
        raw = path.read_bytes()
        after = path.stat()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"真源不是合法 JSON：{exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list):
        raise StorageError("真源结构异常：缺少 videos 列表，拒绝备份")
    meta = {
        "revision": data.get("revision"),
        "videos": len(data.get("videos") or []),
        "tags": len(((data.get("library") or {}).get("tags")) or []),
        "updated_at": data.get("updated_at"),
        "updated_by": data.get("updated_by"),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return raw, data, meta


def gzip_bytes(raw: bytes) -> bytes:
    # mtime=0：同样内容压出同样的字节，便于人工比对
    return gzip.compress(raw, compresslevel=9, mtime=0)


# ---------------------------------------------------------------------------
# 锁：计划任务与手动执行可能撞车
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age > LOCK_STALE_SECONDS:
            path.unlink(missing_ok=True)
        else:
            return False
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.close(fd)
    return True


def release_lock() -> None:
    lock_path().unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def write_log(line: str) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}  {line}\n")


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def sync_versions(storage, versions_dir: Path, prefix: str, args) -> tuple[int, int]:
    """只传云端还没有的快照；按文件名比对，不做额外哈希。"""
    files = sorted(p for p in versions_dir.iterdir()
                   if p.is_file() and (p.name.endswith(".json.gz") or p.name.endswith(".meta.json")))
    if not files:
        return (0, 0)
    remote = {Path(row["key"]).name for row in storage.list_prefix(f"{prefix}/versions", limit=5000)}
    todo = [f for f in files if f.name not in remote]
    uploaded = 0
    for path in todo:
        if args.dry_run:
            print(f"  [待传] 版本快照 {path.name}")
            continue
        try:
            storage.put_bytes(path.read_bytes(), f"{prefix}/versions/{path.name}",
                              content_type="application/json" if path.name.endswith(".json")
                              else "application/gzip")
            uploaded += 1
        except FileNotFoundError:
            # 本地版本轮转（保留最近 N 个）会把老快照删掉，跳过即可
            continue
    return (uploaded, len(todo) - uploaded if args.dry_run else 0)


def prune(storage, prefix: str, keep: int, args) -> int:
    """只清理本脚本生成的 `template_tags-r*.json.gz`，latest/ 与 versions/ 不碰。"""
    if keep <= 0 or args.dry_run:
        return 0
    rows = storage.list_prefix(prefix, limit=2000)
    candidates = [
        row["key"] for row in rows
        if "/template_tags-r" in row["key"] and row["key"].endswith(".json.gz")
        and "/latest/" not in row["key"] and "/versions/" not in row["key"]
    ]
    # 按 key 里的时间戳段排序，而不是按 revision：回滚后 revision 会变小
    candidates.sort(key=_key_time, reverse=True)
    stale = candidates[keep:]
    for key in stale:
        storage.delete(key)
    return len(stale)


_TS_RE = re.compile(r"-(\d{8}-\d{6})-[0-9a-f]{8}\.json\.gz$")


def _key_time(key: str) -> tuple[str, str]:
    match = _TS_RE.search(key)
    return (match.group(1) if match else "", key)


def restore(storage, root_prefix: str, store_path: Path, args) -> int:
    """从云端拉回一份备份，校验指纹后原子替换本机真源。"""
    key = args.restore_key.strip() or f"{root_prefix}/latest/template_tags.json.gz"
    print(f"恢复源  {key}")

    def fetch(obj_key: str) -> bytes:
        try:
            return urllib.request.urlopen(storage.sign_url(obj_key, 600), timeout=120).read()
        except Exception as exc:  # noqa: BLE001 - 网络/权限错误统一翻译
            raise StorageError(f"下载 {obj_key} 失败：{exc}") from None

    try:
        raw = gzip.decompress(fetch(key))
    except OSError as exc:
        raise StorageError(f"备份不是合法 gzip：{exc}") from None
    sha = hashlib.sha256(raw).hexdigest()

    meta: dict = {}
    if key.endswith("/latest/template_tags.json.gz"):
        try:
            meta = json.loads(fetch(f"{root_prefix}/latest/meta.json").decode("utf-8"))
        except (StorageError, ValueError):
            meta = {}
    if meta.get("sha256") and meta["sha256"] != sha:
        raise StorageError(
            f"指纹不符，拒绝恢复：meta {str(meta['sha256'])[:12]} / 实际 {sha[:12]}")

    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"备份内容不是合法 JSON：{exc}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("videos"), list):
        raise StorageError("备份结构异常：缺少 videos 列表")

    rev = doc.get("revision")
    print(f"备份    revision {rev}｜{len(doc['videos'])} 行｜sha {sha[:8]}")
    print(f"本机    {store_path}")

    if args.dry_run:
        print("（--dry-run，未写盘）")
        return 0
    if not args.yes:
        raise StorageError("恢复会覆盖本机真源；确认后加 --yes 重跑")

    if store_path.is_file():
        keep = store_path.with_name(
            f"{store_path.name}.before-restore-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(store_path, keep)
        print(f"备份    当前真源 → {keep.name}")

    tmp = store_path.with_name(store_path.name + ".restore.tmp")
    tmp.write_bytes(raw)
    tmp.replace(store_path)
    print(f"[OK]    已恢复 revision {rev}｜{len(doc['videos'])} 行")
    print("        重启网页服务后「标签」页才会读到新数据")
    write_log(f"RESTORE r{rev} sha={sha[:8]} from={key}")
    return 0


def run(args: argparse.Namespace) -> int:
    store_path = resolve_store(args)
    versions_dir = Path(args.versions_dir).expanduser() if args.versions_dir else ROOT / "web-data" / "tag_versions"

    try:
        cfg = build_config(args)
        cfg.validate()
        storage = open_storage(cfg)
        if storage is None:
            raise StorageError("存储未启用")
    except StorageError as exc:
        print(f"[FAIL] 配置有误：{exc}", file=sys.stderr)
        write_log(f"FAIL 配置：{exc}")
        return 2

    root_prefix = "/".join(p for p in (cfg.clean_prefix(), args.root_prefix.strip("/")) if p)

    if args.restore:
        try:
            return restore(storage, root_prefix, store_path, args)
        except StorageError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            write_log(f"FAIL 恢复：{exc}")
            return 1

    try:
        raw, _data, meta = read_store(store_path)
    except StorageError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        write_log(f"FAIL 真源：{exc}")
        return 2

    sha8 = meta["sha256"][:8]
    rev = meta["revision"]
    payload = gzip_bytes(raw)
    now = datetime.now()
    key = f"{root_prefix}/{now:%Y-%m-%d}/template_tags-r{rev}-{now:%Y%m%d-%H%M%S}-{sha8}.json.gz"

    print(f"真源   {store_path}")
    print(f"规模    revision {rev}｜{meta['videos']} 行｜{meta['tags']} 标签｜"
          f"{human(meta['size'])} → gzip {human(len(payload))}")
    print(f"目标   {cfg.describe()}｜前缀 {root_prefix}")
    print(f"指纹   {sha8}")

    started = time.time()
    try:
        # 1) 内容去重：同一份内容只留一个对象
        existing = storage.list_prefix(root_prefix, limit=2000)
        already = [row["key"] for row in existing if row["key"].endswith(f"-{sha8}.json.gz")]
        changed = not already or args.force

        if args.dry_run:
            print(f"  [{'待传' if changed else '已有'}] {key}")
            if not args.no_versions:
                sync_versions(storage, versions_dir, root_prefix, args)
            print("（--dry-run，未上传、未清理）")
            return 0

        if changed:
            storage.put_bytes(payload, key, content_type="application/gzip")
            size = int(storage.stat(key).get("size", 0))
            if size != len(payload):
                raise StorageError(f"上传后大小不一致：本地 {len(payload)} / 云端 {size}")
            print(f"[OK]   上传 {key}（{human(size)}，{time.time() - started:.1f}s）")
        else:
            print(f"[SKIP] 内容未变，云端已有同指纹备份：{already[0]}")
            key = already[0]

        # 2) latest 指针：恢复时不用翻目录
        meta_doc = {
            **{k: meta[k] for k in ("revision", "videos", "tags", "sha256", "size")},
            "updated_at": meta["updated_at"],
            "updated_by": meta["updated_by"],
            "backup_key": key,
            "backup_at": now.astimezone().isoformat(timespec="seconds"),
        }
        detail = {"revision": rev, "videos": meta["videos"], "tags": meta["tags"],
                  "sha256": meta["sha256"], "zipped": len(payload),
                  "backup_key": key, "backup_at": meta_doc["backup_at"]}
        storage.put_bytes(payload, f"{root_prefix}/latest/template_tags.json.gz",
                          content_type="application/gzip")
        storage.put_bytes(json.dumps(detail, ensure_ascii=False, indent=2).encode("utf-8"),
                          f"{root_prefix}/latest/meta.json", content_type="application/json")
        print(f"[OK]   latest 已更新（revision {rev}）")

        # 3) 版本快照（失败只告警：真源已经安全，快照是锦上添花）
        v_up = 0
        if not args.no_versions:
            try:
                v_up, _ = sync_versions(storage, versions_dir, root_prefix, args)
            except (StorageError, OSError) as exc:
                print(f"[WARN] 版本快照同步失败：{exc}")
            if v_up:
                print(f"[OK]   版本快照补传 {v_up} 个")

        # 4) 保留策略
        removed = prune(storage, root_prefix, args.keep, args)
        if removed:
            print(f"[OK]   清理旧全量备份 {removed} 份（保留 {args.keep}）")

    except (StorageError, OSError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        write_log(f"FAIL r{rev} sha={sha8}：{exc}")
        return 1

    elapsed = time.time() - started
    line = (f"{'OK  ' if changed else 'SKIP'} r{rev} {meta['videos']}行 {human(len(payload))} "
            f"sha={sha8} key={key} {elapsed:.1f}s")
    print(f"完成   耗时 {elapsed:.1f}s")
    write_log(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sink = None
    if args.quiet:
        # pythonw.exe 下 sys.stdout 是 None，直接 print 会炸；统一收进日志文件
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        sink = path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sink
        sys.stderr = sink

    try:
        if not acquire_lock():
            msg = "已有备份在跑（或上次异常退出，锁未释放超过 6 小时才会自动接管）"
            print(f"[SKIP] {msg}")
            write_log(f"SKIP {msg}")
            return 0
        try:
            return run(args)
        except Exception as exc:  # noqa: BLE001 - 计划任务里必须留痕
            print(f"[FAIL] 未预期错误：{exc}", file=sys.stderr)
            write_log(f"FAIL 未预期：{exc!r}")
            return 1
        finally:
            release_lock()
    finally:
        if sink is not None:
            sink.flush()
            sink.close()


if __name__ == "__main__":
    raise SystemExit(main())
