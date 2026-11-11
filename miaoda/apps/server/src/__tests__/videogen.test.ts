import { describe, expect, it } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import {
  MAX_INPUT_PLUS_OUTPUT,
  MAX_VIDEO_TOTAL,
  VideoGenError,
  buildOptions,
  validateRequest,
} from '../videogen/videogen-models';
import { base64BudgetError, validateAudio, validateVideo } from '../videogen/videogen-media';

const base = { prompt: '海边走秀', ratio: '16:9', mode: 'reference', resolution: '720P', duration: 5, count: 1 };

describe('videogen 选项下发', () => {
  it('三种生成方式与时长档位与 Python 同口径', () => {
    const options = buildOptions({
      configured: true,
      reason: '',
      model: 'wan3.0-video',
      provider: 'dashscope',
      cosEnabled: true,
    });
    expect(options.modes.map((m) => m.value)).toEqual(['auto', 'reference', 'edit']);
    expect(options.durations.map((d) => d.value)).toEqual([5, 7, 10, 15, -1]);
    // 顺序跟随 RESOLUTIONS（Python `for key in RESOLUTIONS`），不是按「最清晰优先」重排。
    expect(options.resolutions.map((r) => r.value)).toEqual(['480P', '720P', '1080P']);
    expect(options.resolutions[2].label).toBe('1080P（最清晰）');
    expect(options.limits.max_count).toBe(4);
    expect(options.limits.video_max_seconds).toBe(15);
    expect(options.defaults).toMatchObject({ mode: 'auto', resolution: '1080P', ratio: '16:9', duration: 5 });
  });

  it('未配置时 configured=false 且带原因', () => {
    const options = buildOptions({
      configured: false,
      reason: '未配置 key',
      model: 'wan3.0-video',
      provider: 'dashscope',
      cosEnabled: false,
    });
    expect(options.configured).toBe(false);
    expect(options.reason).toBe('未配置 key');
  });
});

describe('videogen 参数校验', () => {
  it('空提示词', () => {
    expect(() => validateRequest({ prompt: '   ' }, { imageCount: 0, videoMetas: [], audioCount: 0 })).toThrow(
      /请填写提示词/,
    );
  });

  it('提示词超 20000 字', () => {
    expect(() =>
      validateRequest({ prompt: 'x'.repeat(20001) }, { imageCount: 0, videoMetas: [], audioCount: 0 }),
    ).toThrow(/提示词最多 20000 字/);
  });

  it('视频编辑必须带参考视频', () => {
    expect(() => validateRequest({ prompt: '改背景', mode: 'edit' }, { imageCount: 1, videoMetas: [], audioCount: 0 })).toThrow(
      /「视频编辑」至少需要一段参考视频/,
    );
  });

  it('参考生视频不许 adaptive', () => {
    expect(() =>
      validateRequest({ ...base, ratio: 'adaptive' }, { imageCount: 1, videoMetas: [], audioCount: 0 }),
    ).toThrow(/参考生视频需要具体画面比例/);
  });

  it('数量 1–4', () => {
    expect(() => validateRequest({ ...base, count: 9 }, { imageCount: 1, videoMetas: [], audioCount: 0 })).toThrow(
      /生成数量需在 1–4 之间/,
    );
  });

  it('参考图最多 10 张', () => {
    expect(() =>
      validateRequest({ ...base }, { imageCount: 11, videoMetas: [], audioCount: 0 }),
    ).toThrow(/参考图最多 10 张/);
  });

  it('非 edit 时 duration=-1 自动改 5', () => {
    const parsed = validateRequest(
      { ...base, duration: -1 },
      { imageCount: 1, videoMetas: [], audioCount: 0 },
    );
    expect(parsed.duration).toBe(5);
  });

  it('参考视频总时长超 15s', () => {
    expect(() =>
      validateRequest(
        { ...base, mode: 'reference' },
        { imageCount: 1, videoMetas: [{ duration: MAX_VIDEO_TOTAL + 1 }], audioCount: 0 },
      ),
    ).toThrow(/超过 15 秒上限/);
  });

  it('输入 + 输出超 30s', () => {
    expect(() =>
      validateRequest(
        { ...base, duration: 20 },
        { imageCount: 1, videoMetas: [{ duration: 15 }], audioCount: 0 },
      ),
    ).toThrow(new RegExp(`超过 ${MAX_INPUT_PLUS_OUTPUT} 秒上限`));
  });

  it('正常参数返回规范化结果', () => {
    const parsed = validateRequest({ ...base }, { imageCount: 1, videoMetas: [], audioCount: 0 });
    expect(parsed).toMatchObject({ mode: 'reference', resolution: '720P', ratio: '16:9', duration: 5, count: 1 });
    expect(parsed.prompt).toBe('海边走秀');
  });
});

describe('videogen 素材校验', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'vg-media-'));
  const write = (name: string, bytes: number): string => {
    const file = path.join(tmp, name);
    fs.writeFileSync(file, Buffer.alloc(bytes, 1));
    return file;
  };

  it('音频只收 wav/mp3', () => {
    const file = write('a.aac', 1024);
    expect(() => validateAudio(file)).toThrow(/音频 a\.aac 扩展名不支持（只收 wav\/mp3）/);
  });

  it('音频超过 15MB', () => {
    const file = write('big.mp3', 16 * 1024 * 1024);
    expect(() => validateAudio(file)).toThrow(/超过官方 15MB 上限/);
  });

  it('视频超过 100MB', () => {
    const file = write('big.mp4', 101 * 1024 * 1024);
    expect(() => validateVideo(file)).toThrow(/超过官方 100MB 上限/);
  });

  it('未启用 COS 时 Base64 总量超 40MB', () => {
    const a = write('a.png', 21 * 1024 * 1024);
    const b = write('b.png', 21 * 1024 * 1024);
    expect(base64BudgetError([a, b], false)).toMatch(/40MB/);
    expect(base64BudgetError([a, b], true)).toBeNull();
  });

  it('VideoGenError 带状态码', () => {
    const err = new VideoGenError('不行', 409);
    expect(err.status).toBe(409);
    expect(err.message).toBe('不行');
  });
});
