/**
 * Gemini 原生协议客户端（Nano Banana）。`app/imggen/gemini.py` 的 TS 版。
 *
 * 与 Images 协议的三处关键差异：
 * 1. 端点不是 `images/*`，而是 `{root}/v1beta/models/{model}:generateContent`；
 * 2. `size` 是「比例@档位」（`4:5@2K`），直接放进 `generationConfig.imageConfig`；
 * 3. 图在 `candidates[].content.parts[].inlineData.data`（base64），且 HTTP 200 没图
 *    也会计费——所以**任何情况下都不自动重试**，把「本次可能已计费」写进错误文案。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

import { ImageApiError, _post_json, sanitize } from './imagegen-api';
import { GEMINI_SIZE_BY_VALUE } from './imagegen-models';

export const DEFAULT_TIMEOUT = 360;
export const MAX_INLINE_BYTES = 15 * 1024 * 1024;

const DATA_URL_RE = /data:(image\/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)/;

export interface GeminiCallOptions {
  /** `server/imagegen.json` 的 `gemini_base_url`，例如 https://api.laozhang.ai */
  base_url: string;
  model: string;
  quality?: string;
  timeout?: number;
}

/** 复制 Python `quote(model, safe=".-_")` 的口径。 */
function _quote_model(model: string): string {
  const text = String(model ?? '').trim();
  return Array.from(text)
    .map((ch) => (/[A-Za-z0-9._-]/.test(ch) ? ch : encodeURIComponent(ch)))
    .join('');
}

/** `https://host[/v1beta|/v1]` → `https://host/v1beta/models/<model>:generateContent`。 */
export function native_endpoint(base_url: string, model: string): string {
  const base = String(base_url ?? '').trim();
  if (!base) throw new ImageApiError('生图接口地址未配置，请检查 server/imagegen.json 的 base_url');

  let scheme = '';
  try {
    scheme = new URL(base).protocol.replace(':', '').toLowerCase();
  } catch {
    scheme = '';
  }
  if (scheme !== 'http' && scheme !== 'https') {
    throw new ImageApiError('生图接口地址必须以 http:// 或 https:// 开头');
  }

  let root = base.replace(/\/+$/, '');
  if (root.endsWith('/v1beta')) root = root.slice(0, -'/v1beta'.length);
  else if (root.endsWith('/v1')) root = root.slice(0, -'/v1'.length);

  const name = _quote_model(model);
  if (!name) throw new ImageApiError('未指定图片模型');
  return `${root}/v1beta/models/${name}:generateContent`;
}

/** `4:5@2K` → `['4:5', '2K']`；`auto`/空 → `['', '']`。 */
export function split_size(value: string): [string, string] {
  const text = String(value ?? '').trim();
  if (!text) return ['', ''];
  const spec = GEMINI_SIZE_BY_VALUE.get(text);
  if (spec === undefined) throw new ImageApiError(`不支持的输出规格：${text}`);
  return [spec.aspect_ratio, spec.image_size];
}

function _mime_type(file: string): string {
  const ext = path.extname(String(file ?? '')).toLowerCase();
  if (ext === '.jpg' || ext === '.jpeg') return 'image/jpeg';
  if (ext === '.webp') return 'image/webp';
  if (ext === '.bmp') return 'image/bmp';
  if (ext === '.gif') return 'image/gif';
  return 'image/png';
}

function _payload(
  prompt: string,
  image_paths: readonly string[],
  aspect_ratio: string,
  image_size: string,
): Record<string, unknown> {
  const parts: Array<Record<string, unknown>> = [{ text: prompt }];
  let total = 0;
  for (const file of image_paths) {
    const data = fs.readFileSync(file);
    total += data.length;
    parts.push({ inlineData: { mimeType: _mime_type(file), data: data.toString('base64') } });
  }
  if (total > MAX_INLINE_BYTES) {
    throw new ImageApiError(
      `参考图合计 ${(total / 1024 / 1024).toFixed(1)}MB，超过 15MB 内联上限，请减少张数或压缩后再试。`,
    );
  }

  const generationConfig: Record<string, unknown> = { responseModalities: ['IMAGE'] };
  if (aspect_ratio || image_size) {
    const imageConfig: Record<string, string> = {};
    if (aspect_ratio) imageConfig.aspectRatio = aspect_ratio;
    if (image_size) imageConfig.imageSize = image_size;
    generationConfig.imageConfig = imageConfig;
  }
  return { contents: [{ role: 'user', parts }], generationConfig };
}

