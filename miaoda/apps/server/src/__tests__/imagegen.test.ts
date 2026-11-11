/**
 * 生图模块离线测试（不联网、不花钱）：照 `tests/test_imagegen.py` 的 14 步验收逐条对齐。
 *
 * 口径要点：
 * - 任务队列是进程内存 + 守护 worker，测试里用 `set_clients()` 换成桩，跑完再断言磁盘；
 * - Images 协议按 17 档像素规格裁切；Gemini 直出比例不裁（`4:5@2K` 原样落盘）；
 * - 配置缺失时 `options().configured === false` 并带 reason，建任务 503，不抛启动异常。
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import sharp from 'sharp';
import { afterAll, describe, expect, it } from 'vitest';

import type { AppConfigService } from '../config/app-config.service';
import type { AuthedUser } from '../auth/decorators';
import {
  ImagegenManagerService,
  sniff_extension,
} from '../imagegen/imagegen-manager.service';
import { ImagegenController } from '../imagegen/imagegen.controller';
import type { UploadsService } from '../uploads/uploads.service';
import {
  IMAGE_MODEL_IDS,
  image_quality_options,
} from '../imagegen/imagegen-models';
import {
  api_size_for_output,
  finalize_output_image,
} from '../imagegen/imagegen-processing';
import {
  _extract_image,
  native_endpoint,
  split_size,
} from '../imagegen/imagegen-gemini';

const ROOTS: string[] = [];

// 控制器方法签名要完整的 AuthedUser（含 record），测试只关心 owner/role 两个字段。
const USER = { username: 'alice', role: 'user' } as unknown as AuthedUser;
const OTHER = { username: 'bob', role: 'user' } as unknown as AuthedUser;
const ADMIN = { username: 'root', role: 'admin' } as unknown as AuthedUser;

interface Harness {
  cfg: AppConfigService;
  manager: ImagegenManagerService;
  controller: ImagegenController;
  uploads: FakeUploads;
  dataDir: string;
}

class FakeUploads {
  readonly files = new Map<string, { name: string; file: string }>();

  add(id: string, name: string, file: string): void {
    this.files.set(id, { name, file });
  }

  resolve(upload_id: string): { meta: { name: string }; file: string } | null {
    const hit = this.files.get(upload_id);
    return hit === undefined ? null : { meta: { name: hit.name }, file: hit.file };
  }
}

/** 造一个最小可用的假 config（真 AppConfigService 构造函数会校验 session_secret）。 */
function make_config(root: string, overrides: Record<string, unknown> = {}): AppConfigService {
  const dataDir = path.join(root, 'web-data');
  const serverDir = path.join(root, 'server');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(serverDir, { recursive: true });
  return {
    repoRoot: root,
    serverDir,
    dataDir,
    imagegen: {
      base_url: 'https://api.example.com/v1/',
      api_key: 'sk-test-key-1234567890',
      gemini_base_url: 'https://api.example.com',
      model: 'gpt-image-2-vip',
      default_quality: 'high',
      timeout_seconds: 60,
      max_count: 3,
      max_references: 4,
      max_inflight: 2,
      price_per_image: '0.03',
      ...overrides,
    },
  } as unknown as AppConfigService;
}

function harness(overrides: Record<string, unknown> = {}): Harness {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-imagegen-'));
  ROOTS.push(root);
  const cfg = make_config(root, overrides);
  const manager = new ImagegenManagerService(cfg);
  const uploads = new FakeUploads();
  const controller = new ImagegenController(manager, uploads as unknown as UploadsService);
  return { cfg, manager, controller, uploads, dataDir: cfg.dataDir };
}

/** 生成一张真实 PNG（sharp 生成，避免手写二进制）。 */
function png(width: number, height: number): Promise<Buffer> {
  return sharp({ create: { width, height, channels: 3, background: '#3366ff' } })
    .png()
    .toBuffer();
}

