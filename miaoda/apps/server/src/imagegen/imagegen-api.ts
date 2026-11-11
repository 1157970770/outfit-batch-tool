/**
 * 老张（laozhang.ai）图片接口客户端 + 错误文案。`app/imggen/api.py` + `errors.py` 的 TS 版。
 *
 * 接口契约（与桌面版一致，已按 2026-11-09 的文档核对）：
 * - 文生图：POST {base_url}images/generations，JSON 体 {model, prompt, size?, quality?}
 * - 改图：  POST {base_url}images/edits，multipart；参考图字段名固定 `image[]`，
 *           多个文件按传入顺序被模型视作「图1、图2…」，所以调用方必须自己保证顺序
 * - 返回：  {data: [{b64_json | url}]}，由 imagegen-decoder 取字节
 *
 * 只允许 VIP 按次计费型号（$0.03/张）；参数校验在 imagegen-models.image_request_parameters。
 * Python 用 urllib，这里用 Node 18+ 内置 fetch，异常树不同但中文文案逐字一致。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomUUID } from 'node:crypto';

import { extract_image_bytes } from './imagegen-decoder';
import { image_request_parameters } from './imagegen-models';

export const DEFAULT_TIMEOUT = 300;
export const USER_AGENT = 'OutfitWebImageGen/1.0 (+local)';

/** 接口层错误：message 已经是中文人话，status 为 HTTP 状态码（0 表示网络层）。 */
export class ImageApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(message: string, init: { status?: number; detail?: string } = {}) {
    super(message);
    this.name = 'ImageApiError';
    this.status = Math.trunc(Number(init.status ?? 0)) || 0;
    this.detail = init.detail ?? '';
  }
}

// ---------------------------------------------------------------- 错误文案

const HTTP_ERROR_REASONS: Record<number, string> = {
  400: '接口拒绝了请求，所选模型可能不在当前 API Key 分组中，或提示词、图片、尺寸参数不符合要求',
  401: '接口鉴权失败，API 密钥无效或已过期',
  403: '接口拒绝访问，当前密钥没有权限，或内容被安全策略拦截',
  404: '接口地址或所选模型不存在，请确认当前 API Key 分组已开放该模型',
  408: '接口等待请求超时',
  409: '接口当前无法处理该请求，请稍后重试',
  413: '上传的参考图片过大',
  415: '上传的图片格式不受支持',
  422: '接口无法处理当前提示词、图片或参数',
  429: '请求过于频繁，或账号额度、余额不足',
  500: '接口服务器内部异常',
  502: '接口网关异常',
  503: '接口服务暂时不可用',
  504: '接口服务器响应超时',
};

const SECRET_RE = /sk-[A-Za-z0-9_\-\.]+/g;

/** 去掉密钥、压平空白、截断——用于回显接口原始原因。 */
export function sanitize(text: unknown, limit = 400): string {
  if (!text) return '';
  const cleaned = String(text).replace(SECRET_RE, '[已隐藏密钥]').replace(/Authorization/g, '[已隐藏头]');
  return cleaned.split(/\s+/).filter(Boolean).join(' ').slice(0, limit);
}

/** 返回（给用户看的一句话，脱敏后的接口原因）。 */
export function http_error_message(status: number, body = ''): [string, string] {
  let reason = HTTP_ERROR_REASONS[status];
  if (reason === undefined) {
    if (status >= 500 && status <= 599) reason = '接口服务器发生异常';
    else if (status >= 400 && status <= 499) reason = '接口拒绝了当前请求';
    else reason = '接口返回了异常状态';
  }
  const detail = _error_detail(body);
  let text = `${reason}（HTTP ${status}）`;
  if (detail) text += `；接口原因：${detail}`;
  return [text, detail];
}

/** 只取结构化错误原因（error.message / message / detail），不回显整段响应。 */
function _error_detail(body: string): string {
  if (!body) return '';
  let data: unknown;
  try {
    data = JSON.parse(body);
  } catch {
    return sanitize(body, 200);
  }
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return '';
  const payload = data as Record<string, unknown>;
  const error = payload.error;
  let message: unknown;
  if (error !== null && typeof error === 'object' && !Array.isArray(error)) {
    message = (error as Record<string, unknown>).message;
  } else if (typeof error === 'string') {
    message = error;
  }
  if (typeof message !== 'string') message = payload.message ?? payload.detail;
  if (typeof message !== 'string') return '';
  return sanitize(message);
}

function _text_of(value: unknown): string {
  if (value instanceof Error) return value.message;
  return String(value ?? '');
}

/** 沿 `cause` 链取错误码（fetch 的 ENOTFOUND/ECONNREFUSED 都在 cause 上）。 */
function _code_of(error: unknown): string {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && current; depth += 1) {
    const code = (current as { code?: unknown }).code;
    if (typeof code === 'string' && code) return code;
    current = (current as { cause?: unknown }).cause;
  }
  return '';
}

