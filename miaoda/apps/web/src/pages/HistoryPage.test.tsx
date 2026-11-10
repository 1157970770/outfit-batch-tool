/**
 * 历史任务页的渲染级自检：空态、分组卡片、点开分组后的明细行与对比按钮。
 * 真实分页与下载走 HTTP 冒烟脚本 `scripts/p2-smoke.mjs`。
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
    templates_count: 1,
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
    tagged_templates_count: 1,
    source_ready_templates: 1,
    untagged_skipped: 0,
    unlisted_files: 0,
    manifest_warning: '',
    notice: '',
    imagegen: { configured: false },
  },
  prompts: [{ id: 'p02', title: '换装有字幕', body: '提示词' }],
};

const GROUP = {
  key: 'WEB-tester-20261110-120000-ab12',
  day: '2026-11-10',
  batch_no: 'B-001',
  group_kind: 'batch',
  is_ungrouped: false,
  owner: 'tester',
  small_jobs: 1,
  big_jobs: 0,
  job_count: 1,
  total: 2,
  success: 2,
  failed: 0,
  unfinished: 0,
  state_sig: 'x',
  first_at: 1762747200,
  last_at: 1762750800,
};

const ROW = {
  job_id: 'jobabc123456',
  template_id: '001',
  run_no: 1,
  run_nos: [1],
  run_total: 1,
  run_prompt_changed: false,
  image_url: '/api/uploads/aaa',
  template_name: '0001.mp4',
  owner: 'tester',
  created_at: 1762750800,
  batch_no: 'B-001',
  job_status: 'succeeded',
  status: 'success',
  seconds: 12.5,
  download_url: '/api/jobs/jobabc123456/download/001',
  archive_warning: '',
  verify_warn: '',
};

let groups: unknown[] = [];

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

beforeEach(() => {
  groups = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/bootstrap')) return json(BOOTSTRAP);
      if (url.includes('/api/templates')) {
        return json({ ok: true, items: [], categories: [], tag_values: {}, tag_filter_enabled: true });
      }
      if (url.includes('/api/batches')) return json({ ok: true, batches: [] });
      if (url.includes('/api/jobs')) return json({ ok: true, jobs: [], queued: 0, max_active: 2 });
      if (url.includes('/api/history/groups/')) {
        return json({
          ok: true,
          key: GROUP.key,
          batch_no: GROUP.batch_no,
          items: [ROW],
          runs: [
            { no: 1, job_id: ROW.job_id, count: 2, started_at: 1762750700, finished_at: 1762750800, prompt: '提示词', prompt_changed: false },
          ],
          total: 1,
          offset: 0,
          limit: 100,
        });
      }
      if (url.includes('/api/history/groups')) {
        return json({ ok: true, groups, empty_batches: 0 });
      }
      return json({ ok: false, error: '未命中 mock' }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function historyView(): HTMLElement {
  const node = document.getElementById('history-view');
  if (!node) throw new Error('缺少 #history-view');
  return node;
}

async function openHistory() {
  const { App } = await import('@/App');
  const { bootstrap } = await import('@/session');
  await bootstrap();
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: '历史任务' }));
  await waitFor(() => expect(historyView().className).toContain('active-view'));
  return within(historyView());
}

describe('HistoryPage', () => {
  it('没有记录时给空态提示', async () => {
    const view = await openHistory();
    await waitFor(() => expect(view.getByText('还没有历史记录。')).toBeTruthy());
    expect(view.getByText('选择左侧分组查看明细')).toBeTruthy();
  });

  it('点开分组后列出明细行，并带下载与对比入口', async () => {
    groups = [GROUP];
    const view = await openHistory();

    await waitFor(() => expect(view.getByText('B-001')).toBeTruthy());
    expect(view.getByText('2 成功')).toBeTruthy();
    expect(view.getByText('按批次（1）')).toBeTruthy();

    fireEvent.click(view.getByText('B-001'));
    await waitFor(() => expect(view.getByText('#001 · B-001')).toBeTruthy());
    expect(view.getByText('成功')).toBeTruthy();
    expect(view.getByText('第 1 轮')).toBeTruthy();
    expect(view.getByRole('link', { name: '下载' })).toBeTruthy();

    fireEvent.click(view.getByRole('button', { name: '对比' }));
    await waitFor(() => expect(document.querySelector('.compare-overlay')).toBeTruthy());
    expect(screen.getByText('模板视频')).toBeTruthy();
  });
});