async function wait_finished(
  manager: ImagegenManagerService,
  task_id: string,
  timeout_ms = 10_000,
): Promise<ReturnType<ImagegenManagerService['get_record']>> {
  const deadline = Date.now() + timeout_ms;
  for (;;) {
    const task = manager.get_record(task_id, ADMIN);
    if (!task.active) return task;
    if (Date.now() > deadline) throw new Error(`任务没有在 ${timeout_ms}ms 内结束：${task.status}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

function expect_http(fn: () => unknown, status: number, pattern: RegExp): void {
  try {
    fn();
  } catch (err) {
    const error = err as {
      message?: string;
      status?: number;
      getResponse?: () => unknown;
    };
    // 改成 HttpException 之后，中文文案在响应体 `{ok:false,error}` 里，`message` 只剩 "Http Exception"。
    const response = typeof error.getResponse === 'function' ? error.getResponse() : null;
    const body =
      response && typeof response === 'object'
        ? String((response as { error?: unknown }).error ?? '')
        : '';
    expect(error.status).toBe(status);
    expect(body || String(error.message)).toMatch(pattern);
    return;
  }
  throw new Error('期望抛出错误，但没有抛');
}

/** 造一张 32 位 hex 的 upload id（正则 `/^[a-f0-9]{32}$/`）。 */
function upload_id(seed: number): string {
  return seed.toString(16).padStart(32, '0');
}

afterAll(() => {
  for (const root of ROOTS) fs.rmSync(root, { recursive: true, force: true });
});

describe('imagegen options', () => {
  it('下发 6 个型号、两套尺寸口径与上限（与 Python options() 字段一致）', () => {
    const { manager } = harness();
    const options = manager.options() as {
      configured: boolean;
      models: Array<{ id: string; provider: string; sizes: Array<{ value: string }>; qualities: Array<{ value: string }> }>;
      default_model: string;
      default_quality: string;
      max_count: number;
      max_references: number;
      prompt_limit: number;
      price: string;
    };

    expect(options.configured).toBe(true);
    expect(options.models.map((item) => item.id)).toEqual(IMAGE_MODEL_IDS);
    expect(options.models).toHaveLength(6);
    expect(options.default_model).toBe('gpt-image-2-vip');
    expect(options.default_quality).toBe('high');
    expect(options.max_count).toBe(3);
    expect(options.max_references).toBe(4);
    expect(options.prompt_limit).toBe(4000);
    expect(options.price).toBe('0.03');

    const nano = options.models.find((item) => item.id === 'gemini-nano-banana-2.1');
    expect(nano?.provider).toBe('gemini');
    // Gemini 是「比例@档位」：19 档，且不出现 Images 的像素值。
    expect(nano?.sizes).toHaveLength(19);
    expect(nano?.sizes.map((size) => size.value)).toContain('4:5@2K');
    expect(nano?.sizes.map((size) => size.value)).not.toContain('2048x2048');
    expect(nano?.qualities.map((quality) => quality.value)).toEqual(['auto']);

    // GPT Image 2.5 才放开 6 档质量，其余 4 档。
    const flare = options.models.find((item) => item.id === 'gpt-image-2.5-flare-vip');
    expect(flare?.qualities.map((quality) => quality.value)).toEqual([
      'auto',
      'low',
      'medium',
      'high',
      'xhigh',
      'max',
    ]);
    expect(image_quality_options('gpt-image-2-vip')).toEqual(['auto', 'low', 'medium', 'high']);
  });

  it('未配置时 configured:false + reason，且建任务 503（不抛启动异常）', () => {
    const { manager } = harness({ api_key: '' });
    const options = manager.options() as { configured: boolean; reason: string };
    expect(options.configured).toBe(false);
    expect(options.reason).toContain('api_key');
    expect_http(
      () => manager.create_task(USER, { prompt: '一只猫' }, []),
      503,
      /生图密钥未配置/,
    );
  });
});

describe('imagegen create_task 校验', () => {
  it('空提示词 / 未知型号 / 未知尺寸 / 不支持的画质 全部 400', () => {
    const { manager } = harness();
    expect_http(() => manager.create_task(USER, { prompt: '  ' }, []), 400, /请先填写提示词/);
    expect_http(
      () => manager.create_task(USER, { prompt: 'x', model: 'dall-e-3' }, []),
      400,
      /不支持的图片模型/,
    );
    expect_http(
      () => manager.create_task(USER, { prompt: 'x', size: '512x512' }, []),
      400,
      /所选模型不支持这个输出尺寸/,
    );
    expect_http(
      () => manager.create_task(USER, { prompt: 'x', quality: 'xhigh' }, []),
      400,
      /所选模型不支持此图片质量/,
    );
    expect_http(
      () => manager.create_task(USER, { prompt: '字'.repeat(4001) }, []),
      400,
      /提示词过长（4001 字）/,
    );
  });

  it('两套协议口径不能串台', () => {
    const { manager } = harness();
    // Gemini：尺寸必须是「比例@档位」，画质只能是 auto。
    expect_http(
      () =>
        manager.create_task(
          USER,
          { prompt: 'x', model: 'gemini-nano-banana-2.1', size: '2048x2048' },
          [],
        ),
      400,
      /所选模型不支持这个输出尺寸/,
    );
    expect_http(
      () =>
        manager.create_task(
          USER,
          { prompt: 'x', model: 'gemini-nano-banana-2.1', quality: 'high' },
          [],
        ),
      400,
      /所选模型不支持此图片质量/,
    );
    // Images 协议不认 `4:5@2K`。
    expect_http(
      () => manager.create_task(USER, { prompt: 'x', size: '4:5@2K' }, []),
      400,
      /所选模型不支持这个输出尺寸/,
    );
  });

  it('count 被钳到 [1, max_count]，参考图超限 400', async () => {
    const { manager } = harness({ max_references: 2 });
    manager.set_clients({ generate: async () => png(32, 32) });
    const low = manager.create_task(USER, { prompt: 'x', count: 0 }, []);
    expect(low.count).toBe(1);
    const high = manager.create_task(USER, { prompt: 'x', count: 99 }, []);
    expect(high.count).toBe(3);
    await wait_finished(manager, low.id);
    await wait_finished(manager, high.id);

    const refs = [1, 2, 3].map((index) => ({ path: path.join(ROOTS[ROOTS.length - 1], 'x.png') }));
    expect_http(() => manager.create_task(USER, { prompt: 'x' }, refs), 400, /参考图最多 2 张/);
  });

  it('public() 字段与 Python 完全一致（含 url / download_url / active）', async () => {
    const { manager } = harness();
    manager.set_clients({ generate: async () => png(64, 64) });
    const task = manager.create_task(USER, { prompt: '一只猫', count: 1 }, []);
    expect(Object.keys(task).sort()).toEqual(
      [
        'active',
        'api_size',
        'count',
        'created_at',
        'elapsed_ms',
        'error',
        'id',
        'images',
        'model',
        'model_label',
        'owner',
        'progress',
        'prompt',
        'quality',
        'references',
        'size',
        'size_label',
        'status',
        'updated_at',
      ].sort(),
    );
    expect(task.active).toBe(true);
    expect(task.owner).toBe('alice');
    expect(task.model_label).toBe('GPT Image 2 VIP｜$0.03/张');

    const done = await wait_finished(manager, task.id);
    expect(done.images[0]?.url).toBe(`/api/imagegen/tasks/${task.id}/images/1`);
    expect(done.images[0]?.download_url).toBe(`/api/imagegen/tasks/${task.id}/images/1?download=1`);
  });
});

describe('imagegen 任务执行', () => {
  it('参考图按顺序拷贝进 files/<id>/，Images 走 edit 且按 4:5 裁切', async () => {
    const { manager, dataDir } = harness();
    const source_dir = path.join(dataDir, 'uploads');
    fs.mkdirSync(source_dir, { recursive: true });
    const ref1 = path.join(source_dir, 'a.png');
    const ref2 = path.join(source_dir, 'b.png');
    fs.writeFileSync(ref1, await png(40, 50));
    fs.writeFileSync(ref2, await png(40, 50));

    const calls: Array<{ prompt: string; size: string; names: string[]; model: string; quality: string }> = [];
    manager.set_clients({
      edit: async (prompt, size, paths, _key, options) => {
        calls.push({
          prompt,
          size,
          names: paths.map((file) => path.basename(file)),
          model: options.model,
          quality: String(options.quality),
        });
        return png(640, 640);
      },
    });

    const task = manager.create_task(
      USER,
      { prompt: '换背景', size: '4:5', count: 2 },
      [
        { upload_id: upload_id(1), name: 'a.png', path: ref1 },
        { upload_id: upload_id(2), name: 'b.png', path: ref2 },
      ],
    );
    expect(task.count).toBe(2);
    expect(task.api_size).toBe('2048x2048');
    expect(task.size_label).toBe('竖版 4:5｜高分辨率');
    expect(task.references.map((item) => item.index)).toEqual([1, 2]);

    const task_dir = path.join(dataDir, 'imagegen', 'files', task.id);
    expect(fs.existsSync(path.join(task_dir, 'ref1.png'))).toBe(true);
    expect(fs.existsSync(path.join(task_dir, 'ref2.png'))).toBe(true);

    const done = await wait_finished(manager, task.id);
    expect(done.status).toBe('已完成');
    expect(done.progress).toBe(2);
    expect(done.images).toHaveLength(2);
    expect(done.elapsed_ms).toBeGreaterThanOrEqual(0);

    // 4:5 → 640×640 裁成 512×640（比例 0.8）。
    const meta = await sharp(path.join(task_dir, '1.png')).metadata();
    expect(meta.format).toBe('png');
    expect(meta.width).toBe(512);
    expect(meta.height).toBe(640);

    expect(calls).toHaveLength(2);
    expect(calls[0]?.prompt).toBe('换背景');
    expect(calls[0]?.size).toBe('2048x2048');
    expect(calls[0]?.names).toEqual(['ref1.png', 'ref2.png']);
    expect(calls[0]?.model).toBe('gpt-image-2-vip');
    expect(calls[0]?.quality).toBe('high');

    // 侧车 meta：前端「详情」与后续排查都读它。
    const sidecar = JSON.parse(fs.readFileSync(path.join(task_dir, '1.meta.json'), 'utf8')) as Record<string, unknown>;
    expect(sidecar.api_size).toBe('2048x2048');
    expect(sidecar.output_size).toEqual([512, 640]);
    expect(sidecar.reference_images).toEqual(['ref1.png', 'ref2.png']);
    expect(sidecar.operator).toBe('alice');
    expect(sidecar.size).toBe('4:5');
  });

  it('Gemini 直出比例不裁切：4:5@2K 原样落盘，api_size/size_label 用档位口径', async () => {
    const { manager, dataDir } = harness();
    const calls: Array<{ size: string; base_url: string; timeout: number; quality: string }> = [];
    manager.set_clients({
      gemini_generate: async (prompt, size, _key, options) => {
        calls.push({ size, base_url: options.base_url, timeout: Number(options.timeout), quality: String(options.quality) });
        return png(640, 800);
      },
    });

    const task = manager.create_task(
      USER,
      { prompt: '竖版海报', model: 'gemini-nano-banana-2.1', size: '4:5@2K', count: 1 },
      [],
    );
    expect(task.api_size).toBe('2K（4:5）');
    expect(task.size_label).toBe('竖版 4:5｜2K');
    expect(task.quality).toBe('auto');

    const done = await wait_finished(manager, task.id);
    expect(done.status).toBe('已完成');
    const meta = await sharp(path.join(dataDir, 'imagegen', 'files', task.id, '1.png')).metadata();
    expect(meta.width).toBe(640);
    expect(meta.height).toBe(800);

    expect(calls[0]?.size).toBe('4:5@2K');
    expect(calls[0]?.base_url).toBe('https://api.example.com');
    expect(calls[0]?.timeout).toBe(60);
    expect(calls[0]?.quality).toBe('auto');
  });

  it('Gemini 带参考图走 edit 分支，参考图名同样是 ref1.png', async () => {
    const { manager, dataDir } = harness();
    const source = path.join(dataDir, 'ref.png');
    fs.writeFileSync(source, await png(30, 30));
    let names: string[] = [];
    manager.set_clients({
      gemini_edit: async (_prompt, _size, paths) => {
        names = paths.map((file) => path.basename(file));
        return png(80, 100);
      },
    });
    const task = manager.create_task(
      USER,
      { prompt: '改风格', model: 'gemini-nano-banana-2.1', size: '4:5@2K' },
      [{ upload_id: upload_id(7), name: 'ref.png', path: source }],
    );
    const done = await wait_finished(manager, task.id);
    expect(done.status).toBe('已完成');
    expect(names).toEqual(['ref1.png']);
  });

  it('接口报错落成「生成失败」并保留中文原因', async () => {
    const { manager } = harness();
    manager.set_clients({
      generate: async () => {
        throw Object.assign(new Error('接口拒绝了请求（HTTP 400）'), { status: 400 });
      },
    });
    const task = manager.create_task(USER, { prompt: 'x' }, []);
    const done = await wait_finished(manager, task.id);
    expect(done.status).toBe('生成失败');
    expect(done.error).toContain('HTTP 400');
    expect(done.active).toBe(false);
  });
});

describe('imagegen 取图 / 删除 / 权限', () => {
  it('取图与参考图都能回看，越界 404', async () => {
    const { manager, dataDir } = harness();
    const source = path.join(dataDir, 'ref.png');
    fs.writeFileSync(source, await png(20, 20));
    manager.set_clients({ edit: async () => png(640, 640) });
    const task = manager.create_task(
      USER,
      { prompt: 'x', size: '4:5' },
      [{ upload_id: upload_id(3), name: 'ref.png', path: source }],
    );
    await wait_finished(manager, task.id);

    const image = manager.image_file(task.id, 1, USER);
    expect(fs.readFileSync(image.file).subarray(0, 4)).toEqual(Buffer.from([0x89, 0x50, 0x4e, 0x47]));
    expect(image.name.endsWith('.png')).toBe(true);

    const reference = manager.image_file(task.id, 1, USER, { reference: true });
    expect(path.basename(reference.file)).toBe('ref1.png');

    expect_http(() => manager.image_file(task.id, 2, USER), 404, /找不到这张图片/);
    expect_http(() => manager.image_file(task.id, 9, USER, { reference: true }), 404, /找不到这张参考图/);
  });

  it('删除会连任务 json 与 files/<id>/ 一起清掉', async () => {
    const { manager, dataDir } = harness();
    manager.set_clients({ generate: async () => png(64, 64) });
    const task = manager.create_task(USER, { prompt: 'x' }, []);
    await wait_finished(manager, task.id);

    const task_file = path.join(dataDir, 'imagegen', 'tasks', `${task.id}.json`);
    const task_dir = path.join(dataDir, 'imagegen', 'files', task.id);
    expect(fs.existsSync(task_file)).toBe(true);

    manager.delete_task(task.id, USER);
    expect(fs.existsSync(task_file)).toBe(false);
    expect(fs.existsSync(task_dir)).toBe(false);
    expect_http(() => manager.image_file(task.id, 1, USER), 404, /找不到这个生图任务/);
  });

  it('生成中的任务不能删：409「任务还在生成中，请等它跑完再删。」', async () => {
    const { manager, controller } = harness();
    const deferred: { resolve?: (data: Buffer) => void } = {};
    const pending = new Promise<Buffer>((resolve) => {
      deferred.resolve = resolve;
    });
    manager.set_clients({ generate: async () => pending });

    const task = controller.create({ prompt: 'x', count: 1 }, USER).task;
    // 等 worker 把状态推到「生成中」，否则可能还在排队。
    const deadline = Date.now() + 2000;
    while (manager.get_record(task.id, USER).status !== '生成中') {
      if (Date.now() > deadline) throw new Error('worker 没有开始执行');
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    expect_http(() => controller.remove(task.id, USER), 409, /任务还在生成中，请等它跑完再删。/);

    deferred.resolve?.(await png(64, 64));
    const done = await wait_finished(manager, task.id);
    expect(done.status).toBe('已完成');
  });

  it('权限按 owner 隔离，admin 可见全部', async () => {
    const { manager } = harness();
    manager.set_clients({ generate: async () => png(32, 32) });
    const mine = manager.create_task(USER, { prompt: 'x' }, []);
    await wait_finished(manager, mine.id);

    expect(manager.list_tasks(USER).map((item) => item.id)).toEqual([mine.id]);
    expect(manager.list_tasks(OTHER)).toEqual([]);
    expect(manager.list_tasks(ADMIN).map((item) => item.id)).toEqual([mine.id]);
    expect_http(() => manager.get_record(mine.id, OTHER), 404, /找不到这个生图任务/);
    expect_http(() => manager.delete_task(mine.id, OTHER), 404, /找不到这个生图任务/);
  });
});

describe('imagegen 重启恢复', () => {
  it('上次进程留下的「生成中」落成「生成失败」并写明服务重启', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-imagegen-restart-'));
    ROOTS.push(root);
    const cfg = make_config(root);
    const tasks_dir = path.join(cfg.dataDir, 'imagegen', 'tasks');
    fs.mkdirSync(tasks_dir, { recursive: true });
    const id = 'c'.repeat(32);
    fs.writeFileSync(
      path.join(tasks_dir, `${id}.json`),
      JSON.stringify({
        id,
        owner: 'alice',
        created_at: 1,
        updated_at: 1,
        started_at: 1,
        finished_at: 0,
        elapsed_ms: 0,
        prompt: '半路重启',
        model: 'gpt-image-2-vip',
        model_label: 'GPT Image 2 VIP｜$0.03/张',
        size: 'auto',
        size_label: '自动｜由模型决定',
        quality: 'high',
        count: 1,
        api_size: 'auto',
        status: '生成中',
        progress: 0,
        error: '',
        references: [],
        images: [],
      }),
    );

    const manager = new ImagegenManagerService(cfg);
    const task = manager.get_record(id, USER);
    expect(task.status).toBe('生成失败');
    expect(task.error).toContain('服务重启');
    expect(task.active).toBe(false);
  });
});

describe('imagegen 协议纯函数', () => {
  it('api_size_for_output：2.5 的 4:5 走原生 2048x2560，2.0 仍是 2048x2048', () => {
    expect(api_size_for_output('4:5', 'gpt-image-2.5-flare-vip')).toBe('2048x2560');
    expect(api_size_for_output('4:5', 'gpt-image-2-vip')).toBe('2048x2048');
    expect(api_size_for_output('auto', 'gpt-image-2-vip')).toBe('auto');
  });

  it('finalize_output_image：2048×2048 裁 4:5 输出 1636×2045（Python 的 batch 取整口径）', async () => {
    const { data } = await finalize_output_image(await png(2048, 2048), '4:5');
    const meta = await sharp(data).metadata();
    expect(meta.format).toBe('png');
    expect(meta.width).toBe(1636);
    expect(meta.height).toBe(2045);
    expect((meta.width as number) / (meta.height as number)).toBeCloseTo(0.8, 2);
  });

  it('finalize_output_image：非裁切档位原样返回字节', async () => {
    const source = await png(300, 200);
    const { data, width, height } = await finalize_output_image(source, '1024x1024');
    expect(width).toBe(300);
    expect(height).toBe(200);
    expect(data.equals(source)).toBe(true);
  });

  it('native_endpoint / split_size', () => {
    expect(native_endpoint('https://api.example.com/v1beta', 'gemini-nano-banana-2.1')).toBe(
      'https://api.example.com/v1beta/models/gemini-nano-banana-2.1:generateContent',
    );
    expect(native_endpoint('https://api.example.com/', 'm')).toBe(
      'https://api.example.com/v1beta/models/m:generateContent',
    );
    expect(split_size('4:5@2K')).toEqual(['4:5', '2K']);
    expect(split_size('auto')).toEqual(['', '']);
    expect(() => split_size('4:5@8K')).toThrow(/不支持的输出规格/);
  });

  it('_extract_image：只取最后一张非 thought 图；没图时报 blockReason 并提示已计费', async () => {
    const thought = await png(8, 8);
    const final = await png(16, 16);
    const picked = _extract_image({
      candidates: [
        {
          content: {
            parts: [
              { inlineData: { mimeType: 'image/png', data: thought.toString('base64') }, thought: true },
              { inlineData: { mimeType: 'image/png', data: final.toString('base64') } },
            ],
          },
        },
      ],
    });
    expect(picked.equals(final)).toBe(true);

    let message = '';
    try {
      _extract_image({
        candidates: [{ content: { parts: [{ text: '被安全策略拦截' }] }, finishReason: 'SAFETY' }],
      });
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain('SAFETY');
    expect(message).toContain('计费');
    expect(message).toContain('生成一张图片');
  });

  it('sniff_extension 按文件头判扩展名', () => {
    expect(sniff_extension(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))).toBe('.png');
    expect(sniff_extension(Buffer.from([0xff, 0xd8, 0xff, 0xe0]))).toBe('.jpg');
    expect(
      sniff_extension(Buffer.concat([Buffer.from('RIFF'), Buffer.from([0, 0, 0, 0]), Buffer.from('WEBP')])),
    ).toBe('.webp');
    expect(sniff_extension(Buffer.from('not-an-image'))).toBe('.png');
  });
});

describe('imagegen 控制器', () => {
  it('POST /api/imagegen/tasks 声明 201', () => {
    const status = Reflect.getMetadata('__httpCode__', ImagegenController.prototype.create);
    expect(status).toBe(201);
  });

  it('upload_ids 口径：>10 张 / 重复 / 非法 id 分别 400、400、404', () => {
    const { controller } = harness();
    const many = Array.from({ length: 11 }, (_unused, index) => upload_id(index + 1));
    expect_http(
      () => controller.create({ prompt: 'x', upload_ids: many }, USER),
      400,
      /一次最多 10 张图片/,
    );
    expect_http(
      () => controller.create({ prompt: 'x', upload_ids: [upload_id(1), upload_id(1)] }, USER),
      400,
      /同一张图片不能重复提交/,
    );
    expect_http(
      () => controller.create({ prompt: 'x', upload_ids: ['not-a-real-upload'] }, USER),
      404,
      /第 1 张图片已失效/,
    );
    expect_http(
      () => controller.create({ prompt: 'x', upload_ids: 'oops' }, USER),
      400,
      /图片参数无效/,
    );
  });

  it('upload_ids 命中上传记录后建任务，且 allow_empty 允许无参考图', async () => {
    const { controller, uploads, dataDir, manager } = harness();
    manager.set_clients({ edit: async () => png(640, 640) });
    const file = path.join(dataDir, 'up.png');
    fs.writeFileSync(file, await png(24, 24));
    uploads.add(upload_id(5), 'up.png', file);

    const created = controller.create({ prompt: '带参考图', upload_ids: [upload_id(5)] }, USER);
    expect(created.ok).toBe(true);
    expect(created.task.references).toHaveLength(1);
    expect(created.task.references[0]?.name).toBe('up.png');
    await wait_finished(manager, created.task.id);

    const empty = controller.create({ prompt: '无参考图' }, USER);
    expect(empty.task.references).toHaveLength(0);
    await wait_finished(manager, empty.task.id);
  });

  it('列表带 configured / reason，limit 与 Python 一样钳在 [1, 200]', () => {
    const { controller, manager } = harness();
    manager.set_clients({ generate: async () => png(32, 32) });
    for (let index = 0; index < 3; index += 1) {
      manager.create_task(USER, { prompt: `p${index}` }, []);
    }
    const listed = controller.list('2', USER);
    expect(listed.ok).toBe(true);
    expect(listed.items).toHaveLength(2);
    expect(listed.configured).toBe(true);
    expect(listed.reason).toBe('');
    expect(controller.list('abc', USER).items).toHaveLength(3);
  });
});
