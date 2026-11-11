/**
 * 从接口响应里取出图片字节：优先 `b64_json`，其次 `url`（再下载一次）。
 * `app/imggen/decoder.py` 的 TS 版；下载上限 64MB。
 */
import { network_error_message } from './imagegen-api';

export const MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024;

const BAD_B64 = '接口返回的 base64 图片数据损坏';

function _normalize_b64(value: string): string {
  let text = value.trim();
  if (text.startsWith('data:')) text = text.slice(text.indexOf(',') + 1);
  text += '='.repeat((4 - (text.length % 4)) % 4);
  return text;
}

/** 严格 base64（等价 Python `b64decode(..., validate=True)`）：有非法字符就报错。 */
function _strict_b64(value: string): Buffer {
  if (value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new Error(BAD_B64);
  const data = Buffer.from(value, 'base64');
  if (data.length === 0 && value.length > 0) throw new Error(BAD_B64);
  return data;
}

async function _download(url: string, timeout: number): Promise<Buffer> {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error('接口返回的图片地址格式不受支持');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('接口返回的图片地址格式不受支持');
  }

  let response: Response;
  try {
    response = await fetch(url, { signal: AbortSignal.timeout(Math.max(1, timeout) * 1000) });
  } catch (err) {
    throw new Error(`下载接口返回的图片失败：${network_error_message(err, timeout)}`);
  }
  if (!response.ok) {
    throw new Error(`下载接口返回的图片失败：HTTP ${response.status}`);
  }

  const declared = Number(response.headers.get('content-length') ?? '');
  if (Number.isFinite(declared) && declared > MAX_DOWNLOAD_BYTES) {
    throw new Error('接口返回的图片超过 64MB，已放弃下载');
  }
  const data = Buffer.from(await response.arrayBuffer());
  if (data.length > MAX_DOWNLOAD_BYTES) throw new Error('接口返回的图片超过 64MB，已放弃下载');
  return data;
}

/**
 * 从 `{data:[{b64_json|url}]}` 里取第一张图。
 * `timeout` 只作用于 url 下载（Python 调用方传 `min(timeout, 180)`）。
 */
export async function extract_image_bytes(
  payload: Record<string, unknown>,
  options: { timeout?: number } = {},
): Promise<Buffer> {
  const items = payload?.data;
  if (!Array.isArray(items) || items.length === 0) throw new Error('API 返回内容中没有找到图片数据');

  const first = items[0];
  if (first === null || typeof first !== 'object') throw new Error('API 返回的图片数据结构不正确');
  const item = first as Record<string, unknown>;

  const b64_value = item.b64_json;
  if (b64_value) return _strict_b64(_normalize_b64(String(b64_value)));

  const url = item.url;
  if (url) {
    const text = String(url);
    if (text.startsWith('data:')) return Buffer.from(_normalize_b64(text), 'base64');
    return _download(text, options.timeout ?? 120);
  }

  throw new Error('API 返回内容里没有 b64_json 或 url 字段');
}
