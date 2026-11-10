/** 与 Python `_error()` 同构的错误体：前端只认 `error` 字段。 */
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

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** 401 时不触发全局跳登录（登录接口自己处理）。 */
  skipUnauthorized?: boolean;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, skipUnauthorized } = options;
  const init: RequestInit = {
    method,
    credentials: 'same-origin',
    signal,
    headers: { Accept: 'application/json' },
  };
  if (body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }

  const res = await fetch(path, init);
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

export function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err ?? '未知错误');
}
