/**
 * 自由视频：选项下发与请求校验。
 *
 * 与 `app/videogen/models.py` 一一对应：常量取自 `wan-client`（等同 Python 从
 * `app.wan_api` 导入），中文文案逐字照搬，前端拿到的字段名与顺序都不变。
 */
import { MAX_IMAGES, RATIOS, RESOLUTIONS } from '../wan/wan-client.service';

/** 一次最多生成几个分镜（每个分镜都是独立计费的云端任务）。 */
export { MAX_IMAGES, RATIOS, RESOLUTIONS };

export const MAX_COUNT = 4;
/** 提示词上限与官方一致。 */
export const PROMPT_LIMIT = 20000;
/** 反向提示词上限。 */
export const NEGATIVE_LIMIT = 2000;

export const MAX_VIDEOS = 5;
export const MAX_AUDIOS = 5;
export const MAX_VIDEO_TOTAL = 15.0;
export const MAX_INPUT_PLUS_OUTPUT = 30.0;
export const MAX_SEED = 2147483647;

export type VideoGenMode = 'auto' | 'reference' | 'edit';
export type VideoGenEngineMode = 'reference' | 'edit';

export const MODES_UI: ReadonlyArray<{ value: VideoGenMode; label: string; hint: string }> = [
  { value: 'auto', label: '自动判断', hint: '有参考视频就按视频编辑，没有就按参考生视频' },
  {
    value: 'reference',
    label: '参考生视频',
    hint: '参考图 / 参考视频 / 参考音频 → 新视频；无素材即文生视频',
  },
  { value: 'edit', label: '视频编辑', hint: '在原视频上改风格 / 换主体，必须至少一段参考视频' },
];

export const RESOLUTION_LABELS: Record<string, string> = {
  '1080P': '1080P（最清晰）',
  '720P': '720P（均衡）',
  '480P': '480P（最快）',
};

export const RATIO_LABELS: Record<string, string> = {
  '16:9': '16:9 横屏',
  '9:16': '9:16 竖屏',
  '4:3': '4:3 横屏',
  '3:4': '3:4 竖屏',
  '1:1': '1:1 方形',
  '21:9': '21:9 宽银幕',
  adaptive: '跟随参考视频（仅视频编辑）',
};

export const DURATION_CHOICES: ReadonlyArray<{ value: number; label: string }> = [
  { value: 5, label: '5 秒' },
  { value: 7, label: '7 秒' },
  { value: 10, label: '10 秒' },
  { value: 15, label: '15 秒' },
  { value: -1, label: '自动（视频编辑智能时长）' },
];

/** 可直接展示给用户的生产错误；status 决定 HTTP 码。 */
export class VideoGenError extends Error {
  readonly status: number;

  constructor(message: string, status = 400) {
    super(message);
    this.name = 'VideoGenError';
    this.status = Math.trunc(status);
  }
}

