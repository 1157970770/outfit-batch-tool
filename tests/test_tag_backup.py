#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标签库 → COS 备份脚本的离线测试（LocalStorage 镜像，不联网、不花钱）。

覆盖：真源读取与指纹、内容去重、latest 指针、版本快照增量、保留策略。
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tmpdir import tempdir  # noqa: E402

import backup_tag_store as B  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


def make_store(path: Path, videos: int = 2, revision: int = 10) -> bytes:
    doc = {
        "schema": 2,
        "revision": revision,
        "updated_at": 1793760000 + revision,
        "updated_by": "test",
        "library": {"version": 1,
                    "tags": [{"id": "t1", "name": "通勤"}, {"id": "t2", "name": "运动"}],
                    "categories": [{"id": "c1", "name": "场景"}]},
        "videos": [{"id": f"v{i}", "name": f"第{i}套", "tags": ["通勤"]} for i in range(videos)],
    }
    raw = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return raw


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = B.main(argv)
    return code, buf.getvalue()


def keys(remote: Path, prefix: str = "backup/tags") -> list[str]:
    base = remote.joinpath(*prefix.split("/"))
    if not base.exists():
        return []
    return sorted(p.relative_to(remote).as_posix() for p in base.rglob("*") if p.is_file())


def full_keys(remote: Path) -> list[str]:
    return [k for k in keys(remote)
            if "/template_tags-r" in k and "/latest/" not in k and "/versions/" not in k]


def version_keys(remote: Path) -> list[str]:
    return [k for k in keys(remote) if "/versions/" in k]


def main() -> int:
    print("标签库 COS 备份（离线）")

    tmp = Path(tempdir("tag-backup"))
    store = tmp / "template_tags.json"
    versions = tmp / "tag_versions"
    remote = tmp / "remote"
    state = tmp / "state"
    versions.mkdir()
    (versions / "v000001-1793760000-auto-aaaa.json.gz").write_bytes(b"gz-1")
    (versions / "v000001-1793760000-auto-aaaa.json.gz.meta.json").write_text(
        json.dumps({"revision": 1, "rated": 0}), encoding="utf-8")

    os.environ["COS_PROVIDER"] = "local"
    os.environ["COS_LOCAL_ROOT"] = str(remote)
    os.environ["OUTFIT_BACKUP_STATE"] = str(state)
    os.environ.pop("OUTFIT_TAG_STORE", None)
    os.environ.pop("COS_BUCKET", None)

    raw = make_store(store, videos=2, revision=10)
    sha = hashlib.sha256(raw).hexdigest()
    base_argv = ["--store", str(store), "--versions-dir", str(versions),
                 "--config", str(tmp / "no-config.json")]

    # 1) 首次备份：真源 + latest + 版本快照
    code, out = run_cli(base_argv)
    check(code == 0, "首次备份退出码 0")
    check(len(full_keys(remote)) == 1, "全量备份落 1 个对象")
    latest = remote / "backup" / "tags" / "latest" / "template_tags.json.gz"
    check(latest.is_file(), "latest 指针存在")
    check(hashlib.sha256(gzip.decompress(latest.read_bytes())).hexdigest() == sha,
          "latest 解压后与真源字节一致")
    meta = json.loads((latest.parent / "meta.json").read_text(encoding="utf-8"))
    check(meta["revision"] == 10 and meta["videos"] == 2 and meta["tags"] == 2,
          "latest/meta.json 记录 revision/行数/标签数")
    check(meta["sha256"] == sha, "meta 里的 sha256 与真源一致")
    check(len(version_keys(remote)) == 2, "版本快照与 .meta.json 各传 1 个")

    # 2) 内容未变：幂等跳过
    code, out = run_cli(base_argv)
    check(code == 0 and "SKIP" in out, "内容未变时跳过上传")
    check(len(full_keys(remote)) == 1, "跳过后对象数不变")

    # 3) 内容变化：新增一份
    raw2 = make_store(store, videos=3, revision=11)
    code, out = run_cli(base_argv)
    check(code == 0 and len(full_keys(remote)) == 2, "内容变化后新增 1 份全量")
    meta = json.loads((latest.parent / "meta.json").read_text(encoding="utf-8"))
    check(meta["revision"] == 11 and meta["sha256"] == hashlib.sha256(raw2).hexdigest(),
          "latest 指向最新 revision")

    # 4) 保留策略：只留最近 1 份，且不动 latest/versions
    code, out = run_cli(base_argv + ["--keep", "1"])
    check(code == 0 and len(full_keys(remote)) == 1, "--keep 1 只保留最新全量")
    check(latest.is_file(), "清理不动 latest")
    check(len(version_keys(remote)) == 2, "清理不动版本快照")

    # 5) 版本增量：只传缺失的那一个
    (versions / "v000002-1793760100-auto-bbbb.json.gz").write_bytes(b"gz-2")
    code, out = run_cli(base_argv + ["--keep", "1", "--force"])
    check(code == 0 and "版本快照补传 1 个" in out, "只补传缺失的版本快照")
    check(len(version_keys(remote)) == 3, "版本对象数 +1")

    # 6) 坏真源：拒绝备份，而不是传半份
    store.write_text("{ not json", encoding="utf-8")
    code, out = run_cli(base_argv)
    check(code == 2 and "不是合法 JSON" in out, "坏真源返回 2，不落任何对象")
    check(len(full_keys(remote)) == 1, "坏真源未污染云端")

    # 7) --dry-run 不写任何东西
    make_store(store, videos=4, revision=12)
    before = len(keys(remote))
    code, out = run_cli(base_argv + ["--dry-run"])
    check(code == 0 and "dry-run" in out, "--dry-run 正常返回")
    check(len(keys(remote)) == before, "--dry-run 未产生新对象")

    # 8) 恢复：无 --yes 拒绝，有 --yes 校验指纹后覆盖
    before_raw = store.read_bytes()
    code, out = run_cli(base_argv + ["--restore"])
    check(code == 1 and "--yes" in out, "无 --yes 时拒绝恢复")
    check(store.read_bytes() == before_raw, "拒绝恢复后本机真源未被改动")

    code, out = run_cli(base_argv + ["--restore", "--yes"])
    check(code == 0 and store.is_file(), "带 --yes 恢复成功")
    check(hashlib.sha256(store.read_bytes()).hexdigest() == hashlib.sha256(raw2).hexdigest(),
          "恢复内容与云端 latest 字节一致（revision 11）")
    check(list(tmp.glob("template_tags.json.before-restore-*")), "恢复前自动留了本机副本")

    check((state / "tag_backup.log").is_file(), "日志落在隔离目录，未污染项目 web-data")

    print("\n标签库 COS 备份：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
