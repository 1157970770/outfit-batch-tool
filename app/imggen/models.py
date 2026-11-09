from dataclasses import dataclass

@dataclass(frozen=True)
class ImageModelSpec:
    model_id: str
    label: str
    price_usd: str = "0.03"


IMAGE_MODEL_SPECS = (
    ImageModelSpec("gpt-image-2-vip", "GPT Image 2 VIP｜$0.03/张"),
    ImageModelSpec(
        "gpt-image-2.5-flare-vip",
        "GPT Image 2.5 Flare VIP｜$0.03/张",
    ),
    ImageModelSpec(
        "gpt-image-2.5-sunburst-vip",
        "GPT Image 2.5 Sunburst VIP｜$0.03/张",
    ),
    ImageModelSpec("gpt-image-2.6-flare-vip", "GPT Image 2.6 Flare VIP｜$0.03/张"),
    ImageModelSpec("gpt-image-2.6-sunburst-vip", "GPT Image 2.6 Sunburst VIP｜$0.03/张"),
)
IMAGE_MODEL_IDS = tuple(spec.model_id for spec in IMAGE_MODEL_SPECS)


def image_model_label(model_id: str) -> str:
    for spec in IMAGE_MODEL_SPECS:
        if spec.model_id == model_id:
            return spec.label
    return model_id


def image_model_price_label(model_id: str) -> str:
    for spec in IMAGE_MODEL_SPECS:
        if spec.model_id == model_id:
            return f"单张价格：US${spec.price_usd}（美元）"
    return "单张价格：未知"


def per_call_model_for_history(model_id: str) -> str:
    """历史按量模型只用于展示；新分支必须切换到对应 VIP 型号。"""
    if model_id in IMAGE_MODEL_IDS:
        return model_id
    if model_id.startswith("gpt-image-2.6-flare"):
        return "gpt-image-2.6-flare-vip"
    if model_id.startswith("gpt-image-2.6-sunburst"):
        return "gpt-image-2.6-sunburst-vip"
    if model_id.startswith("gpt-image-2.5-flare"):
        return "gpt-image-2.5-flare-vip"
    if model_id.startswith("gpt-image-2.5-sunburst"):
        return "gpt-image-2.5-sunburst-vip"
    return "gpt-image-2-vip"


# https://docs.laozhang.ai/en/api-capabilities/gpt-image-2
# https://docs.laozhang.ai/en/api-capabilities/gpt-image-2-5
# 2.6 VIP: $0.03/image, PNG, quality up to high (checked 2026-11-09).
# https://blog.laozhang.ai/en/posts/gpt-image-2-6-pricing
QUALITY_LABELS = {
    "auto": "自动｜由模型决定",
    "low": "低｜low",
    "medium": "中｜medium",
    "high": "高｜high",
    "xhigh": "超高｜xhigh",
    "max": "最高｜max",
}


def supports_image_parameters(model: str) -> bool:
    return model in IMAGE_MODEL_IDS


def image_quality_options(model: str) -> tuple[str, ...]:
    if not supports_image_parameters(model):
        return ("auto",)
    if model.startswith("gpt-image-2.5-"):
        return ("auto", "low", "medium", "high", "xhigh", "max")
    return ("auto", "low", "medium", "high")


def image_request_parameters(
    model: str, size: str, quality: str,
) -> dict[str, str]:
    if model not in IMAGE_MODEL_IDS:
        raise ValueError("仅允许使用 $0.03/次的 VIP 图片模型，禁止按量计费模型")
    if quality not in image_quality_options(model):
        raise ValueError("所选模型不支持此图片质量")
    parameters = {}
    if size != "auto":
        parameters["size"] = size
    if quality != "auto":
        parameters["quality"] = quality
    return parameters
