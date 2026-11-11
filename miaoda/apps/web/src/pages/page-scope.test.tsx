import { describe, expect, it, vi } from 'vitest';

/**
 * 套壳构建（`VITE_VIEW_SCOPE=core`）只保留《妙搭复刻需求手册》里的 5 个模块；
 * 本地开发默认 `all`，所有页面都在。这里用动态 import + 重置模块来验两条口径。
 */
describe('页面范围开关', () => {
  it('core 只保留试跑 / 批次 / 历史 / 管理 / 模板库', async () => {
    vi.stubEnv('VITE_VIEW_SCOPE', 'core');
    vi.resetModules();
    const mod = await import('@/pages/page-registry');
    expect(mod.CORE_SCOPE).toBe(true);
    expect(mod.VIEWS.map((view) => view.key)).toEqual(['create', 'batch', 'history', 'admin', 'tag']);
    vi.unstubAllEnvs();
  });

  it('all（默认）保留全部页面', async () => {
    vi.stubEnv('VITE_VIEW_SCOPE', 'all');
    vi.resetModules();
    const mod = await import('@/pages/page-registry');
    expect(mod.CORE_SCOPE).toBe(false);
    expect(mod.VIEWS.map((view) => view.key)).toContain('videogen');
    expect(mod.VIEWS.map((view) => view.key)).toContain('canvas');
    vi.unstubAllEnvs();
  });
});
