/**
 * 标签页（视频库）的渲染级自检（jsdom）：快照渲染、搜索/状态筛选、批量弃用的写请求。
 *
 * 只验证「请求发得对、界面显示对」，真实拖拽/视频播放仍靠人工验收。
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
  prompts: [{ id: 'p02', title: '换装有字幕', body: '换装' }],
};

function row(over: Record<string, unknown>) {
  return {
    id: '001',
    name: '0001.mp4',
    filename: '0001.mp4',
    relative_path: '0001.mp4',
    tags: ['中远景', '适合春夏'],
    tag_categories: {},
    deprecated: false,
    deprecated_by: '',
    ready: true,
    upload_state: '待上传',
    duration: 7.5,
    size_mb: 8.59,
    missing: false,
    changed: false,
    touched_by: 'tester',
    touched_at: '2026-11-10 10:00:00',
    uploaded_at: 0,
    rating: 3,
    ...over,
  };
}

const SNAPSHOT = {
  ok: true,
  enabled: true,
  source: 'store',
  schema: 3,
  revision: 7,
  updated_at: '2026-11-10 10:00:00',
  updated_by: 'tester',
  categories: ['景别', '适合季节'],
  library: {
    version: 2,
    tags: ['中远景', '中景', '适合春夏'],
    categories: { 中远景: '景别', 中景: '景别', 适合春夏: '适合季节' },
    category_order: ['景别', '适合季节'],
    usage: { 中远景: 4 },
  },
  videos: [
    row({ id: '001', name: '0001.mp4' }),
    row({ id: '002', name: '0002.mp4', tags: ['中景'], deprecated: true, deprecated_by: 'tester' }),
    row({ id: '003', name: '0003.mp4', tags: [], ready: false, upload_state: '未打标' }),
  ],
  stats: { ready: 2, candidates: 2 },
  audit: [],
  manifest: { count: 3, matched: 3, pulled_at: 0, warning: '' },
  path: 'web-data-dev/template_tags.json',
};

type Call = { url: string; init: RequestInit };

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

let calls: Call[] = [];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init: init || {} });
      if (url.includes('/api/bootstrap')) return json(BOOTSTRAP);
      if (url.includes('/api/tags/snapshot')) return json(SNAPSHOT);
      if (url.includes('/api/tags/impact')) return json({ ok: true, affected: 2, locked_batches: [] });
      if (url.includes('/api/tags/deprecate')) {
        return json({
          ok: true,
          revision: 8,
          changed: 2,
          updated: [row({ id: '001', deprecated: true }), row({ id: '002', deprecated: true })],
          stats: { ready: 1 },
        });
      }
      if (url.includes('/api/batches')) return json({ ok: true, batches: [] });
      if (url.includes('/api/templates')) return json({ ok: true, items: [], categories: [], tag_values: {} });
      if (url.includes('/api/jobs')) return json({ ok: true, jobs: [] });
      return json({ ok: false, error: '未命中 mock' }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function openTagPage() {
  const { App } = await import('@/App');
  const { bootstrap } = await import('@/session');
  await bootstrap();
  render(<App />);
  fireEvent.click(screen.getAllByRole('button', { name: '视频库' })[0]);
  const view = document.getElementById('tag-view');
  if (!view) throw new Error('缺少 #tag-view');
  return within(view);
}

describe('TagPage', () => {
  it('渲染快照里的三行视频与统计', async () => {
    const view = await openTagPage();
    await waitFor(() => expect(view.getAllByText('0001.mp4').length).toBeGreaterThan(0));
    expect(view.getByText('0002.mp4')).toBeTruthy();
    expect(view.getAllByText('未打标').length).toBeGreaterThan(0);
    expect(view.getByText(/共 3 条/)).toBeTruthy();
    // revision 来自快照，直接显示在页头
    expect(view.getByText(/revision 7/)).toBeTruthy();
  });

  it('搜索与状态筛选能收窄列表', async () => {
    const view = await openTagPage();
    await waitFor(() => expect(view.getByText('0002.mp4')).toBeTruthy());

    fireEvent.change(view.getByPlaceholderText('搜索编号 / 文件名 / 标签'), { target: { value: '0002' } });
    await waitFor(() => expect(view.queryByText('0001.mp4')).toBeNull());
    expect(view.getByText('0002.mp4')).toBeTruthy();

    fireEvent.change(view.getByPlaceholderText('搜索编号 / 文件名 / 标签'), { target: { value: '' } });
    const status = view.getByDisplayValue('全部') as HTMLSelectElement;
    fireEvent.change(status, { target: { value: 'untagged' } });
    await waitFor(() => expect(view.queryByText('0002.mp4')).toBeNull());
    expect(view.getByText('0003.mp4')).toBeTruthy();
  });

  it('选中两条后批量弃用：先问影响面，再带 revision 提交', async () => {
    const view = await openTagPage();
    await waitFor(() => expect(view.getByText('0001.mp4')).toBeTruthy());

    const table = document.querySelector('#tag-view table') as HTMLElement | null;
    if (!table) throw new Error('缺少标签表格');
    const boxes = Array.from(table.querySelectorAll('tbody input[type="checkbox"]'));
    fireEvent.click(boxes[0] as HTMLElement);
    fireEvent.click(boxes[1] as HTMLElement);
    await waitFor(() => expect(view.getByText('已选 2 条')).toBeTruthy());

    // 「弃用」在每行操作列也有一个，这里限定在批量条里点。
    const bulk = view.getByText('已选 2 条').closest('section');
    if (!bulk) throw new Error('缺少批量条');
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    fireEvent.click(within(bulk).getByRole('button', { name: '弃用' }));

    await waitFor(() => expect(calls.some((call) => call.url.includes('/api/tags/deprecate'))).toBe(true));
    expect(confirmSpy).toHaveBeenCalled();
    const impact = calls.find((call) => call.url.includes('/api/tags/impact'));
    expect(impact).toBeTruthy();
    const deprecate = calls.find((call) => call.url.includes('/api/tags/deprecate'));
    const body = JSON.parse(String(deprecate?.init.body || '{}')) as {
      ids?: string[];
      deprecated?: boolean;
      revision?: number;
    };
    expect(body.deprecated).toBe(true);
    expect(body.revision).toBe(7);
    expect(body.ids).toEqual(['001', '002']);
  });
});
