#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库：一个 JSON 文件，按名字取用沉淀好的提示词。

不做分类、不做标签筛选——条数少时名字本身就是索引。页面上只有三件事：
列表 → 全文 → 复制（或直接填入生产页）。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


class PromptLibraryError(RuntimeError):
    """库文件不存在、不是合法 JSON、或结构不对。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prompt:
    id: str
    title: str
    body: str

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "body": self.body}

    @property
    def chars(self) -> int:
        return len(self.body)


@dataclass
class PromptLibrary:
    """内存中的提示词库；``issues`` 记录被跳过的坏条目（不阻塞整页）。"""

    prompts: list[Prompt] = field(default_factory=list)
    path: Path | None = None
    issues: list[str] = field(default_factory=list)

    # ---- 加载 ----
    @classmethod
    def load(cls, path: str | Path | None = None) -> "PromptLibrary":
        target = Path(path) if path else library_path()
        if not target.exists():
            raise PromptLibraryError(f"提示词库文件不存在：{target}")
        try:
            raw = json.loads(target.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise PromptLibraryError(f"{target.name} 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}") from None
        except OSError as exc:
            raise PromptLibraryError(f"读不了 {target}：{exc}") from None
        return cls.from_data(raw, path=target)

    @classmethod
    def from_data(cls, raw: object, path: Path | None = None) -> "PromptLibrary":
        if not isinstance(raw, dict):
            raise PromptLibraryError("库文件顶层必须是对象：{\"version\": 1, \"prompts\": [...]}")
        items = raw.get("prompts")
        if not isinstance(items, list):
            raise PromptLibraryError("库文件缺少 prompts 数组")

        issues: list[str] = []
        prompts: list[Prompt] = []
        seen: set[str] = set()
        for idx, item in enumerate(items, 1):
            where = f"第 {idx} 条"
            if not isinstance(item, dict):
                issues.append(f"{where}：不是对象，已跳过")
                continue
            title = str(item.get("title") or "").strip()
            body = str(item.get("body") or "").strip()
            if not body:
                issues.append(f"{where}：正文为空，已跳过")
                continue
            if not title:
                issues.append(f"{where}：没有名字，已跳过")
                continue
            pid = str(item.get("id") or "").strip() or f"p{idx:02d}"
            if pid in seen:
                pid = f"{pid}-{idx}"
            seen.add(pid)
            prompts.append(Prompt(id=pid, title=title, body=body))

        return cls(prompts=prompts, path=path, issues=issues)

    # ---- 查询 ----
    def get(self, prompt_id: str) -> Prompt | None:
        return next((p for p in self.prompts if p.id == prompt_id), None)

    def __len__(self) -> int:
        return len(self.prompts)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def library_path() -> Path:
    """库文件位置，优先级：环境变量 PROMPT_LIBRARY > 打包目录 > 源码仓库根。

    打包后放在 exe 同目录的 prompts/ 下，方便升级时整包替换；源码运行时用仓库里的那份。
    """
    override = (os.getenv("PROMPT_LIBRARY") or "").strip()
    if override:
        return Path(override)

    name = Path("prompts") / "prompt-library.json"
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / name)
        bundle = getattr(sys, "_MEIPASS", "")
        if bundle:
            candidates.append(Path(bundle) / name)
    candidates.append(Path(__file__).resolve().parent.parent / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]
