/**
 * 本地成品处理：17 档像素规格 + 4:5 裁切。`app/imggen/processing.py` 的 TS 版。
 *
 * **只服务 Images 协议**：Gemini（Nano Banana）直出比例就是成品比例，这里不碰。
 * Pillow 的 `exif_transpose` / `LANCZOS` 对应 sharp 的 `.rotate()` / `kernel: 'lanczos3'`。
 */
import sharp from 'sharp';

import { _register_size_options, supports_image_parameters } from './imagegen-models';

export const FOUR_FIVE_SIZE = '4:5';
export const FOUR_FIVE_API_SIZE = '2048x2048';
export const FOUR_FIVE_NATIVE_API_SIZE = '2048x2560';

export interface OutputSpec {
  value: string;
  label: string;
  api_size: string;
  aspect_ratio?: [number, number];
  target_size?: [number, number];
}

export const OUTPUT_SPECS: readonly OutputSpec[] = [
  { value: 'auto', label: '自动｜由模型决定', api_size: 'auto' },
  { value: '800x800', label: '方形 1:1｜800 × 800', api_size: '1024x1024', aspect_ratio: [1, 1], target_size: [800, 800] },
  { value: '1024x1024', label: '方形 1:1｜1024 × 1024', api_size: '1024x1024', aspect_ratio: [1, 1] },
  { value: '1800x1800', label: '方形 1:1｜1800 × 1800', api_size: '2048x2048', aspect_ratio: [1, 1], target_size: [1800, 1800] },
  { value: '2048x2048', label: '方形 1:1｜2048 × 2048', api_size: '2048x2048', aspect_ratio: [1, 1] },
  { value: '1125x1500', label: '竖版 3:4｜1125 × 1500', api_size: '2048x2048', aspect_ratio: [3, 4], target_size: [1125, 1500] },
  { value: '1800x2400', label: '竖版 3:4｜1800 × 2400', api_size: '2048x2048', aspect_ratio: [3, 4], target_size: [1800, 2400] },
  { value: FOUR_FIVE_SIZE, label: '竖版 4:5｜高分辨率', api_size: FOUR_FIVE_API_SIZE, aspect_ratio: [4, 5] },
  { value: '1024x1536', label: '竖版 2:3｜1024 × 1536', api_size: '1024x1536', aspect_ratio: [2, 3] },
  { value: '2160x3840', label: '竖版 9:16｜2160 × 3840', api_size: '2160x3840', aspect_ratio: [9, 16] },
  { value: '1536x1024', label: '横版 3:2｜1536 × 1024', api_size: '1536x1024', aspect_ratio: [3, 2] },
  { value: '2048x1152', label: '横版 16:9｜2048 × 1152', api_size: '2048x1152', aspect_ratio: [16, 9] },
  { value: '3840x2160', label: '横版 16:9｜3840 × 2160', api_size: '3840x2160', aspect_ratio: [16, 9] },
  { value: '2560x1440', label: '横版 16:9｜2560 × 1440', api_size: '2560x1440', aspect_ratio: [16, 9] },
  { value: '1440x2560', label: '竖版 9:16｜1440 × 2560', api_size: '1440x2560', aspect_ratio: [9, 16] },
  { value: '1440x960', label: '横版 3:2｜1440 × 960', api_size: '1440x960', aspect_ratio: [3, 2] },
  { value: '1536x864', label: '横版 16:9｜1536 × 864', api_size: '1536x864', aspect_ratio: [16, 9] },
];

export const OUTPUT_SPEC_BY_VALUE = new Map(OUTPUT_SPECS.map((spec) => [spec.value, spec]));

export const SIZE_OPTIONS: readonly string[] = OUTPUT_SPECS.map((spec) => spec.value);

export function output_specs_for_model(model: string): readonly OutputSpec[] {
  if (!supports_image_parameters(model)) {
    return OUTPUT_SPECS.filter((spec) => spec.value === 'auto' || requires_local_output_processing(spec.value));
  }
  if (model.startsWith('gpt-image-2.5-')) return OUTPUT_SPECS;
  // GPT Image 2 lists common sizes; arbitrary custom API sizes are documented for 2.5.
  return OUTPUT_SPECS.slice(0, -4);
}

// 把 Images 的尺寸表注册回 models（models 不能反向 import 本文件，见 `_register_size_options`）。
_register_size_options((model) =>
  output_specs_for_model(model).map((spec) => ({ value: spec.value, label: spec.label })),
);

/** 返回供任务卡和历史记录显示的友好规格名称。 */
export function output_spec_label(requested_size: string): string {
  return OUTPUT_SPEC_BY_VALUE.get(requested_size)?.label ?? requested_size;
}

/** 判断规格是否需要在 API 返回后执行本地裁剪或缩放。 */
export function requires_local_output_processing(requested_size: string): boolean {
  if (requested_size === FOUR_FIVE_SIZE) return true;
  const spec = OUTPUT_SPEC_BY_VALUE.get(requested_size);
  return spec !== undefined && spec.target_size !== undefined;
}

/** 把本地成品规格转换成图片 API 支持的请求尺寸。 */
export function api_size_for_output(requested_size: string, model = ''): string {
  if (requested_size === FOUR_FIVE_SIZE && model.startsWith('gpt-image-2.5-')) return FOUR_FIVE_NATIVE_API_SIZE;
  return OUTPUT_SPEC_BY_VALUE.get(requested_size)?.api_size ?? requested_size;
}

