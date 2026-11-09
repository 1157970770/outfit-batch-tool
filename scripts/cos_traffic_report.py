#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""COS 外网下行流量报表：把账本、生产记录、费用估算放在一张表里对账。

    python scripts/cos_traffic_report.py              # 最近 7 天
    python scripts/cos_traffic_report.py --days 14
    python scripts/cos_traffic_report.py --data-dir "D:/批量视频任务运行区/web-data"

数据来源
--------
* ``<data_dir>/traffic/YYYY-MM-DD.jsonl``  —— app/traffic.py 写的出口账本
* ``<data_dir>/jobs/*.json``               —— 每天真正产出了多少条（success）
* ``<data_dir>/template_tags.json``        —— 模板平均体积，用来把预览字节折成「看了几次」

三类数字的可靠性不同，别混着看：
  实测  预览 / 下载：我们自己代理，字节数是数出来的。
  推定  模板 / 图片：只交出了签名 URL，按对象大小估。
  未知  归档直链：交给同事后才产生流量，本机只记次数。
和 COS 控制台的「外网下行流量」日曲线比时，实测那一半应该几乎完全吻合；
差额（尤其是模板/图片）就是推定误差，也正是需要观察一周才能定的参数。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.traffic import MEASURED, SOURCE_LABELS, collect  # noqa: E402

GB = 1024 ** 3
MB = 1024 ** 2
DEFAULT_PRICE = 0.5        # 元/GB，腾讯云 COS 外网下行 0–500GB 档的量级
FALLBACK_TPL_MB = 12.39    # 模板平均体积兜底（1457 条实测均值）


def resolve_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    try:
        web = json.loads((ROOT / "server" / "web.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        web = {}
    return (ROOT / (web.get("data_root") or "web-data")).resolve()


def production_by_day(data_dir: Path) -> dict[str, int]:
    """每天 success 的条数（按 job 的 created_at 归日）。"""
    out: dict[str, int] = {}
    jobs_dir = data_dir / "jobs"
    if not jobs_dir.is_dir():
        return out
    for path in jobs_dir.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        created = job.get("created_at")
        if not created:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(float(created)))
        ok = sum(1 for rec in (job.get("records") or [])
                 if isinstance(rec, dict) and rec.get("status") == "success")
        if ok:
            out[day] = out.get(day, 0) + ok
    return out


def average_template_mb(data_dir: Path) -> float:
    try:
        data = json.loads((data_dir / "template_tags.json").read_text(encoding="utf-8"))
        sizes = [float(v.get("size_mb") or 0) for v in data.get("videos") or []
                 if v.get("size_mb")]
        if sizes:
            return sum(sizes) / len(sizes)
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return FALLBACK_TPL_MB


def money(gb: float, price: float) -> str:
    return f"{gb * price:.1f} 元"


def display_width(text: str) -> int:
    """终端显示宽度：汉字/全角算 2 列，否则算 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(value, width: int, right: bool = False) -> str:
    """按显示宽度补空格，保证含中文的表头和数字列能对齐。"""
    text = str(value)
    gap = " " * max(0, width - display_width(text))
    return gap + text if right else text + gap


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="COS 外网下行流量报表")
    parser.add_argument("--days", type=int, default=7, help="看最近多少天（默认 7）")
    parser.add_argument("--data-dir", default="", help="数据目录（默认读 server/web.json）")
    parser.add_argument("--price", type=float, default=DEFAULT_PRICE, help="元/GB")
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir or None)
    stats = collect(days=args.days, directory=data_dir / "traffic")
    print(f"COS 外网下行流量报表 · {data_dir}")
    if not stats["files"]:
        print(f"\n还没有账本：{data_dir / 'traffic'} 下没有 *.jsonl。")
        print("先让网页服务跑起来（账本在 WebApplication 初始化时启用），"
              "有人预览/生产后就会有数据。")
        return 0
    print(f"账本范围：{stats['first']} ~ {stats['last']}（{stats['days']} 天）")
    print(f"单价假设：{args.price:g} 元/GB（改单价用 --price）\n")

    prod = production_by_day(data_dir)
    avg_tpl = average_template_mb(data_dir)

    # ---- 逐日 ----
    print(pad("日期", 12) + pad("生产条数", 10, True) + pad("预览GB", 10, True)
          + pad("下载GB", 10, True) + pad("模板GB", 10, True) + pad("图片GB", 10, True)
          + pad("合计GB", 10, True) + pad("每条MB", 10, True))
    print("-" * 82)
    grand = 0.0
    for day in stats["files"]:
        row = stats["by_day"].get(day) or {}

        def gb(src: str) -> float:
            return (row.get(src, {}).get("b") or 0) / GB

        total = sum((slot.get("b") or 0) for slot in row.values()) / GB
        grand += total
        items = prod.get(day, 0)
        per_item = f"{total * 1024 / items:.1f}" if items else "-"
        print(pad(day, 12) + pad(items, 10, True)
              + pad(f"{gb('preview'):.2f}", 10, True) + pad(f"{gb('download'):.2f}", 10, True)
              + pad(f"{gb('template'):.2f}", 10, True) + pad(f"{gb('image'):.2f}", 10, True)
              + pad(f"{total:.2f}", 10, True) + pad(per_item, 10, True))

    # ---- 来源明细 ----
    measured_gb = sum((v["b"] for k, v in stats["totals"].items() if k in MEASURED)) / GB
    guessed_gb = sum((v["b"] for k, v in stats["totals"].items()
                      if k not in MEASURED and k != "archive")) / GB
    unknown_n = (stats["totals"].get("archive") or {}).get("n", 0)

    print(f"\n合计 {grand:.2f} GB ≈ {money(grand, args.price)}"
          f"（实测 {measured_gb:.2f} GB / 推定 {guessed_gb:.2f} GB）")

    print("\n来源明细")
    print(pad("来源", 16) + pad("次数", 8, True) + pad("GB", 10, True)
          + pad("占比", 9, True) + "  性质")
    for src, slot in sorted(stats["totals"].items(), key=lambda kv: -kv[1]["b"]):
        percent = f"{slot['b'] / GB / grand * 100:.1f}%" if grand else "-"
        if src in MEASURED:
            kind = "实测"
        elif src == "archive":
            kind = "未知（本机不可见）"
        else:
            kind = "推定"
        print(pad(SOURCE_LABELS.get(src, src), 16) + pad(slot["n"], 8, True)
              + pad(f"{slot['b'] / GB:.2f}", 10, True) + pad(percent, 9, True) + "  " + kind)

    # ---- 预览行为：这一周最想量出来的数 ----
    pv = stats["totals"].get("preview") or {"n": 0, "b": 0}
    rng_total = sum(v["total"] for v in stats["ranges"].values())
    rng_hit = sum(v["range"] for v in stats["ranges"].values())
    tpl_ids: set = set()
    for day_set in stats["templates"].values():
        tpl_ids |= day_set
    items_total = sum(prod.get(day, 0) for day in stats["files"])
    views = pv["b"] / (avg_tpl * MB) if avg_tpl else 0

    print("\n预览行为")
    print(f"  请求 {pv['n']} 次，其中带 Range {rng_hit} 次"
          f"（{rng_hit / rng_total * 100:.0f}%）" if rng_total else "  请求 0 次")
    if tpl_ids:
        print(f"  覆盖模板 {len(tpl_ids)} 个，平均每个被拉 {views / len(tpl_ids):.2f} 次")
    if items_total:
        print(f"  同期生产 {items_total} 条，平均每条生产对应预览 {views / items_total:.2f} 次")
        print(f"  等效观看 {views:.0f} 次（预览字节 ÷ 模板均值 {avg_tpl:.1f}MB）")

    # ---- 没计到的东西，明说 ----
    print("\n未计量 / 缺口")
    if unknown_n:
        print(f"  · 归档直链签名 {unknown_n} 次：字节发生在同事电脑侧，本机看不到，"
              "需要拿 COS 日曲线兜底")
    print("  · 模板/图片是推定值：万相若对同一 URL 做了缓存，实测会明显低于推定")
    print("  · 预览字节只在「无本地缓存」时等于 COS 流量；以后加了缓存要按命中率折算")

    # ---- 投影 ----
    if stats["days"] >= 3 and grand > 0:
        daily = grand / stats["days"]
        print(f"\n按这 {stats['days']} 天的日均 {daily:.2f} GB 投影：")
        print(f"  26 天 ≈ {daily * 26:.0f} GB ≈ {money(daily * 26, args.price)}")
        print(f"  30 天 ≈ {daily * 30:.0f} GB ≈ {money(daily * 30, args.price)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