function _is_network_error(error: unknown): boolean {
  if (typeof DOMException !== 'undefined' && error instanceof DOMException) {
    return error.name === 'TimeoutError' || error.name === 'AbortError' || error.name === 'NetworkError';
  }
  const code = _code_of(error);
  if (/^(EAI_AGAIN|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|UND_ERR_.*|CERT_.*)$/.test(code)) return true;
  return error instanceof TypeError && (error as { cause?: unknown }).cause !== undefined;
}

/** urllib 的 URLError.reason 花样很多，这里按最可能的原因翻中文。 */
export function network_error_message(reason: unknown, timeout = 0): string {
  const text = _text_of(reason);
  const lower = text.toLowerCase();
  const code = _code_of(reason);
  const name = reason instanceof Error ? reason.name : '';

  const ssl_like =
    code.startsWith('CERT_') ||
    code === 'DEPTH_ZERO_SELF_SIGNED_CERT' ||
    code === 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' ||
    code === 'SELF_SIGNED_CERT_IN_CHAIN' ||
    lower.includes('certificate') ||
    lower.includes('ssl');
  if (ssl_like) return '安全连接验证失败，请检查系统时间、网络证书或代理设置';

  const timed_out =
    name === 'TimeoutError' ||
    name === 'AbortError' ||
    code === 'ETIMEDOUT' ||
    code === 'UND_ERR_CONNECT_TIMEOUT' ||
    code === 'UND_ERR_HEADERS_TIMEOUT' ||
    code === 'UND_ERR_BODY_TIMEOUT' ||
    lower.includes('timed out') ||
    lower.includes('timeout');
  if (timed_out) {
    if (timeout) return `接口处理时间过长（已等待 ${timeout} 秒），请稍后重试`;
    return '网络请求超时，请检查网络后重试';
  }

  if (code === 'ECONNREFUSED' || lower.includes('econnrefused')) {
    return '接口拒绝连接，请检查接口地址或代理设置';
  }
  if (
    code === 'ENOTFOUND' ||
    code === 'EAI_AGAIN' ||
    lower.includes('name or service not known') ||
    lower.includes('getaddrinfo')
  ) {
    return '无法解析接口域名，请检查网络或 DNS 设置';
  }
  return `无法连接生图接口：${sanitize(text, 120)}`;
}

/** 把任务执行中的异常翻成中文；已经不是技术异常的（自己抛的中文 Error）原样返回。 */
export function explain_error(error: unknown): string {
  if (error instanceof ImageApiError) return error.message;
  if (_is_network_error(error)) {
    const cause = (error as { cause?: unknown }).cause;
    return network_error_message(cause ?? error);
  }

  const code = _code_of(error);
  if (code === 'EACCES' || code === 'EPERM') return '没有文件读写权限，请检查数据目录权限';
  if (code === 'ENOENT') return '需要的图片或目录不存在，可能已被移动或删除';
  if (code === 'EISDIR') return '选择的是文件夹，不是有效的图片文件';
  if (code === 'ENOSPC' || code === 'EDQUOT' || code === 'EMFILE') return '磁盘空间不足，无法写入文件';
  if (/^E[A-Z0-9]+$/.test(code)) return '文件或系统操作失败，请检查路径、权限和磁盘空间';

  if (error instanceof TypeError || error instanceof RangeError || error instanceof ReferenceError) {
    return '接口返回的数据结构不符合预期，请稍后重试或联系维护人员';
  }

  const message = _text_of(error).trim();
  if (message) return message;
  return '生图过程发生未预期异常，请重试；如果持续出现请联系维护人员';
}

// ---------------------------------------------------------------- 请求

const MIME_BY_EXT: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.gif': 'image/gif',
};

export function mime_for_name(name: string): string {
  return MIME_BY_EXT[path.extname(String(name ?? '')).toLowerCase()] ?? 'application/octet-stream';
}

function _endpoint(base_url: string, name: string): string {
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
  return `${base.replace(/\/+$/, '')}/${name.replace(/^\/+/, '')}`;
}

export function _headers(api_key: string, content_type?: string): Record<string, string> {
  const key = String(api_key ?? '').trim();
  if (!key) throw new ImageApiError('生图接口密钥未配置，请检查 server/imagegen.json 的 api_key');
  const headers: Record<string, string> = {
    Authorization: `Bearer ${key}`,
    Accept: 'application/json',
    'User-Agent': USER_AGENT,
  };
  if (content_type) headers['Content-Type'] = content_type;
  return headers;
}

async function _fetch(url: string, init: RequestInit, timeout: number): Promise<Response> {
  try {
    return await fetch(url, { ...init, signal: AbortSignal.timeout(Math.max(1, timeout) * 1000) });
  } catch (err) {
    const cause = (err as { cause?: unknown }).cause;
    throw new ImageApiError(network_error_message(cause ?? err, timeout));
  }
}

