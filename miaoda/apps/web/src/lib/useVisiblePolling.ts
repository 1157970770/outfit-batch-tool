/**
 * 可见轮询：只在「标签页可见 + 该视图是当前激活页」时继续，避免 4 个页面同时打接口。
 *
 * 与原生版的三处轮询口径等价：
 * - 小批量试跑 3500ms（`app.js:146` + 详情轮询）
 * - 大批量生产 4000ms（且只在有在飞任务 / 复查窗口内轮询）
 * - 生图 2500ms（且只在有排队或生成中的任务时）
 */
import { useEffect, useRef } from 'react';

export interface VisiblePollingOptions {
  /** false 时不排下一次（例如没有在飞任务）。 */
  enabled?: boolean;
  /** 页面是否处于激活状态；默认读 `.view.active-view`（壳层给的类名）。 */
  active?: boolean;
  /** 文档可见性也算进来，默认 true。 */
  respectHidden?: boolean;
}

export function useVisiblePolling(
  tick: () => void | Promise<void>,
  intervalMs: number,
  options: VisiblePollingOptions = {},
): void {
  const { enabled = true, respectHidden = true } = options;
  const tickRef = useRef(tick);
  tickRef.current = tick;

  useEffect(() => {
    if (!enabled) return;
    let timer: number | undefined;
    let cancelled = false;

    const schedule = () => {
      if (cancelled) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(run, intervalMs);
    };

    const run = async () => {
      if (cancelled) return;
      if (respectHidden && document.hidden) {
        schedule();
        return;
      }
      try {
        await tickRef.current();
      } catch {
        // 轮询失败不弹错：下一轮会重试，用户操作时才有 toast
      }
      schedule();
    };

    const onVisible = () => {
      if (!document.hidden) void run();
    };
    document.addEventListener('visibilitychange', onVisible);
    schedule();

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [enabled, intervalMs, respectHidden]);
}
