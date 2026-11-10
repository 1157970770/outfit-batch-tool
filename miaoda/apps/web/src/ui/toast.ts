/**
 * 单例 Toast：与原版 app.js:43 一致——后到的消息直接覆盖上一条，不做队列，3200ms 自动消失。
 * 用 useSyncExternalStore 暴露给 React，避免为了一个提示再套一层 context。
 */
import { useSyncExternalStore } from 'react';

export interface ToastMessage {
  text: string;
  tone: 'info' | 'success' | 'error';
  seq: number;
}

let current: ToastMessage | null = null;
let timer: number | null = null;
let seq = 0;
const listeners = new Set<() => void>();

const TOAST_MS = 3200;

function emit(): void {
  for (const listener of listeners) listener();
}

export function toast(text: string, tone: ToastMessage['tone'] = 'info'): void {
  const message = String(text || '').trim();
  if (!message) return;
  current = { text: message, tone, seq: ++seq };
  if (timer !== null) window.clearTimeout(timer);
  timer = window.setTimeout(() => {
    current = null;
    timer = null;
    emit();
  }, TOAST_MS);
  emit();
}

export function toastError(err: unknown): void {
  toast(typeof err === 'string' ? err : (err as Error)?.message || '操作失败', 'error');
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): ToastMessage | null {
  return current;
}

export function useToast(): ToastMessage | null {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
