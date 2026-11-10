/**
 * 大批量生产页的渲染级自检：空列表态 + 新建表单 + 槽位网格。
 * 真实批次流（建批次→锁定→生产）走 HTTP 冒烟脚本 `scripts/p2-smoke.mjs`。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

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
    produce_choices: [5, 10, 20, 50, 100],
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
  prompts: [{ id: 'p02', title: '换装有字幕', body: '把图1的人物换上图2的衣服' }],
};

const TEMPLATES = {
  ok: true,
  items: [
    {
      id: '001',
      name: '0001.mp4',
      shot: 'medium_far',
      duration: 7.5,
      size_mb: 8.59,
      tags: ['中远景'],
      tag_categories: {},
      ready: true,
      upload_state: '待上传',
    },
  ],
  categories: ['景别'],
  tag_values: { 景别: ['中远景'] },
  tag_filter_enabled: true,
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
      if (url.includes('/api/batches')) return json({ ok: true, batches: [] });
      if (url.includes('/api/jobs')) return json({ ok: true, jobs: [], queued: 0, max_active: 2 });
      if (url.includes('/api/history/groups')) return json({ ok: true, groups: [], empty_batches: 0 });
      return json({ ok: false, error: '未命中 mock' }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function batchView(): HTMLElement {
  const node = document.getElementById('batch-view');
  if (!node) throw new Error('缺少 #batch-view');
  return node;
}

async function openBatchPage() {
  const { App } = await import('@/App');
  const { bootstrap } = await import('@/session');
  await bootstrap();
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: '大批量生产' }));
  await waitFor(() => expect(batchView().className).toContain('active-view'));
  return within(batchView());
}

describe('BatchPage', () => {
  it('空列表提示新建，新建表单带批次号与款号', async () => {
    const view = await openBatchPage();

    await waitFor(() => expect(view.getByText('还没有批次，点「新建批次」开始。')).toBeTruthy());
    expect(view.getAllByRole('button', { name: '新建批次' }).length).toBeGreaterThan(0);
    expect(view.getByRole('heading', { name: '新建批次' })).toBeTruthy();
    expect(view.getAllByText('批次号').length).toBeGreaterThan(0);
    expect(view.getByText('款号标签')).toBeTruthy();
    expect(view.getByText('上衣（必填）')).toBeTruthy();
  });

  it('新建表单里有 10 个图片槽位，且第 1 格标注定妆图', async () => {
    await openBatchPage();
    await waitFor(() => expect(batchView().querySelectorAll('.slot-cell')).toHaveLength(10));
    const first = batchView().querySelector('.slot-cell') as HTMLElement;
    expect(first.textContent).toContain('定妆图');
  });

  it('没填批次号就创建会提示，而不是发请求', async () => {
    const view = await openBatchPage();
    const submit = view.getByRole('button', { name: '创建批次' }) as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.click(submit);
    await waitFor(() => expect(screen.getByText('批次号需要 1–40 个字符。')).toBeTruthy());
  });
});
