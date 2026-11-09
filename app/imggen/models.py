"""生图模型表：两种协议、两套尺寸口径。

- `PROVIDER_IMAGES`：老张的 OpenAI Images 协议（`images/generations`、`images/edits`），
  请求体里的 `size` 是像素（`2048x2048`），成品由 processing 按本地规格裁剪。
- `PROVIDER_GEMINI`：Google Gemini 原生协议（`{model}:generateContent`），
  `size` 是「比例@档位」（`4:5@2K`），分辨率与比例由模型直接给出，本地不再裁剪。

两个协议不能混用：Images 客户端认 `data[].b64_json`，Gemini 的图在
`candidates[].content.parts[].inlineData`，所以 manager 按 provider 分派。
"""

from dataclasses import dataclass

PROVIDER_IMAGES = "images"
PROVIDER_GEMINI = "gemini"


@dataclass(frozen=True)
class ImageModelSpec:
    model_id: str
    label: str
    price_usd: str = "0.03"
    provider: str = PROVIDER_IMAGES


@dataclass(frozen=True)
class GeminiSizeSpec:
    """Gemini 的尺寸是「比例 + 档位」的组合，与像素规格不是一回事。"""

    value: str
    label: str
    aspect_ratio: str
    image_size: str


# https://docs.laozhang.ai/api-capabilities/gpt-image-2
# 2.5 / 2.6 VIP: $0.03/image, PNG, quality up to high / xhigh+max (checked 2026-11-09).
# https://docs.laozhang.ai/api-capabilities/nano-banana-image
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
    # Nano Banana 2.1：1K–4K、14 种比例、最多 14 张参考图，$0.045/次（任意分辨率同价）。
    ImageModelSpec(
        "gemini-nano-banana-2.1",
        "Nano Banana 2.1｜$0.045/张",
        "0.045",
        PROVIDER_GEMINI,
    ),
)
IMAGE_MODEL_IDS = tuple(spec.model_id for spec in IMAGE_MODEL_SPECS)
_IMAGE_MODEL_BY_ID = {spec.model_id: spec for spec in IMAGE_MODEL_SPECS}

MODEL_BY_ID = _IMAGE_MODEL_BY_ID

# Nano Banana 只公开 6 个常用比例 × 3 档分辨率；其余比例（2:3、21:9 等）够用即可，
# 需要时在这里加一行就会出现在页面上。
_GEMINI_RATIO_LABELS = (
    ("1:1", "方形 1:1"),
    ("4:5", "竖版 4:5"),
    ("3:4", "竖版 3:4"),
    ("2:3", "竖版 2:3"),
    ("9:16", "竖版 9:16"),
    ("16:9", "横版 16:9"),
)
GEMINI_RESOLUTIONS = ("1K", "2K", "4K")

GEMINI_SIZE_SPECS = (
    GeminiSizeSpec("auto", "自动｜由模型决定", "", ""),
) + tuple(
    GeminiSizeSpec(f"{ratio}@{resolution}", f"{label}｜{resolution}", ratio, resolution)
    for ratio, label in _GEMINI_RATIO_LABELS
    for resolution in GEMINI_RESOLUTIONS
)
GEMINI_SIZE_BY_VALUE = {spec.value: spec for spec in GEMINI_SIZE_SPECS}


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


def image_model_spec(model_id: str) -> ImageModelSpec | None:
    return _IMAGE_MODEL_BY_ID.get(str(model_id or "").strip())


def image_model_provider(model_id: str) -> str:
    spec = image_model_spec(model_id)
    return spec.provider if spec is not None else ""


def is_gemini_model(model_id: str) -> bool:
    return image_model_provider(model_id) == PROVIDER_GEMINI


def image_model_label(model_id: str) -> str:
    spec = image_model_spec(model_id)
    return spec.label if spec is not None else model_id


def image_model_price_label(model_id: str) -> str:
    spec = image_model_spec(model_id)
    if spec is None:
        return "单张价格：未知"
    return f"单张价格：US${spec.price_usd}（美元）"


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


def supports_image_parameters(model: str) -> bool:
    """只有 Images 协议的老张型号吃 `size`/`quality` 参数。"""
    return image_model_provider(model) == PROVIDER_IMAGES


def image_quality_options(model: str) -> tuple[str, ...]:
    spec = image_model_spec(model)
    if spec is None or spec.provider != PROVIDER_IMAGES:
        # Nano Banana 没有 quality 概念（思考级别不暴露），前端会把下拉置灰。
        return ("auto",)
    if model.startswith("gpt-image-2.5-"):
        return ("auto", "low", "medium", "high", "xhigh", "max")
    return ("auto", "low", "medium", "high")


def image_request_parameters(
    model: str, size: str, quality: str,
) -> dict[str, str]:
    """Images 协议的附加参数；Gemini 协议的不走这里（见 gemini.py）。"""
    spec = image_model_spec(model)
    if spec is None:
        raise ValueError("仅允许使用 $0.03/次的 VIP 图片模型，禁止按量计费模型")
    if quality not in image_quality_options(model):
        raise ValueError("所选模型不支持此图片质量")
    if spec.provider == PROVIDER_GEMINI:
        if size not in GEMINI_SIZE_BY_VALUE:
            raise ValueError("所选模型不支持这个输出尺寸")
        return {}
    parameters = {}
    if size != "auto":
        parameters["size"] = size
    if quality != "auto":
        parameters["quality"] = quality
    return parameters
