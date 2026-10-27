#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词库：JSON 存储 + 场景/标签筛选 + {{变量}} 渲染。

一条提示词 = 标题 + 一句话说明 + 用途场景 + 自由标签 + 正文。

筛选语义刻意保持简单，对应"按需求筛"的直觉：
  · 场景：单选；``None`` / 空 / "全部" 表示不限；
  · 标签：多选，**同时满足**（子集匹配）；大小写与首尾空格忽略。

正文里的 ``{{变量}}`` 由 :func:`extract_variables` 识别，选中后现场填值，
再由 :func:`render_body` 渲染成最终提示词——库里**不存**变量默认值。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ALL_SCENES = "全部场景"

# {{名字}}：允许花括号内有空格，但不允许嵌套花括号
VARIABLE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class PromptLibraryError(RuntimeError):
    """库文件不存在、不是合法 JSON、或结构不对。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prompt:
    id: str
    title: str
    summary: str
    scene: str
    tags: tuple[str, ...]
    body: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "scene": self.scene,
            "tags": list(self.tags),
            "body": self.body,
        }

    @property
    def variables(self) -> list[str]:
        return extract_variables(self.body)


@dataclass
class PromptLibrary:
    """内存中的提示词库；``issues`` 记录被跳过的坏条目（不阻塞整页）。"""

    prompts: list[Prompt]
    scenes: list[str]
    path: Path | None = None
    issues: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.issues is None:
            self.issues = []

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
            body = str(item.get("body") or "").strip()
            title = str(item.get("title") or "").strip()
            if not body:
                issues.append(f"{where}：正文为空，已跳过")
                continue
            if not title:
                issues.append(f"{where}：标题为空，已跳过")
                continue
            pid = str(item.get("id") or "").strip() or _slug(title)
            if pid in seen:
                pid = f"{pid}-{idx}"
            seen.add(pid)
            scene = str(item.get("scene") or "").strip()
            if not scene:
                issues.append(f"{where}：缺用途场景，已归到「未分类」")
                scene = "未分类"
            tags_raw = item.get("tags") or []
            if isinstance(tags_raw, str):
                tags_raw = re.split(r"[,，、;；\s]+", tags_raw)
            tags: list[str] = []
            for tag in tags_raw if isinstance(tags_raw, list) else []:
                text = str(tag).strip()
                if text and text not in tags:
                    tags.append(text)
            prompts.append(Prompt(
                id=pid,
                title=title,
                summary=str(item.get("summary") or "").strip(),
                scene=scene,
                tags=tuple(tags),
                body=body,
            ))

        declared = raw.get("scenes") or []
        scenes: list[str] = []
        for scene in list(declared) + [p.scene for p in prompts]:
            name = str(scene).strip()
            if name and name != ALL_SCENES and name not in scenes:
                scenes.append(name)

        return cls(prompts=prompts, scenes=scenes, path=path, issues=issues)

    # ---- 查询 ----
    def filter(self, scene: str | None = None, tags: tuple[str, ...] | list[str] = ()) -> list[Prompt]:
        """场景单选 + 标签全包含。返回保持文件里的顺序（人工排过的优先级）。"""
        wanted_scene = (scene or "").strip()
        wanted_tags = [_norm(t) for t in tags if _norm(t)]
        result = []
        for prompt in self.prompts:
            if wanted_scene and wanted_scene != ALL_SCENES and prompt.scene != wanted_scene:
                continue
            have = {_norm(t) for t in prompt.tags}
            if any(tag not in have for tag in wanted_tags):
                continue
            result.append(prompt)
        return result

    def get(self, prompt_id: str) -> Prompt | None:
        return next((p for p in self.prompts if p.id == prompt_id), None)

    def scene_facets(self) -> list[tuple[str, int]]:
        """[(场景, 条数)]，按文件声明顺序，未声明的排后面。"""
        counts: dict[str, int] = {}
        for prompt in self.prompts:
            counts[prompt.scene] = counts.get(prompt.scene, 0) + 1
        ordered = [s for s in self.scenes if s in counts]
        ordered += [s for s in counts if s not in ordered]
        return [(s, counts[s]) for s in ordered]

    def tag_facets(self, scene: str | None = None) -> list[tuple[str, int]]:
        """[(标签, 条数)]，按条数从多到少；可按场景先收窄。"""
        counts: dict[str, int] = {}
        for prompt in self.filter(scene=scene):
            for tag in prompt.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def __len__(self) -> int:
        return len(self.prompts)


# ---------------------------------------------------------------------------
# 变量
# ---------------------------------------------------------------------------

def extract_variables(body: str) -> list[str]:
    """正文里出现过的 {{变量}}，按首次出现顺序去重。"""
    names: list[str] = []
    for match in VARIABLE_RE.finditer(body or ""):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def render_body(body: str, values: dict | None = None) -> tuple[str, list[str]]:
    """用 values 替换 {{变量}}；没填的保持原样，并在第二个返回值里报告（去重）。

    返回 (渲染后的文本, 仍然缺值的变量名)。第二个返回值为空表示可以直接用。
    """
    values = {_norm(k): str(v).strip() for k, v in (values or {}).items()}
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1).strip()
        filled = values.get(_norm(name), "")
        if filled:
            return filled
        if name not in missing:
            missing.append(name)
        return match.group(0)  # 留个记号，肉眼看得出哪儿没填

    return VARIABLE_RE.sub(replace, body or ""), missing


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


def _norm(text: str) -> str:
    return (text or "").strip().casefold()


def _slug(text: str) -> str:
    keep = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-").lower()
    return keep[:40] or "prompt"
