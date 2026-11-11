/**
 * 会话与启动数据：一次 `/api/bootstrap` 同时拿到用户、服务上限、提示词库。
 * 与原版 app.js 的 state.user / state.service / state.prompts 一一对应，
 * 只是改成订阅式，避免把 service 一层层往下传。
 */
import { useSyncExternalStore } from 'react';

import type { BootstrapResponse, PromptItem, ServiceInfo, User } from '@outfit/shared';
import { ApiError, api, setToken, setUnauthorizedHandler } from '@/api';

export type SessionStatus = 'boot' | 'anon' | 'ready';

interface SessionState {
  status: SessionStatus;
  user: User | null;
  service: ServiceInfo | null;
  prompts: PromptItem[];
  error: string;
}

let state: SessionState = { status: 'boot', user: null, service: null, prompts: [], error: '' };
const listeners = new Set<() => void>();

function setState(patch: Partial<SessionState>): void {
  state = { ...state, ...patch };
  for (const listener of listeners) listener();
}

export function getSession(): SessionState {
  return state;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useSession(): SessionState {
  return useSyncExternalStore(subscribe, getSession, getSession);
}

setUnauthorizedHandler(() => {
  if (state.status === 'ready') setState({ status: 'anon', user: null, service: null, prompts: [] });
});

/** 启动时探活：404/401 都当作未登录，避免首屏报错。 */
export async function bootstrap(): Promise<void> {
  try {
    const data = await api<BootstrapResponse>('/api/bootstrap', { skipUnauthorized: true });
    setState({ status: 'ready', user: data.user, service: data.service, prompts: data.prompts || [], error: '' });
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      setState({ status: 'anon', user: null, service: null, prompts: [], error: '' });
      return;
    }
    setState({ status: 'anon', user: null, service: null, prompts: [], error: (err as Error).message || '服务连接失败' });
  }
}

export async function login(username: string, password: string): Promise<void> {
  const data = await api<{ ok: true; user: User; token?: string }>('/api/login', {
    method: 'POST',
    body: { username, password },
    skipUnauthorized: true,
  });
  // 跨域套壳模式下后端会额外下发 token（跨站 Cookie 不可用），同源模式没有这个字段。
  if (data && data.token) setToken(data.token);
  await bootstrap();
}

export async function logout(): Promise<void> {
  try {
    await api('/api/logout', { method: 'POST', skipUnauthorized: true });
  } finally {
    setToken(null);
    setState({ status: 'anon', user: null, service: null, prompts: [], error: '' });
  }
}

export function isAdmin(session: SessionState): boolean {
  return session.user?.role === 'admin';
}
