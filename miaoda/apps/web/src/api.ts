/**
 * 与 Python 后端通信的唯一入口。
 *
 * 两种运行形态：
 *   1) 同源（默认）：前端由本机 `python run_web.py` 的静态目录托管，走 Cookie 会话；
 *   2) 跨域套壳（`VITE_API_BASE` / `window.__OUTFIT_API_BASE__` 指向本机服务）：
 *      前端部署在飞书妙搭，后端仍跑本机。跨站 Cookie 会被浏览器丢弃，
 *      所以登录响应会额外下发 `token`，这里用 `Authorization: Bearer` 带上。
 */
export interface ApiErrorBody {
  ok: false;
  error: string;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** 全局 401 处理：由 SessionProvider 注册，避免每个页面各自判断。 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

const TOKEN_KEY = 'outfit.api_token';

function runtimeBase(): string {
  if (typeof window === 'undefined') return '';
  const injected = (window as unknown as { __OUTFIT_API_BASE__?: unknown }).__OUTFIT_API_BASE__;
  if (typeof injected === 'string' && injected.trim()) return injected.trim();
  try {
    return window.localStorage.getItem('outfit.api_base') || '';
  } catch {
    return '';
  }
}

/** 后端根地址，空串表示同源。末尾斜杠会被去掉。 */
export const API_BASE = String(runtimeBase() || import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '');

/** true = 前端与后端不同源（妙搭套壳模式）。 */
export const IS_REMOTE = API_BASE !== '';

/** 站外登录令牌：同源模式下后端不下发，保持空串。 */
let token = '';
try {
  token = (typeof localStorage !== 'undefined' && localStorage.getItem(TOKEN_KEY)) || '';
} catch {
  token = '';
}

export function getToken(): string {
  return token;
}

export function setToken(value: string | null): void {
  token = value || '';
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // 隐私模式下写不了 localStorage，内存态仍然可用
  }
}

/**
 * 把后端返回的相对地址（`/api/uploads/...`、`/api/jobs/.../download/...`）
 * 拼成可放进 `src` / `href` 的绝对地址；已经是绝对地址的原样返回。
 */
export function apiUrl(path: string): string {
  const value = String(path || '');
  if (!value || !API_BASE) return value;
  if (/^(https?:|data:|blob:)/i.test(value)) return value;
  return value.startsWith('/') ? `${API_BASE}${value}` : `${API_BASE}/${value}`;
}

/**
 * 混合内容预警：HTTPS 页面调 `http://` 后端会被浏览器直接拦掉（连请求都发不出去）。
 * 本机/局域网地址尤其容易踩，启动时先给出人话提示，别让用户对着空白页猜。
 */
export const MIXED_CONTENT =
  typeof window !== 'undefined' &&
  window.location.protocol === 'https:' &&
  IS_REMOTE &&
  /^http:\/\//i.test(API_BASE) &&
  !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])/i.test(API_BASE);

/**
 * 媒体地址（`<img>` / `<video>` / `<a download>` 用）。
 *
 * 这些标签发不了 `Authorization` 头，跨域时也带不上 Cookie，
 * 所以套壳模式下把令牌挂在查询串上（`?access_token=`），后端 `_token()` 认这个参数。
 * 同源模式原样返回，不改变任何既有行为。
 */
export function mediaUrl(path: string): string {
  const url = apiUrl(path);
  // 只给自己的接口加令牌；COS 签名链接这类站外地址原样返回。
  if (!IS_REMOTE || !token || !url.startsWith(API_BASE)) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}access_token=${encodeURIComponent(token)}`;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** 401 时不触发全局跳登录（登录接口自己处理）。 */
  skipUnauthorized?: boolean;
  /** 原始字节流（视频上传这类接口不吃 JSON）。 */
  raw?: BodyInit;
  /** 追加/覆盖请求头（与 `raw` 搭配用：X-File-Name / X-Tags 等）。 */
  headers?: Record<string, string>;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, skipUnauthorized, raw, headers } = options;
  const init: RequestInit = {
    method,
    credentials: IS_REMOTE ? 'include' : 'same-origin',
    signal,
    headers: { Accept: 'application/json', ...(headers || {}) },
  };
  if (token) init.headers = { ...init.headers, Authorization: `Bearer ${token}` };
  if (raw !== undefined) {
    init.body = raw;
  } else if (body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }

  const res = await fetch(apiUrl(path), init);
  let payload: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (res.status === 401 && !skipUnauthorized) onUnauthorized?.();

  if (!res.ok) {
    const record = payload as { error?: unknown } | null;
    const message = record && typeof record.error === 'string' ? record.error : `请求失败（HTTP ${res.status}）`;
    throw new ApiError(message, res.status);
  }
  return payload as T;
}

/** 给 XHR（需要上传进度）用的请求头：跨域时带上令牌。 */
export function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err ?? '未知错误');
}
