import { render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '@/App';
import { bootstrap } from '@/session';

const bootstrapPayload = {
  ok: true,
  user: { username: 'tester', role: 'user' },
  service: {
    provider: 'dashscope',
    model: 'wan3.0-video',
    dev_mode: true,
    cos_enabled: false,
    templates_count: 3,
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
    tagged_templates_count: 3,
    source_ready_templates: 3,
    untagged_skipped: 0,
    unlisted_files: 0,
    manifest_warning: '',
    notice: '',
    imagegen: null,
    videogen: {
      configured: true,
      reason: '',
      model: 'wan3.0-video',
      provider: 'dashscope',
      cos_enabled: false,
      modes: [
        { value: 'auto', label: '自动判断', hint: '有参考视频就按视频编辑，没有就按参考生视频' },
        { value: 'reference', label: '参考生视频', hint: '' },
        { value: 'edit', label: '视频编辑', hint: '' },
      ],
      resolutions: [
        { value: '1080P', label: '1080P（最清晰）' },
        { value: '720P', label: '720P（均衡）' },
        { value: '480P', label: '480P（最快）' },
      ],
      ratios: [
        { value: '16:9', label: '16:9 横屏' },
        { value: '9:16', label: '9:16 竖屏' },
      ],
      durations: [
        { value: 5, label: '5 秒' },
        { value: -1, label: '自动（视频编辑智能时长）' },
      ],
      defaults: {
        mode: 'auto',
        resolution: '1080P',
        ratio: '16:9',
        duration: 5,
        count: 1,
        prompt_extend: true,
        watermark: false,
        audio: false,
      },
      limits: {
        max_count: 4,
        max_images: 10,
        max_videos: 5,
        max_audios: 5,
        prompt_limit: 20000,
        negative_limit: 2000,
        video_max_seconds: 15,
        video_max_mb: 100,
        audio_max_mb: 15,
        image_max_mb: 20,
      },
      modes_engine: ['edit', 'reference'],
    },
  },
  prompts: [],
};

const task = {
  id: 'b'.repeat(32),
  owner: 'tester',
  created_at: '2026-11-11 08:00:00',
  updated_at: '2026-11-11 08:01:00',
  started_at: '2026-11-11 08:00:05',
  finished_at: '',
  status: '生成中',
  progress: '0/2',
  prompt: '雨后的东京街头，电影感',
  negative_prompt: '',
  mode: 'reference',
  mode_choice: 'auto',
  resolution: '1080P',
  ratio: '16:9',
  duration: 5,
  count: 2,
  seed: null,
  audio: false,
  prompt_extend: true,
  watermark: false,
  video_seconds: null,
  message: '',
  active: true,
  cancel_requested: false,
  shots: [
    {
      index: 1,
      status: '生成中',
      error: '',
      message: '',
      task_id: 'task-1',
      seconds: 0,
      url: '',
      download_url: '',
    },
  ],
  refs: { images: [], videos: [], audios: [] },
};

function mockFetch() {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const json = (payload: unknown, status = 200) =>
      new Response(JSON.stringify(payload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    if (url.includes('/api/bootstrap')) return json(bootstrapPayload);
    if (url.includes('/api/videogen/tasks') && url.includes('limit=')) {
      return json({ ok: true, items: [task], configured: true, reason: '' });
    }
    if (url.includes('/api/videogen/tasks')) return json({ ok: true, items: [], configured: true, reason: '' });
    if (url.includes('/api/templates')) return json({ ok: true, items: [], categories: [], tag_values: {} });
    if (url.includes('/api/batches')) return json({ ok: true, batches: [] });
    if (url.includes('/api/jobs')) return json({ ok: true, jobs: [] });
    return json({ ok: true });
  });
  vi.stubGlobal('fetch', spy);
  return spy;
}

describe('VideogenPage', () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockFetch();
  });

  it('渲染自由视频表单与选项', async () => {
    await bootstrap();
    window.localStorage.setItem('outfit.view', 'videogen');
    render(<App />);

    const view = document.getElementById('videogen-view') as HTMLElement;
    expect(view).toBeTruthy();
    expect(within(view).getByText('自由视频')).toBeTruthy();
    expect(within(view).getByText('提示词')).toBeTruthy();
    expect(within(view).getByText('自动判断')).toBeTruthy();
    expect(within(view).getByText('1080P（最清晰）')).toBeTruthy();
    expect(within(view).getByText('开始生成')).toBeTruthy();
    // 未启用 COS 时给出提示
    expect(within(view).getByText(/未启用 COS/)).toBeTruthy();
  });

  it('展示生成记录与分镜状态', async () => {
    await bootstrap();
    window.localStorage.setItem('outfit.view', 'videogen');
    render(<App />);

    const view = document.getElementById('videogen-view') as HTMLElement;
    await waitFor(() => expect(within(view).getByText('雨后的东京街头，电影感')).toBeTruthy());
    expect(within(view).getByText('分镜 1')).toBeTruthy();
    expect(within(view).getByText('取消')).toBeTruthy();
  });

  it('没写提示词就提交会被拦下', async () => {
    await bootstrap();
    window.localStorage.setItem('outfit.view', 'videogen');
    render(<App />);

    const view = document.getElementById('videogen-view') as HTMLElement;
    const submit = within(view).getByText('开始生成') as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(false));
    submit.click();
    await waitFor(() => expect(within(view).getByText('请填写提示词，描述你要生成的视频。')).toBeTruthy());
  });
});