/** 读响应：非 2xx 走中文映射，非 JSON 单独报错（Gemini 客户端也复用这一份）。 */
export async function _read_response(response: Response, timeout: number): Promise<Record<string, unknown>> {
  if (!response.ok) {
    let body = '';
    try {
      body = (await response.text()).slice(0, 8000);
    } catch {
      body = '';
    }
    const [message, detail] = http_error_message(response.status, body);
    throw new ImageApiError(message, { status: response.status, detail });
  }

  let raw: string;
  try {
    raw = await response.text();
  } catch (err) {
    throw new ImageApiError(network_error_message((err as { cause?: unknown }).cause ?? err, timeout));
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object');
    return parsed as Record<string, unknown>;
  } catch {
    throw new ImageApiError('接口没有返回 JSON，可能被网关拦截或地址配置有误');
  }
}

/** 发一个 JSON POST 并返回解析后的响应体。 */
export async function _post_json(
  url: string,
  payload: Record<string, unknown>,
  api_key: string,
  timeout: number,
): Promise<Record<string, unknown>> {
  const response = await _fetch(
    url,
    {
      method: 'POST',
      headers: _headers(api_key, 'application/json'),
      body: JSON.stringify(payload),
    },
    timeout,
  );
  return _read_response(response, timeout);
}

/** multipart 的 filename 只能是 ascii，参考图我们统一重命名过，这里兜底。 */
function _safe_filename(name: string): string {
  const cleaned = Array.from(String(name ?? ''))
    .filter((ch) => /[A-Za-z0-9._-]/.test(ch))
    .join('');
  return cleaned || 'reference.png';
}

function _multipart(fields: Record<string, string>, files: Array<[string, string]>): { body: Buffer; contentType: string } {
  const boundary = '----imggen' + randomUUID().replace(/-/g, '');
  const chunks: Buffer[] = [];
  for (const [key, value] of Object.entries(fields)) {
    chunks.push(
      Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="${key}"\r\n\r\n${value}\r\n`,
        'utf8',
      ),
    );
  }
  for (const [key, file] of files) {
    const filename = _safe_filename(path.basename(file));
    const mime = mime_for_name(file) === 'application/octet-stream' ? 'application/octet-stream' : mime_for_name(file);
    chunks.push(
      Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="${key}"; filename="${filename}"\r\n` +
          `Content-Type: ${mime}\r\n\r\n`,
        'utf8',
      ),
    );
    chunks.push(fs.readFileSync(file));
    chunks.push(Buffer.from('\r\n', 'utf8'));
  }
  chunks.push(Buffer.from(`--${boundary}--\r\n`, 'utf8'));
  return { body: Buffer.concat(chunks), contentType: `multipart/form-data; boundary=${boundary}` };
}

export interface ImageCallOptions {
  base_url: string;
  model: string;
  quality?: string;
  timeout?: number;
}

/** 文生图（不带参考图）。返回 PNG/JPEG 原始字节。 */
export async function generate_image(
  prompt: string,
  size: string,
  api_key: string,
  options: ImageCallOptions,
): Promise<Buffer> {
  const timeout = options.timeout ?? DEFAULT_TIMEOUT;
  const payload: Record<string, unknown> = {
    model: options.model,
    prompt,
    ...image_request_parameters(options.model, size, options.quality ?? 'high'),
  };
  const data = await _post_json(_endpoint(options.base_url, 'images/generations'), payload, api_key, timeout);
  return extract_image_bytes(data, { timeout: Math.min(timeout, 180) });
}

/** 参考图改图。`image_paths` 的顺序就是模型的「图1、图2…」。 */
export async function edit_image(
  prompt: string,
  size: string,
  image_paths: readonly string[],
  api_key: string,
  options: ImageCallOptions,
): Promise<Buffer> {
  if (!image_paths.length) throw new Error('至少需要一张参考图片');
  const timeout = options.timeout ?? DEFAULT_TIMEOUT;

  const fields: Record<string, string> = {
    model: options.model,
    prompt,
    ...image_request_parameters(options.model, size, options.quality ?? 'high'),
  };
  const { body, contentType } = _multipart(
    fields,
    image_paths.map((item) => ['image[]', item] as [string, string]),
  );

  const response = await _fetch(
    _endpoint(options.base_url, 'images/edits'),
    { method: 'POST', headers: _headers(api_key, contentType), body },
    timeout,
  );
  const data = await _read_response(response, timeout);
  return extract_image_bytes(data, { timeout: Math.min(timeout, 180) });
}

/** 给状态接口用的密钥指纹，绝不回显完整 Key。 */
export function masked_key(api_key: string): string {
  const key = String(api_key ?? '').trim();
  if (!key) return '';
  if (key.length <= 8) return '****';
  return sanitize(`${key.slice(0, 4)}…${key.slice(-4)}`);
}
