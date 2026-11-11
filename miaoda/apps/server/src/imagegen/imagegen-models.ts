/**
 * 生图模型表：两种协议、两套尺寸口径。`app/imggen/models.py` 的 TS 版（逐行对应）。
 *
 * - `PROVIDER_IMAGES`：老张的 OpenAI Images 协议（`images/generations`、`images/edits`），
 *   请求体里的 `size` 是像素（`2048x2048`），成品由 processing 按本地规格裁剪。
 * - `PROVIDER_GEMINI`：Google Gemini 原生协议（`{model}:generateContent`），
 *   `size` 是「比例@档位」（`4:5@2K`），分辨率与比例由模型直接给出，本地不再裁剪。
 *
 * 两个协议不能混用：Images 客户端认 `data[].b64_json`，Gemini 的图在
 * `candidates[].content.parts[].inlineData`，所以 manager 按 provider 分派。
 *
 * 函数名保持 Python 原名（snake_case），方便和 Python 侧逐行对照。
 */

export const PROVIDER_IMAGES = 'images';
export const PROVIDER_GEMINI = 'gemini';

/** 提示词上限（`manager.py` 的 PROMPT_LIMIT）。 */
export const IMAGE_PROMPT_LIMIT = 4000;
/** 单次最少 1 张、最多 8 张。 */
export const IMAGE_COUNT_CAP = 8;
/** 参考图上限（与上传槽位一致：1 张定妆图 + 9 张细节图）。 */
export const IMAGE_REFERENCE_CAP = 10;
/** 同时跑几个生图任务（Python 的 `max_inflight`，钳在 1–8）。 */
export const IMAGE_INFLIGHT_CAP = 8;

export interface ImageModelSpec {
  model_id: string;
  label: string;
  price_usd: string;
  provider: string;
}

/** Gemini 的尺寸是「比例 + 档位」的组合，与像素规格不是一回事。 */
export interface GeminiSizeSpec {
  value: string;
  label: string;
  aspect_ratio: string;
  image_size: string;
}

// https://docs.laozhang.ai/api-capabilities/gpt-image-2
// 2.5 / 2.6 VIP: $0.03/image, PNG, quality up to high / xhigh+max (checked 2026-11-09).
// https://docs.laozhang.ai/api-capabilities/nano-banana-image
export const IMAGE_MODEL_SPECS: readonly ImageModelSpec[] = [
  { model_id: 'gpt-image-2-vip', label: 'GPT Image 2 VIP｜$0.03/张', price_usd: '0.03', provider: PROVIDER_IMAGES },
  { model_id: 'gpt-image-2.5-flare-vip', label: 'GPT Image 2.5 Flare VIP｜$0.03/张', price_usd: '0.03', provider: PROVIDER_IMAGES },
  { model_id: 'gpt-image-2.5-sunburst-vip', label: 'GPT Image 2.5 Sunburst VIP｜$0.03/张', price_usd: '0.03', provider: PROVIDER_IMAGES },
  { model_id: 'gpt-image-2.6-flare-vip', label: 'GPT Image 2.6 Flare VIP｜$0.03/张', price_usd: '0.03', provider: PROVIDER_IMAGES },
  { model_id: 'gpt-image-2.6-sunburst-vip', label: 'GPT Image 2.6 Sunburst VIP｜$0.03/张', price_usd: '0.03', provider: PROVIDER_IMAGES },
  // Nano Banana 2.1：1K–4K、最多 14 张参考图，$0.045/次（任意分辨率同价）。
  { model_id: 'gemini-nano-banana-2.1', label: 'Nano Banana 2.1｜$0.045/张', price_usd: '0.045', provider: PROVIDER_GEMINI },
];

export const IMAGE_MODEL_IDS: readonly string[] = IMAGE_MODEL_SPECS.map((spec) => spec.model_id);

const _IMAGE_MODEL_BY_ID = new Map(IMAGE_MODEL_SPECS.map((spec) => [spec.model_id, spec]));

export const MODEL_BY_ID = _IMAGE_MODEL_BY_ID;

// Nano Banana 只公开 6 个常用比例 × 3 档分辨率；其余比例（2:3、21:9 等）够用即可，
// 需要时在这里加一行就会出现在页面上。
const GEMINI_RATIO_LABELS: ReadonlyArray<readonly [string, string]> = [
  ['1:1', '方形 1:1'],
  ['4:5', '竖版 4:5'],
  ['3:4', '竖版 3:4'],
  ['2:3', '竖版 2:3'],
  ['9:16', '竖版 9:16'],
  ['16:9', '横版 16:9'],
];

export const GEMINI_RESOLUTIONS = ['1K', '2K', '4K'] as const;

export const GEMINI_SIZE_SPECS: readonly GeminiSizeSpec[] = [
  { value: 'auto', label: '自动｜由模型决定', aspect_ratio: '', image_size: '' },
  ...GEMINI_RATIO_LABELS.flatMap(([ratio, label]) =>
    GEMINI_RESOLUTIONS.map((resolution) => ({
      value: `${ratio}@${resolution}`,
      label: `${label}｜${resolution}`,
      aspect_ratio: ratio,
      image_size: resolution as string,
    })),
  ),
];

export const GEMINI_SIZE_BY_VALUE = new Map(GEMINI_SIZE_SPECS.map((spec) => [spec.value, spec]));

// https://docs.laozhang.ai/en/api-capabilities/gpt-image-2
// https://docs.laozhang.ai/en/api-capabilities/gpt-image-2-5
// 2.6 VIP: $0.03/image, PNG, quality up to high (checked 2026-11-09).
export const QUALITY_LABELS: Record<string, string> = {
  auto: '自动｜由模型决定',
  low: '低｜low',
  medium: '中｜medium',
  high: '高｜high',
  xhigh: '超高｜xhigh',
  max: '最高｜max',
};

