#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「1 张定妆图 + 1 套换装提示词模板 + N 行服装描述」展开成 wan3 批量任务文件。

    python3 make_batch.py --spec batch/spec.example.json --out batch/tasks.batch.json
    python3 make_batch.py --seed-outfits 100 --out batch/outfits.tsv

生成后直接交给 wan3_r2v.py validate / run，校验规则完全复用同一份实现。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wan3_r2v import build_body  # noqa: E402  复用同一套请求体校验

PLACEHOLDER = re.compile(r"\{(\w+)\}")

TOPS = [
    "真丝衬衫", "针织开衫", "廓形西装外套", "羊绒大衣", "短款皮衣",
    "水洗牛仔外套", "工装夹克", "泡泡袖连衣裙", "缎面吊带长裙", "改良旗袍",
    "连帽卫衣", "风衣", "针织马甲", "高领羊绒衫", "短款羽绒服",
    "一字肩针织衫", "亚麻套头衫", "衬衫式连衣裙", "小香风外套", "运动背心配防晒衬衫",
]
BOTTOMS = [
    "直筒牛仔裤", "阔腿西裤", "百褶半裙", "A 字短裙", "多口袋工装裤",
    "针织长裙", "高腰微喇裤", "束脚运动裤", "缎面长裙", "皮质短裤",
]
COLORS = [
    "米白色", "燕麦色", "雾霾蓝", "墨绿色", "酒红色",
    "焦糖色", "奶油粉色", "纯黑色", "象牙白", "冷灰色",
]
SCENES = [
    "午后咖啡馆窗边", "城市天台黄昏", "极简白墙影棚", "日式木质长廊", "秋日银杏街道",
    "海边栈桥", "复古书店过道", "雪后松林", "落地窗公寓客厅", "夜樱街道",
]
SHOTS = [
    "中景缓慢环绕", "半身特写轻微推近", "全身缓慢横移", "低角度仰拍", "逆光轮廓缓慢拉远",
]


def seed_outfits(count: int) -> list[str]:
    """生成 count 行「服装 + 场景 + 镜头」，保证不重复、可读、可直接改。"""
    rows: list[str] = []
    for scene_idx, scene in enumerate(SCENES):
        for slot in range(10):
            if len(rows) >= count:
                break
            top = TOPS[(scene_idx * 3 + slot) % len(TOPS)]
            bottom = BOTTOMS[(scene_idx * 7 + slot) % len(BOTTOMS)]
            color = COLORS[(scene_idx * 2 + slot) % len(COLORS)]
            shot = SHOTS[slot % len(SHOTS)]
            rows.append(f"{color}{top}搭配{bottom}\t{scene}\t{shot}")
        if len(rows) >= count:
            break
    return rows


def read_rows(path: Path, fields: list[str]) -> list[dict]:
    rows: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) == 1 and fields == ["outfit"]:
            rows.append({"outfit": cells[0]})
            continue
        if len(cells) < len(fields):
            raise SystemExit(f"{path}:{lineno} 只给了 {len(cells)} 列，spec.fields 需要 {len(fields)} 列：{fields}")
        rows.append(dict(zip(fields, cells)))
    return rows


def render(template: str, values: dict, where: str) -> str:
    missing = [name for name in PLACEHOLDER.findall(template) if name not in values]
    if missing:
        raise SystemExit(f"{where} 缺少占位符对应的列：{', '.join(sorted(set(missing)))}（available: {list(values)}）")
    return template.format(**values)


def slug(text: str, limit: int = 18) -> str:
    keep = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    return keep[:limit] or "task"


def main() -> int:
    parser = argparse.ArgumentParser(description="展开 wan3 换装批量任务")
    parser.add_argument("--spec")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed-outfits", type=int, metavar="N", help="生成 N 行示例服装清单后退出")
    parser.add_argument("--count", type=int, help="只取前 N 条（试跑用）")
    parser.add_argument("--unit-price", type=float, help="单条实际扣费（元），用于估算总价")
    args = parser.parse_args()

    if args.seed_outfits:
        rows = seed_outfits(args.seed_outfits)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(rows) + "\n", encoding="utf-8")
        print(f"已生成 {len(rows)} 行服装清单 -> {out}")
        print("列顺序：服装\\t场景\\t镜头（用 Excel / 记事本直接改，改完再跑 --spec）")
        return 0

    if not args.spec:
        parser.error("--spec 必填（或用 --seed-outfits 先造清单）")

    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    template = spec.get("promptTemplate")
    if not isinstance(template, str) or not template.strip():
        raise SystemExit("spec.promptTemplate 必填，用 {outfit} 这类占位符")

    fields = spec.get("fields") or ["outfit"]
    if "itemsFile" in spec:
        rows = read_rows((spec_path.parent / spec["itemsFile"]).resolve(), fields)
    elif "items" in spec:
        rows = []
        for item in spec["items"]:
            rows.append({"outfit": item} if isinstance(item, str) else dict(item))
    else:
        raise SystemExit("spec 需要 itemsFile 或 items")

    if args.count:
        rows = rows[: args.count]

    defaults = dict(spec.get("defaults") or {})
    image = spec.get("image")
    if image:
        # 相对 spec 文件解析，再尽量转成相对当前目录的路径（方便从仓库根跑 run）
        resolved = (spec_path.parent / image).resolve()
        try:
            image = str(resolved.relative_to(Path.cwd()))
        except ValueError:
            image = str(resolved)
        image = image.replace("\\", "/")
    tasks: list[dict] = []
    problems: list[str] = []

    for idx, row in enumerate(rows, 1):
        tid = f"t{idx:03d}"
        try:
            prompt = render(template, row, f"第 {idx} 行")
        except SystemExit as exc:
            problems.append(str(exc))
            continue
        task: dict = {"id": tid, "prompt": prompt, "note": row.get("outfit", "")}
        if image:
            task["imageUrls"] = [image]
        task["output"] = f"out/{tid}-{slug(row.get('outfit', tid))}.mp4"
        tasks.append(task)

    if problems:
        for msg in problems:
            print(f"✗ {msg}", file=sys.stderr)
        return 2

    batch = {"defaults": defaults, "tasks": tasks}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")

    failures = 0
    for task in tasks:
        _, errors, _ = build_body(task, defaults, probe=False)
        if errors:
            failures += 1
            print(f"✗ {task['id']}: {'; '.join(errors)}", file=sys.stderr)

    duration = defaults.get("duration", "5")
    resolution = defaults.get("resolution", "1080P")
    total_seconds = len(tasks) * (int(duration) if str(duration).isdigit() else 0)
    print(f"已生成 {len(tasks)} 个任务 -> {out_path}")
    print(f"规格：{resolution} / {duration}s / 比例 {defaults.get('aspectRatio', 'adaptive')}"
          f" / 音轨 {defaults.get('audio')} / 智能改写 {defaults.get('promptExtend')}")
    print(f"定妆图：{image or '（未配置 image，任务里没有 imageUrls）'}")
    if total_seconds:
        print(f"输出总时长：{total_seconds}s（约 {total_seconds / 60:.1f} 分钟成片）")
    if args.unit_price:
        print(f"按单价 ¥{args.unit_price} 估算：{len(tasks)} 条 ≈ ¥{args.unit_price * len(tasks):,.2f}")
    print(f"校验：{'全部通过' if failures == 0 else f'{failures} 条不通过'}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
