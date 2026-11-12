#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计费与统计口径的唯一实现（网页端、数据镜像、外部报表都走这里）。

为什么单独一个模块：
    ``JobManager.usage_for()``、``work_for()``、``scripts/feishu_daily_report.py``
    都要回答同两个问题——「这条记录算不算失败」「这条记录花了多少钱」。以前口径写在
    ``app/web/jobs.py`` 的方法里，外部脚本只能复制一份，任何一边改了另一边就悄悄错。
    这里把纯计算抽出来，谁要算就 import 谁，不再有第二份实现。

只做纯函数：不读配置、不碰文件、不依赖 WebSettings；单价由调用方注入
（形如 ``settings.unit_price``，返回 ``(元/秒 或 None, 来源)``）。
"""
from __future__ import annotations

# 统计口径里的「失败侧」：非成功终态全部计入，保证 items = success + failed + unfinished。
# unknown（提交结果未知/停止等待）云端可能已建任务并计费，必须算失败侧而非静默消失；
# skipped 与 timeout 一样在重试白名单里，同属失败侧。
FAILED_RECORD_STATES = frozenset(
    {"failed", "invalid", "expired", "canceled", "unknown", "timeout", "skipped"})

# 任务分类的兜底名：老任务、试跑任务（没有批次）没有分类字段，统计时归到这里，
# 绝不能把它们的消耗从报表里丢掉。
UNCATEGORIZED = "未分类"


def record_cost(rec: dict, job: dict, unit_price) -> tuple[float, str]:
    """单条记录的费用：优先用生成时落下的金额，否则按当前单价回算。

    返回 ``(金额, 来源)``，来源有三种：

    * ``record``——生成时实测落账（``rec.estimated_cost`` 有值）；
    * ``derived``——历史任务没有落账（单价还是 null 的年代），按当前单价回算；
    * ``missing``——既没有落账也没有计费秒/单价，金额按 0 计。

    回算只发生在读取时，**不改任务文件**：``cost_source`` 就是给消费方区分
    「实测」与「事后估算」用的。
    """
    raw = rec.get("estimated_cost")
    if raw not in (None, ""):
        try:
            return float(raw), "record"
        except (TypeError, ValueError):
            pass
    try:
        seconds = float(rec.get("billable_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    price, _source = unit_price(str(job.get("resolution") or ""))
    if seconds > 0 and price:
        return seconds * float(price), "derived"
    return 0.0, "missing"


def is_failed(status) -> bool:
    """非成功终态（含 timeout / unknown / skipped），与重试白名单同一套定义。"""
    return str(status or "") in FAILED_RECORD_STATES


def category_of(job: dict) -> str:
    """任务分类；老任务与试跑任务没有分类，统一折算成「未分类」。"""
    return str(job.get("category") or "").strip() or UNCATEGORIZED