export function image_model_spec(model_id: unknown): ImageModelSpec | null {
  return _IMAGE_MODEL_BY_ID.get(String(model_id ?? '').trim()) ?? null;
}

export function image_model_provider(model_id: unknown): string {
  return image_model_spec(model_id)?.provider ?? '';
}

export function is_gemini_model(model_id: unknown): boolean {
  return image_model_provider(model_id) === PROVIDER_GEMINI;
}

export function image_model_label(model_id: unknown): string {
  return image_model_spec(model_id)?.label ?? String(model_id ?? '');
}

export function image_model_price_label(model_id: unknown): string {
  const spec = image_model_spec(model_id);
  if (spec === null) return '单张价格：未知';
  return `单张价格：US$${spec.price_usd}（美元）`;
}

/** 历史按量模型只用于展示；新分支必须切换到对应 VIP 型号。 */
export function per_call_model_for_history(model_id: string): string {
  if (IMAGE_MODEL_IDS.includes(model_id)) return model_id;
  if (model_id.startsWith('gpt-image-2.6-flare')) return 'gpt-image-2.6-flare-vip';
  if (model_id.startsWith('gpt-image-2.6-sunburst')) return 'gpt-image-2.6-sunburst-vip';
  if (model_id.startsWith('gpt-image-2.5-flare')) return 'gpt-image-2.5-flare-vip';
  if (model_id.startsWith('gpt-image-2.5-sunburst')) return 'gpt-image-2.5-sunburst-vip';
  return 'gpt-image-2-vip';
}

/** 只有 Images 协议的老张型号吃 `size`/`quality` 参数。 */
export function supports_image_parameters(model: string): boolean {
  return image_model_provider(model) === PROVIDER_IMAGES;
}

export function image_quality_options(model: string): readonly string[] {
  const spec = image_model_spec(model);
  if (spec === null || spec.provider !== PROVIDER_IMAGES) {
    // Nano Banana 没有 quality 概念（思考级别不暴露），前端会把下拉置灰。
    return ['auto'];
  }
  if (model.startsWith('gpt-image-2.5-')) return ['auto', 'low', 'medium', 'high', 'xhigh', 'max'];
  return ['auto', 'low', 'medium', 'high'];
}

/** Images 协议的附加参数；Gemini 协议的不走这里（见 imagegen-gemini.ts）。 */
export function image_request_parameters(model: string, size: string, quality: string): Record<string, string> {
  const spec = image_model_spec(model);
  if (spec === null) throw new Error('仅允许使用 $0.03/次的 VIP 图片模型，禁止按量计费模型');
  if (!image_quality_options(model).includes(quality)) throw new Error('所选模型不支持此图片质量');
  if (spec.provider === PROVIDER_GEMINI) {
    if (!GEMINI_SIZE_BY_VALUE.has(size)) throw new Error('所选模型不支持这个输出尺寸');
    return {};
  }
  const parameters: Record<string, string> = {};
  if (size !== 'auto') parameters.size = size;
  if (quality !== 'auto') parameters.quality = quality;
  return parameters;
}

// ---------------------------------------------------------------- 配置数值钳制

/** Python `_as_int`：int(str(value).strip())，失败用默认值。 */
export function _as_int(value: unknown, fallback: number): number {
  const text = String(value ?? '').trim();
  if (!/^[+-]?\d+$/.test(text)) return fallback;
  const parsed = Number(text);
  return Number.isSafeInteger(parsed) ? parsed : fallback;
}

export function max_count(value: unknown, fallback = 4): number {
  return Math.max(1, Math.min(IMAGE_COUNT_CAP, _as_int(value, fallback)));
}

export function max_references(value: unknown, fallback = IMAGE_REFERENCE_CAP): number {
  return Math.max(0, Math.min(IMAGE_REFERENCE_CAP, _as_int(value, fallback)));
}

export function max_inflight(value: unknown, fallback = 3): number {
  return Math.max(1, Math.min(IMAGE_INFLIGHT_CAP, _as_int(value, fallback)));
}

export function clamp_timeout(value: unknown, fallback = 300): number {
  return Math.max(30, Math.min(900, _as_int(value, fallback)));
}

// ---------------------------------------------------------------- 下拉选项

export function _quality_pairs(model: string): Array<[string, string]> {
  return image_quality_options(model).map((value) => [value, QUALITY_LABELS[value] ?? value]);
}

export type SizeOption = { value: string; label: string };

type SizeOptionsProvider = (model: string) => Array<SizeOption>;

let images_size_options: SizeOptionsProvider | null = null;

/**
 * 由 `imagegen-processing.ts` 在模块加载时注册：Images 协议的 17 档像素规格维护在那边，
 * 而 processing 又需要本文件的 `supports_image_parameters`，两边直接 import 会成环。
 */
export function _register_size_options(provider: SizeOptionsProvider): void {
  images_size_options = provider;
}

/**
 * 尺寸下拉：Images 协议给像素规格（17 档，按型号裁剪），Gemini 给「比例@档位」。
 */
export function _size_options(model: string): SizeOption[] {
  if (is_gemini_model(model)) {
    return GEMINI_SIZE_SPECS.map((spec) => ({ value: spec.value, label: spec.label }));
  }
  if (images_size_options === null) {
    // 走到这里说明用到了 models 却没人 import processing；宁可报错也不要给出假选项。
    throw new Error('生图尺寸表未注册：请先加载 imagegen-processing.ts');
  }
  return images_size_options(model);
}