/** 本机时间（与网页端其它列表一致的展示格式）。 */
export function nowText(date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

export interface VideoGenOptions {
  configured: boolean;
  reason: string;
  model: string;
  provider: string;
  cos_enabled: boolean;
  modes: Array<{ value: string; label: string; hint: string }>;
  resolutions: Array<{ value: string; label: string }>;
  ratios: Array<{ value: string; label: string }>;
  durations: Array<{ value: number; label: string }>;
  defaults: Record<string, unknown>;
  limits: Record<string, number>;
  modes_engine: string[];
}

/** 下发给前端的全部选项与限制（字段与 Python `options()` 一致）。 */
export function buildOptions(input: {
  configured: boolean;
  reason: string;
  model: string;
  provider: string;
  cosEnabled: boolean;
}): VideoGenOptions {
  return {
    configured: Boolean(input.configured),
    reason: input.reason,
    model: input.model,
    provider: input.provider,
    cos_enabled: Boolean(input.cosEnabled),
    modes: MODES_UI.map((item) => ({ value: item.value, label: item.label, hint: item.hint })),
    resolutions: RESOLUTIONS.map((key) => ({ value: key, label: RESOLUTION_LABELS[key] || key })),
    ratios: RATIOS.map((key) => ({ value: key, label: RATIO_LABELS[key] || key })),
    durations: DURATION_CHOICES.map((item) => ({ value: item.value, label: item.label })),
    defaults: {
      mode: 'auto',
      resolution: '1080P',
      ratio: '16:9',
      duration: 5,
      count: 1,
      prompt_extend: true,
      watermark: false,
      audio: false,
    },
    limits: {
      max_count: MAX_COUNT,
      max_images: MAX_IMAGES,
      max_videos: MAX_VIDEOS,
      max_audios: MAX_AUDIOS,
      prompt_limit: PROMPT_LIMIT,
      negative_limit: NEGATIVE_LIMIT,
      video_max_seconds: MAX_VIDEO_TOTAL,
      video_max_mb: 100,
      audio_max_mb: 15,
      image_max_mb: 20,
    },
    modes_engine: ['edit', 'reference'],
  };
}

function asInt(value: unknown, field: string, fallback: number): number {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') throw new VideoGenError(`${field}需要填写整数。`);
  const text = String(value).trim();
  if (!/^[+-]?\d+$/.test(text)) throw new VideoGenError(`${field}需要填写整数。`);
  const parsed = Number.parseInt(text, 10);
  if (!Number.isFinite(parsed)) throw new VideoGenError(`${field}需要填写整数。`);
  return parsed;
}

export function asBool(value: unknown, fallback = false): boolean {
  if (value === null || value === undefined) return fallback;
  if (typeof value === 'boolean') return value;
  return ['1', 'true', 'yes', 'on', '是'].includes(String(value).trim().toLowerCase());
}

export interface VideoMetaLike {
  duration?: number | null;
}

export interface ValidatedRequest {
  prompt: string;
  negative_prompt: string;
  mode_choice: VideoGenMode;
  mode: VideoGenEngineMode;
  resolution: string;
  ratio: string;
  duration: number;
  count: number;
  seed: number | null;
  audio: boolean;
  prompt_extend: boolean;
  watermark: boolean;
  video_seconds: number | null;
}

/**
 * 把前端请求规整成任务字段；不合法直接抛 VideoGenError。
 * `videoMetas` 是已校验过的上传记录（含 duration），用来挡「输入+输出 ≤30 秒」。
 */
export function validateRequest(
  payload: Record<string, unknown>,
  opts: { imageCount: number; videoMetas: VideoMetaLike[]; audioCount: number },
): ValidatedRequest {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new VideoGenError('请求格式不正确。');
  }

  const prompt = String(payload.prompt ?? '').trim();
  if (!prompt) throw new VideoGenError('请填写提示词，描述你要生成的视频。');
  if (prompt.length > PROMPT_LIMIT) {
    throw new VideoGenError(`提示词最多 ${PROMPT_LIMIT} 字（当前 ${prompt.length} 字）。`);
  }

  const negative = String(payload.negative_prompt ?? '').trim();
  if (negative.length > NEGATIVE_LIMIT) {
    throw new VideoGenError(`反向提示词最多 ${NEGATIVE_LIMIT} 字。`);
  }

  const modeChoice = String(payload.mode ?? 'auto').trim().toLowerCase();
  if (!['auto', 'reference', 'edit'].includes(modeChoice)) {
    throw new VideoGenError('生成方式无效。');
  }
  const hasVideos = opts.videoMetas.length > 0;
  const mode: VideoGenEngineMode =
    (modeChoice === 'edit' || modeChoice === 'auto') && hasVideos ? 'edit' : 'reference';
  if (modeChoice === 'edit' && !hasVideos) {
    throw new VideoGenError('「视频编辑」至少需要一段参考视频；只给图片请用「参考生视频」。');
  }

  const resolution = String(payload.resolution ?? '1080P').trim().toUpperCase();
  if (!(RESOLUTIONS as readonly string[]).includes(resolution)) {
    throw new VideoGenError(`分辨率只能是 ${RESOLUTIONS.join(' / ')}。`);
  }

  let ratio = String(payload.ratio ?? '').trim().toLowerCase();
  ratio = ratio || (mode === 'edit' ? 'adaptive' : '16:9');
  if (!(RATIOS as readonly string[]).includes(ratio)) {
    throw new VideoGenError(`画面比例只能是 ${RATIOS.join(' / ')}。`);
  }
  if (mode !== 'edit' && ratio === 'adaptive') {
    throw new VideoGenError('参考生视频需要具体画面比例（如 16:9 / 9:16 / 1:1）。');
  }

  let duration = asInt(payload.duration, '时长', 5);
  if (duration !== -1 && !(duration >= 2 && duration <= 30)) {
    throw new VideoGenError('时长只能是 -1（自动）或 2–30 的整数。');
  }
  if (mode !== 'edit' && duration === -1) {
    // -1 是视频编辑的「智能时长」；参考生视频没有原片可参照，按 5 秒走。
    duration = 5;
  }

  const count = asInt(payload.count, '生成数量', 1);
  if (!(count >= 1 && count <= MAX_COUNT)) {
    throw new VideoGenError(`生成数量需在 1–${MAX_COUNT} 之间。`);
  }

  const rawSeed = payload.seed;
  let seed: number | null = null;
  if (rawSeed !== null && rawSeed !== undefined && String(rawSeed).trim() !== '') {
    seed = asInt(rawSeed, '随机种子', -1);
    if (seed !== -1 && !(seed >= 0 && seed <= MAX_SEED)) {
      throw new VideoGenError(`随机种子需在 -1（随机）或 0–${MAX_SEED} 之间。`);
    }
  }

  if (opts.imageCount > MAX_IMAGES) throw new VideoGenError(`参考图最多 ${MAX_IMAGES} 张。`);
  if (opts.videoMetas.length > MAX_VIDEOS) throw new VideoGenError(`参考视频最多 ${MAX_VIDEOS} 段。`);
  if (opts.audioCount > MAX_AUDIOS) throw new VideoGenError(`参考音频最多 ${MAX_AUDIOS} 段。`);

  const known = opts.videoMetas
    .map((meta) => meta?.duration)
    .filter((value): value is number => typeof value === 'number' && value > 0);
  const totalSeconds =
    opts.videoMetas.length > 0 && known.length === opts.videoMetas.length
      ? Math.round(known.reduce((sum, value) => sum + value, 0) * 100) / 100
      : null;
  if (totalSeconds !== null) {
    if (totalSeconds > MAX_VIDEO_TOTAL) {
      throw new VideoGenError(
        `参考视频总时长 ${fmtNum(totalSeconds)}s，超过 ${fmtNum(MAX_VIDEO_TOTAL)} 秒上限。`,
      );
    }
    if (duration !== -1 && totalSeconds + duration > MAX_INPUT_PLUS_OUTPUT) {
      throw new VideoGenError(
        `参考视频 ${fmtNum(totalSeconds)}s + 输出 ${duration}s = ` +
          `${fmtNum(totalSeconds + duration)}s，超过 ${fmtNum(MAX_INPUT_PLUS_OUTPUT)} 秒上限；` +
          '请缩短参考视频或输出时长。',
      );
    }
  }

  return {
    prompt,
    negative_prompt: negative,
    mode_choice: modeChoice as VideoGenMode,
    mode,
    resolution,
    ratio,
    duration,
    count,
    seed,
    audio: asBool(payload.audio, false),
    prompt_extend: asBool(payload.prompt_extend, true),
    watermark: asBool(payload.watermark, false),
    video_seconds: totalSeconds,
  };
}

/** 等价 Python 的 `{value:g}`：整数不带小数点。 */
export function fmtNum(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(2)));
}