/** Python `round()` 是银行家舍入（.5 取偶）；JS 的 Math.round 一律进位，必须自己实现。 */
function pyRound(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** 小于这个宽高就根本没法裁：与 Pillow 报同一句中文。 */
const TOO_SMALL_45 = 'API 返回的图片尺寸太小，无法裁剪为 4:5';
const CORRUPT = '接口返回的图片数据损坏或编码不正确';

interface Oriented {
  png: Buffer;
  width: number;
  height: number;
}

/** 解码 + 按 EXIF 转正；统一成 PNG 缓冲区，后面只用 sharp 的 extract/resize。 */
async function open_oriented(image_bytes: Buffer): Promise<Oriented> {
  try {
    const { data, info } = await sharp(image_bytes).rotate().png().toBuffer({ resolveWithObject: true });
    return { png: data, width: info.width, height: info.height };
  } catch {
    throw new Error(CORRUPT);
  }
}

async function crop_resize_png(
  png: Buffer,
  crop: { left: number; top: number; width: number; height: number },
  target: [number, number],
): Promise<Buffer> {
  let pipeline = sharp(png).extract(crop);
  if (crop.width !== target[0] || crop.height !== target[1]) {
    pipeline = pipeline.resize(target[0], target[1], { kernel: 'lanczos3' });
  }
  return pipeline.png().toBuffer();
}

/** 居中裁剪到目标比例后缩放，不拉伸图像。 */
export async function crop_and_resize_to_target(image_bytes: Buffer, target_size: [number, number]): Promise<Buffer> {
  const [target_width, target_height] = target_size;
  if (target_width < 1 || target_height < 1) throw new Error('目标图片尺寸无效');

  const { png, width, height } = await open_oriented(image_bytes);
  if (width < 1 || height < 1) throw new Error('API 返回的图片尺寸无效，无法处理为目标规格');

  let left = 0;
  let top = 0;
  let crop_width = width;
  let crop_height = height;
  if (width * target_height >= height * target_width) {
    // 方图或较宽图片只裁左右，完整保留人物所在的上下边缘。
    crop_width = Math.min(width, Math.max(1, pyRound((height * target_width) / target_height)));
    left = Math.floor((width - crop_width) / 2);
  } else {
    // 接口意外返回过窄图片时，使用上下居中裁剪兜底。
    crop_height = Math.min(height, Math.max(1, pyRound((width * target_height) / target_width)));
    top = Math.floor((height - crop_height) / 2);
  }

  return crop_resize_png(png, { left, top, width: crop_width, height: crop_height }, target_size);
}

/** 优先只裁左右并保留完整高度，输出精确 4:5 的 PNG。 */
export async function crop_to_four_five(image_bytes: Buffer): Promise<{ data: Buffer; width: number; height: number }> {
  const { png, width, height } = await open_oriented(image_bytes);
  if (width < 4 || height < 5) throw new Error(TOO_SMALL_45);
  if (width * 5 === height * 4) return { data: image_bytes, width, height };

  let left = 0;
  let top = 0;
  let crop_width = width;
  let crop_height = height;
  if (width * 5 >= height * 4) {
    // 方图或横图只裁左右，完整保留头顶和脚底所在的上下边缘。
    crop_width = Math.min(width, Math.max(1, pyRound((height * 4) / 5)));
    left = Math.floor((width - crop_width) / 2);
  } else {
    // 极少数接口未按请求返回方图时，只能从上下居中裁切成 4:5。
    crop_height = Math.min(height, Math.max(1, pyRound((width * 5) / 4)));
    top = Math.floor((height - crop_height) / 2);
  }

  // 缩到 4 的倍数 × 5 的倍数，保证输出严格 4:5（2048×2048 → 1636×2045）。
  const scale = Math.min(Math.floor(crop_width / 4), Math.floor(crop_height / 5));
  if (scale < 1) throw new Error(TOO_SMALL_45);
  const output_size: [number, number] = [scale * 4, scale * 5];
  const data = await crop_resize_png(png, { left, top, width: crop_width, height: crop_height }, output_size);
  return { data, width: output_size[0], height: output_size[1] };
}

/** 执行本地成品处理并返回最终真实像素。 */
export async function finalize_output_image(
  image_bytes: Buffer,
  requested_size: string,
): Promise<{ data: Buffer; width: number; height: number }> {
  if (requested_size === FOUR_FIVE_SIZE) return crop_to_four_five(image_bytes);

  const spec = OUTPUT_SPEC_BY_VALUE.get(requested_size);
  if (spec === undefined || spec.target_size === undefined) {
    // 不转码：原字节直接落盘，尺寸只用于侧车记录（与 Python 一致，不做 EXIF 转正）。
    const meta = await sharp(image_bytes)
      .metadata()
      .catch(() => {
        throw new Error(CORRUPT);
      });
    return { data: image_bytes, width: meta.width ?? 0, height: meta.height ?? 0 };
  }

  const data = await crop_and_resize_to_target(image_bytes, spec.target_size);
  return { data, width: spec.target_size[0], height: spec.target_size[1] };
}
