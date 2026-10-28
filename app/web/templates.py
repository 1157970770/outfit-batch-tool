#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板选择逻辑；浏览器只拿模板编号/名称，不拿 COS 地址。"""
from __future__ import annotations

import math
import random


class TemplateError(ValueError):
    pass


class TemplateCatalog:
    def __init__(self, templates: list[dict]):
        self.items = [dict(item) for item in templates]
        self.by_id = {str(item.get("id")): item for item in self.items}

    def public(self) -> list[dict]:
        return [{"id": str(t.get("id", "")), "name": str(t.get("name") or f"场景 {t.get('id', '')}"),
                 "shot": str(t.get("shot") or ""), "duration": t.get("duration"),
                 "size_mb": t.get("size_mb")} for t in self.items]

    def choose(self, mode: str, count: int = 1, template_id: str = "", max_items: int = 20) -> list[dict]:
        if not self.items:
            raise TemplateError("模板暂不可用，请管理员检查模板清单后重试")
        mode = (mode or "random").lower()
        if mode == "selected":
            item = self.by_id.get(str(template_id))
            if not item:
                raise TemplateError("请选择有效的视频场景")
            selected = [item]
        elif mode == "random":
            try:
                count = int(count)
            except (TypeError, ValueError):
                raise TemplateError("生成数量不正确") from None
            if not 1 <= count <= min(max_items, len(self.items)):
                raise TemplateError(f"一次可生成 1–{min(max_items, len(self.items))} 条")
            selected = random.SystemRandom().sample(self.items, count)
        else:
            raise TemplateError("请选择自动推荐或手动挑选场景")
        return [dict(item) for item in selected]

    def preview(self, template_id: str) -> dict:
        item = self.by_id.get(str(template_id))
        if not item:
            raise TemplateError("找不到该视频场景")
        return item
