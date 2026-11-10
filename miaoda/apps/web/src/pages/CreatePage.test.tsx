/**
 * 小批量试跑页的渲染级自检（jsdom）：确认外壳、10 个图片槽位、候选场景、提交按钮都在，
 * 并且切到「历史」页签时不会炸。真实浏览器里的视觉与拖拽手感仍靠人工验收。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('@/session', async () => {
  const actual = await vi.importActual<typeof import('@/session')>('@/session');
  return actual;
});

const TEMPLATES = {
  ok: true,
  items: [
    {
      id: '001',
      name: '0001.mp4',
      shot: 'medium_far',
      duration: 7.5,
      size_mb: 8.59,
      tags: ['中远景', '适合春夏'],
      tag_categories: {},
      ready: true,
      upload_state: '待上传',
    },
    {
      id: '002',
      name: '0004.mp4',
      shot: 'medium',
      duration: 6,
      size_mb: 7.1,
      tags: ['中景', '适合秋冬'],
      tag_categories: {},
      ready: true,
      upload_state: '待上传',
    },
  ],
  categories: ['景别', '适合季节'],
  tag_values: { 景别: ['中远景', '中景'], 适合季节: ['适合春夏', '适合秋冬'] },
  tag_filter_enabled: true,
};

const BOOTSTRAP = {
  ok: true,
  user: { username: 'tester', role: 'admin' },
  service: {
    provider: 'dashscope',
    model: 'wan3.0-video',
    dev_mode: true,
    cos_enabled: true,
    templates_count: 2,
    templates_error: '',
    max_items: 20,
    small_lane_items: 3,
    max_batch_items: 500,
    max_batch_run_items: 100,
    produce_choices: [5, 10],
    produce_min: 5,
    max_images: 10,
    max_concurrency: 100,
    tag_filter_enabled: true,
    tagged_templates_count: 2,
    source_ready_templates: 2,
    untagged_skipped: 0,
    unlisted_files: 0,
    manifest_warning: '',
    notice: '',
    imagegen: { configured: false },
  },
  prompts: [
    { id: 'p02', title: '换装有字幕', body: '把图1的人物换上图2的衣服，保持动作' },
    { id: 'p03', title: '换装无字幕', body: '无字幕版提示词' },
  ],
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/bootstrap')) return json(BOOTSTRAP);
      if (url.includes('/api/templates')) return json(TEMPLATES);
      if (url.includes('/api/jobs')) return json({ ok: true, jobs: [], queued: 0, max_active: 2 });
      return json({ ok: false, error: '未命中 mock' }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function renderReadyApp() {
  const { App } = await import('@/App');
  const { bootstrap } = await import('@/session');
  await bootstrap();
  return render(<App />);
}

describe('CreatePage', () => {
  it('渲染十个图片槽位、候选场景与提交按钮', async () => {
    const { container } = await renderReadyApp();

    await waitFor(() => expect(container.querySelectorAll('.slot-cell')).toHaveLength(10));
    expect(screen.getAllByText('小批量试跑').length).toBeGreaterThan(0);
    expect(screen.getByText('定妆图（必填）')).toBeTruthy();
    expect(screen.getByRole('button', { name: '确认提交' })).toBeTruthy();
    expect(screen.getByText('换装有字幕')).toBeTruthy();

    fireEvent.click(screen.getByText('我来挑选'));
    await waitFor(() => expect(container.querySelectorAll('.pick-card')).toHaveLength(2));
    expect(screen.getByText('#001')).toBeTruthy();
  });

  it('切到历史页签显示空状态而不是报错', async () => {
    await renderReadyApp();
    fireEvent.click(screen.getByRole('button', { name: '历史' }));
    await waitFor(() => expect(screen.getByText('试跑历史')).toBeTruthy());
    expect(document.body.textContent).toContain('还没有试跑任务');
  });

  it('没选图片就提交会给出提示，而不是发请求', async () => {
    await renderReadyApp();
    const submit = screen.getByRole('button', { name: '确认提交' }) as HTMLButtonElement;
    // 候选集加载完之前按钮是禁用的，先等它可用再点
    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.click(submit);
    await waitFor(() => expect(screen.getByText('请先上传服装图片。')).toBeTruthy());
  });
});
