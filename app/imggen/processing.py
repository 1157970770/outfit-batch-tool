from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from .models import supports_image_parameters


FOUR_FIVE_SIZE = "4:5"
FOUR_FIVE_API_SIZE = "2048x2048"
FOUR_FIVE_NATIVE_API_SIZE = "2048x2560"


@dataclass(frozen=True)
class OutputSpec:
    value: str
    label: str
    api_size: str
    aspect_ratio: tuple[int, int] | None = None
    target_size: tuple[int, int] | None = None


OUTPUT_SPECS = (
    OutputSpec("auto", "自动｜由模型决定", "auto"),
    OutputSpec("800x800", "方形 1:1｜800 × 800", "1024x1024", (1, 1), (800, 800)),
    OutputSpec("1024x1024", "方形 1:1｜1024 × 1024", "1024x1024", (1, 1)),
    OutputSpec(
        "1800x1800",
        "方形 1:1｜1800 × 1800",
        "2048x2048",
        (1, 1),
        (1800, 1800),
    ),
    OutputSpec("2048x2048", "方形 1:1｜2048 × 2048", "2048x2048", (1, 1)),
    OutputSpec(
        "1125x1500",
        "竖版 3:4｜1125 × 1500",
        "2048x2048",
        (3, 4),
        (1125, 1500),
    ),
    OutputSpec(
        "1800x2400",
        "竖版 3:4｜1800 × 2400",
        "2048x2048",
        (3, 4),
        (1800, 2400),
    ),
    OutputSpec(FOUR_FIVE_SIZE, "竖版 4:5｜高分辨率", FOUR_FIVE_API_SIZE, (4, 5)),
    OutputSpec("1024x1536", "竖版 2:3｜1024 × 1536", "1024x1536", (2, 3)),
    OutputSpec("2160x3840", "竖版 9:16｜2160 × 3840", "2160x3840", (9, 16)),
    OutputSpec("1536x1024", "横版 3:2｜1536 × 1024", "1536x1024", (3, 2)),
    OutputSpec("2048x1152", "横版 16:9｜2048 × 1152", "2048x1152", (16, 9)),
    OutputSpec("3840x2160", "横版 16:9｜3840 × 2160", "3840x2160", (16, 9)),
    OutputSpec("2560x1440", "横版 16:9｜2560 × 1440", "2560x1440", (16, 9)),
    OutputSpec("1440x2560", "竖版 9:16｜1440 × 2560", "1440x2560", (9, 16)),
    OutputSpec("1440x960", "横版 3:2｜1440 × 960", "1440x960", (3, 2)),
    OutputSpec("1536x864", "横版 16:9｜1536 × 864", "1536x864", (16, 9)),
)
OUTPUT_SPEC_BY_VALUE = {spec.value: spec for spec in OUTPUT_SPECS}
SIZE_OPTIONS = tuple(spec.value for spec in OUTPUT_SPECS)


def output_specs_for_model(
    model: str,
) -> tuple[OutputSpec, ...]:
    if not supports_image_parameters(model):
        return tuple(
            spec for spec in OUTPUT_SPECS
            if spec.value == "auto" or requires_local_output_processing(spec.value)
        )
    if model.startswith("gpt-image-2.5-"):
        return OUTPUT_SPECS
    # GPT Image 2 lists common sizes; arbitrary custom API sizes are documented for 2.5.
    return OUTPUT_SPECS[:-4]


def output_spec_label(requested_size: str) -> str:
    """返回供任务卡和历史记录显示的友好规格名称。"""
    spec = OUTPUT_SPEC_BY_VALUE.get(requested_size)
    return spec.label if spec is not None else requested_size


def requires_local_output_processing(requested_size: str) -> bool:
    """判断规格是否需要在 API 返回后执行本地裁剪或缩放。"""
    if requested_size == FOUR_FIVE_SIZE:
        return True
    spec = OUTPUT_SPEC_BY_VALUE.get(requested_size)
    return spec is not None and spec.target_size is not None


def api_size_for_output(requested_size: str, model: str = "") -> str:
    """把本地成品规格转换成图片 API 支持的请求尺寸。"""
    if requested_size == FOUR_FIVE_SIZE and model.startswith("gpt-image-2.5-"):
        return FOUR_FIVE_NATIVE_API_SIZE
    spec = OUTPUT_SPEC_BY_VALUE.get(requested_size)
    return spec.api_size if spec is not None else requested_size


def crop_and_resize_to_target(
    image_bytes: bytes,
    target_size: tuple[int, int],
) -> bytes:
    """居中裁剪到目标比例后缩放，不拉伸图像。"""
    target_width, target_height = target_size
    if target_width < 1 or target_height < 1:
        raise ValueError("目标图片尺寸无效")

    with Image.open(BytesIO(image_bytes)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        if width < 1 or height < 1:
            raise ValueError("API 返回的图片尺寸无效，无法处理为目标规格")

        if width * target_height >= height * target_width:
            # 方图或较宽图片只裁左右，完整保留人物所在的上下边缘。
            crop_width = min(width, max(1, round(height * target_width / target_height)))
            left = (width - crop_width) // 2
            cropped = image.crop((left, 0, left + crop_width, height))
        else:
            # 接口意外返回过窄图片时，使用上下居中裁剪兜底。
            crop_height = min(height, max(1, round(width * target_height / target_width)))
            top = (height - crop_height) // 2
            cropped = image.crop((0, top, width, top + crop_height))

        if cropped.size != target_size:
            cropped = cropped.resize(target_size, Image.Resampling.LANCZOS)

        output = BytesIO()
        cropped.save(output, format="PNG")
        return output.getvalue()


def finalize_output_image(
    image_bytes: bytes,
    requested_size: str,
) -> tuple[bytes, tuple[int, int]]:
    """执行本地成品处理并返回最终真实像素。"""
    if requested_size == FOUR_FIVE_SIZE:
        return crop_to_four_five(image_bytes)

    spec = OUTPUT_SPEC_BY_VALUE.get(requested_size)
    if spec is None or spec.target_size is None:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            return image_bytes, image.size

    return crop_and_resize_to_target(image_bytes, spec.target_size), spec.target_size


def crop_to_four_five(image_bytes: bytes) -> tuple[bytes, tuple[int, int]]:
    """优先只裁左右并保留完整高度，输出精确 4:5 的 PNG。"""
    with Image.open(BytesIO(image_bytes)) as image:
        image.load()
        width, height = image.size
        if width < 4 or height < 5:
            raise ValueError("API 返回的图片尺寸太小，无法裁剪为 4:5")

        if width * 5 == height * 4:
            return image_bytes, (width, height)

        if width * 5 >= height * 4:
            # 方图或横图只裁左右，完整保留头顶和脚底所在的上下边缘。
            crop_width = min(width, max(1, round(height * 4 / 5)))
            left = (width - crop_width) // 2
            cropped = image.crop((left, 0, left + crop_width, height))
        else:
            # 极少数接口未按请求返回方图时，只能从上下居中裁切成 4:5。
            crop_height = min(height, max(1, round(width * 5 / 4)))
            top = (height - crop_height) // 2
            cropped = image.crop((0, top, width, top + crop_height))

        scale = min(cropped.width // 4, cropped.height // 5)
        if scale < 1:
            raise ValueError("API 返回的图片尺寸太小，无法裁剪为 4:5")
        output_size = (scale * 4, scale * 5)
        if cropped.size != output_size:
            cropped = cropped.resize(output_size, Image.Resampling.LANCZOS)

        output = BytesIO()
        cropped.save(output, format="PNG")
        return output.getvalue(), output_size