/** 兼容 `data:` 前缀、换行、缺失 padding 的 base64 解码（Python validate=False）。 */
export function _decode_b64(value: unknown): Buffer {
  let text = String(value ?? '').trim();
  if (text.startsWith('data:')) text = text.slice(text.lastIndexOf(',') + 1);
  text = text.replace(/\s+/g, '').replace(/[^A-Za-z0-9+/=]/g, '');
  text += '='.repeat((4 - (text.length % 4)) % 4);
  if (!text) return Buffer.alloc(0);
  const data = Buffer.from(text, 'base64');
  if (data.length === 0 && text.replace(/=/g, '').length > 0) {
    throw new ImageApiError('接口返回的 base64 图片数据损坏');
  }
  return data;
}

/**
 * 从 `generateContent` 响应里取图：取**最后一张非 thought 图**，
 * 没有图就拼一条可执行的中文错误（含 blockReason / finishReason / 文本原因）。
 */
export function _extract_image(result: Record<string, unknown>): Buffer {
  const candidates = Array.isArray(result.candidates) ? result.candidates : [];
  let image: Buffer | null = null;
  const texts: string[] = [];

  for (const candidate of candidates) {
    if (candidate === null || typeof candidate !== 'object') continue;
    const content = (candidate as Record<string, unknown>).content;
    if (content === null || typeof content !== 'object') continue;
    const parts = (content as Record<string, unknown>).parts;
    if (!Array.isArray(parts)) continue;

    for (const part of parts) {
      if (part === null || typeof part !== 'object') continue;
      const item = part as Record<string, unknown>;
      const inline = (item.inlineData ?? item.inline_data) as Record<string, unknown> | undefined;
      if (inline !== null && typeof inline === 'object' && inline.data) {
        // 思考过程也会吐图，必须跳过，否则会把中间稿当成品。
        if (item.thought || inline.thought) continue;
        const data = _decode_b64(inline.data);
        if (data.length > 0) image = data;
      }
      const text = typeof item.text === 'string' ? item.text.trim() : '';
      if (text) texts.push(text);
    }
  }

  if (image) return image;

  for (const text of texts) {
    const match = DATA_URL_RE.exec(text);
    if (match) return _decode_b64(match[2]);
  }

  const feedback = result.promptFeedback as Record<string, unknown> | undefined;
  let reason = typeof feedback?.blockReason === 'string' ? feedback.blockReason : '';
  if (!reason) {
    const first = candidates[0] as Record<string, unknown> | undefined;
    reason = typeof first?.finishReason === 'string' ? first.finishReason : '';
  }
  let message = '模型没有返回图片';
  if (reason) message += `（${reason}）`;
  const detail = sanitize(texts.join(' '), 200);
  if (detail) message += `：${detail}`;
  message += '。提示词里要明确说「生成一张图片」；这次请求可能已被计费，请先改提示词再试，不要原样重复提交。';
  throw new ImageApiError(message);
}

async function _generate(
  prompt: string,
  size: string,
  image_paths: readonly string[],
  api_key: string,
  options: GeminiCallOptions,
): Promise<Buffer> {
  const [aspect_ratio, image_size] = split_size(size);
  const payload = _payload(prompt, image_paths, aspect_ratio, image_size);
  const timeout = options.timeout ?? DEFAULT_TIMEOUT;
  const data = await _post_json(native_endpoint(options.base_url, options.model), payload, api_key, timeout);
  return _extract_image(data);
}

export async function generate_image(
  prompt: string,
  size: string,
  api_key: string,
  options: GeminiCallOptions,
): Promise<Buffer> {
  return _generate(prompt, size, [], api_key, options);
}

export async function edit_image(
  prompt: string,
  size: string,
  image_paths: readonly string[],
  api_key: string,
  options: GeminiCallOptions,
): Promise<Buffer> {
  if (!image_paths.length) throw new Error('至少需要一张参考图片');
  return _generate(prompt, size, image_paths, api_key, options);
}
