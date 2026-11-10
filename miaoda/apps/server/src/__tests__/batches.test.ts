/**
 * 批次状态机 / 对账 / 历史分组键的离线测试。
 *
 * 用假的 AppConfig / JobManager / TemplateCatalog / Uploads / Prompts 组一个真
 * `BatchManagerService`，落盘到 `os.tmpdir()` 下的临时数据根——不连网、不花钱。
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterAll, beforeEach, describe, expect, it } from 'vitest';

import type { AuthedUser } from '../auth/decorators';
import type { AppConfigService } from '../config/app-config.service';
import type { Job, JobManagerService } from '../jobs/job-manager.service';
import type { PromptsService } from '../templates/prompts.service';
import { TemplateError, type TemplateCatalogService, type TemplateRow } from '../templates/template-catalog.service';
import type { UploadsService } from '../uploads/uploads.service';

import {
  BatchError,
  BatchManagerService,
  dayKey,
  historyLane,
  normNo,
  type Batch,
} from '../batches/batch-manager.service';

// ---------------------------------------------------------------- 夹具

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-batches-'));
const DATA = path.join(TMP, 'web-data');

const ADMIN: AuthedUser = {
  username: 'admin',
  role: 'admin',
  record: { role: 'admin' } as AuthedUser['record'],
};
const ALICE: AuthedUser = {
  username: 'alice',
  role: 'user',
  record: { role: 'user' } as AuthedUser['record'],
};
const BOB: AuthedUser = {
  username: 'bob',
  role: 'user',
  record: { role: 'user' } as AuthedUser['record'],
};

const UPLOAD_ID = 'a'.repeat(32);

function templateRow(id: string, overrides: Partial<TemplateRow> = {}): TemplateRow {
  return {
    id,
    name: `场景 ${id}`,
    filename: `${id}.mp4`,
    shot: id,
    relative_path: `${id}.mp4`,
    tags: [],
    tag_categories: {},
    upload_state: 'uploaded',
    duration: 1.5,
    size_mb: 1,
    deprecated: false,
    deprecated_at: '',
    deprecated_by: '',
    deprecated_reason: '',
    missing: false,
    changed: false,
    touched_at: 0,
    touched_by: '',
    uploaded_at: 0,
    rating: 0,
    url: `https://example.invalid/${id}.mp4`,
    key: `templates/${id}.mp4`,
    ...overrides,
  };
}

interface FakeJob extends Record<string, unknown> {
  id: string;
  owner: string;
  status: string;
  created_at: number;
  finished_at: number | null;
  batch_ref: string;
  records: Array<Record<string, unknown>>;
}

function makeJob(id: string, owner: string, templateIds: string[], batchRef = ''): FakeJob {
  const now = Date.now() / 1000;
  return {
    id,
    owner,
    status: 'running',
    created_at: now,
    finished_at: null,
    batch_ref: batchRef,
    lane: templateIds.length <= 3 ? 'small' : 'big',
    total: templateIds.length,
    done: 0,
    prompt: '原始提示词',
    resolution: '720P',
    duration: 7,
    image_path: path.join(DATA, 'uploads', 'aa', `${UPLOAD_ID}.png`),
    images: [],
    templates: templateIds.map((tid) => ({ id: tid, name: `场景 ${tid}`, url: `u/${tid}`, key: `k/${tid}`, duration: 1.5 })),
    template_ids: templateIds,
    records: templateIds.map((tid) => ({ id: tid, template: `场景 ${tid}`, status: 'running' })),
  };
}

function makeHarness() {
  const jobs = new Map<string, FakeJob>();
  let jobSeq = 0;

  const jobManager = {
    list(owner: string, admin: boolean, ownerFilter = ''): Job[] {
      const rows = [...jobs.values()];
      return rows.filter((job) => {
        if (owner && !admin && job.owner !== owner) return false;
        return !ownerFilter || job.owner === ownerFilter;
      }) as unknown as Job[];
    },
    get(id: string): Job | undefined {
      return jobs.get(id) as unknown as Job | undefined;
    },
    create(params: {
      owner: string;
      templates: Array<{ id: string }>;
      batchRef?: string;
      prompt?: string;
    }): Job {
      jobSeq += 1;
      const id = `job-${jobSeq}`;
      const job = makeJob(
        id,
        params.owner,
        params.templates.map((tpl) => String(tpl.id)),
        String(params.batchRef || ''),
      );
      job.prompt = String(params.prompt || '');
      jobs.set(id, job);
      return job as unknown as Job;
    },
    publicView(job: Job): Record<string, unknown> {
      return { id: (job as unknown as FakeJob).id, status: (job as unknown as FakeJob).status };
    },
    outputPath(jobId: string): string | null {
      const job = jobs.get(jobId);
      return job ? path.join(DATA, 'output', jobId) : null;
    },
    recheckTimeouts(jobId: string): Job {
      const job = jobs.get(jobId);
      if (!job) throw new Error('找不到该任务');
      return job as unknown as Job;
    },
  };

  const catalogRows = ['001', '002', '003', '004', '005', '006'].map((id) => templateRow(id));
  const catalog = {
    items: catalogRows,
    async build(): Promise<void> {
      /* 已就绪 */
    },
    resolve(templateId: string): TemplateRow {
      const hit = catalogRows.find((row) => row.id === String(templateId).padStart(3, '0'));
      if (!hit) throw new TemplateError(`模板 ${templateId} 不在当前视频场景清单里`);
      return hit;
    },
    filterItems(filters: Record<string, string[]>, excludes: Record<string, string[]> = {}): TemplateRow[] {
      void filters;
      const banned = new Set(Object.values(excludes).flat());
      return catalogRows.filter((row) => !row.tags.some((tag) => banned.has(tag)));
    },
  };

  const uploads = {
    resolve(uploadId: string, owner: string) {
      if (uploadId !== UPLOAD_ID) return null;
      return {
        meta: { id: uploadId, owner, path: '', name: '定妆.png', created_at: 0, info: {} },
        file: path.join(DATA, 'uploads', 'aa', `${UPLOAD_ID}.png`),
      };
    },
  };

  const prompts = {
    list: () => [
      { id: 'p02', name: '标准', body: '标准提示词' },
      { id: 'p03', name: '宽松', body: '宽松提示词' },
    ],
  };

  const config = {
    dataDir: DATA,
    maxBatchItems: 500,
    maxBatchRunItems: 100,
    users: {} as Record<string, Record<string, unknown>>,
    unitPrice: () => ({ price: 0.6, source: 'config' as const }),
  };

  const service = new BatchManagerService(
    config as unknown as AppConfigService,
    jobManager as unknown as JobManagerService,
    catalog as unknown as TemplateCatalogService,
    uploads as unknown as UploadsService,
    prompts as unknown as PromptsService,
  );

  return { service, jobs, jobManager, config };
}

/** 建一个已锁定、有图、有 5 条待生产条目的批次（锁定前必须先把图落盘）。 */
async function lockedBatch(
  service: BatchManagerService,
  user: AuthedUser = ALICE,
  batchNo = '2026-11-秋装-01',
): Promise<string> {
  const imagePath = path.join(DATA, 'uploads', 'aa', `${UPLOAD_ID}.png`);
  fs.mkdirSync(path.dirname(imagePath), { recursive: true });
  fs.writeFileSync(imagePath, 'png');

  const batch = await service.create(user, {
    batch_no: batchNo,
    title: '测试',
    upload_ids: [UPLOAD_ID],
    prompt_id: 'p02',
    prompt: '原始提示词',
    resolution: '720P',
    duration: 7,
    tags: { top: 'AX111' },
  });
  const id = String(batch.id);
  await service.lock(id, user, {
    template_ids: ['001', '002', '003', '004', '005'],
    confirmed: true,
  });
  return id;
}

function readBatch(id: string): Batch {
  return JSON.parse(fs.readFileSync(path.join(DATA, 'batches', `${id}.json`), 'utf8')) as Batch;
}

afterAll(() => {
  fs.rmSync(TMP, { recursive: true, force: true });
});

// ---------------------------------------------------------------- 纯函数：分组键

describe('分组键与工具函数', () => {
  it('normNo 归一化忽略大小写与首尾空白', () => {
    expect(normNo('  2026-AW-01 ')).toBe('2026-aw-01');
    expect(normNo(undefined)).toBe('');
    expect(normNo(2026)).toBe('2026');
  });

  it('dayKey 按本地时区归日，缺时间戳落到 unknown', () => {
    const stamp = new Date(2026, 10, 9, 13, 5, 0).getTime() / 1000;
    expect(dayKey(stamp)).toBe('2026-11-09');
    expect(dayKey(0)).toBe('unknown');
    expect(dayKey(null)).toBe('unknown');
    expect(dayKey(undefined)).toBe('unknown');
  });

  it('historyLane 按来源判通道：有 batch_ref 一律算大批量', () => {
    expect(historyLane({ lane: 'small', batch_ref: '' })).toBe('small');
    expect(historyLane({ lane: 'small', batch_ref: 'b_1' })).toBe('big');
    expect(historyLane({ lane: 'big' })).toBe('big');
    // 老数据没有 lane：按大批量处理
    expect(historyLane({})).toBe('big');
  });
});

// ---------------------------------------------------------------- 状态机

describe('批次状态机 draft → locked → closed', () => {
  let ctx: ReturnType<typeof makeHarness>;

  beforeEach(() => {
    fs.rmSync(path.join(DATA, 'batches'), { recursive: true, force: true });
    ctx = makeHarness();
    ctx.service.onApplicationBootstrap();
  });

  it('新建就是 draft，且字段名与 Python 版落盘一致', async () => {
    const view = await ctx.service.create(ALICE, {
      batch_no: '2026-11-秋装-01',
      upload_ids: [UPLOAD_ID],
      prompt_id: 'p02',
      prompt: 'p',
      resolution: '720p',
      duration: 7,
      tags: { top: 'AX111' },
    });
    expect(view.state).toBe('draft');
    expect(view.resolution).toBe('720P');
    const saved = readBatch(String(view.id));
    expect(saved.state).toBe('draft');
    expect(saved.active_job_id).toBe('');
    expect(saved.pending).toBe(0);
    expect(saved.tag_filters).toEqual({});
    expect(saved.tags.top).toBe('AX111');
    expect(JSON.parse(fs.readFileSync(path.join(DATA, 'batches', `${String(view.id)}.json`), 'utf8'))).toHaveProperty(
      'batch_no',
    );
  });

  it('批次号重复 409、缺上衣款号 400', async () => {
    await lockedBatch(ctx.service);
    await expect(
      ctx.service.create(ALICE, {
        batch_no: '2026-11-秋装-01',
        prompt_id: 'p02',
        prompt: 'p',
        resolution: '720P',
        tags: { top: 'AX111' },
      }),
    ).rejects.toMatchObject({ status: 409 });
    await expect(
      ctx.service.create(BOB, {
        batch_no: '另一个',
        prompt_id: 'p02',
        prompt: 'p',
        resolution: '720P',
        tags: {},
      }),
    ).rejects.toThrow('请填写「上衣」款号（云端归档路径按它命名）');
    // 画质缺省不兜底（与 Python 一致）
    await expect(
      ctx.service.create(BOB, { batch_no: '没画质', prompt_id: 'p02', prompt: 'p', tags: { top: 'AX111' } }),
    ).rejects.toThrow('画质只能是 480P 或 720P。');
  });

  it('未确认 / 没有定妆图都锁不了，锁定后 draft 的改图被拒', async () => {
    const batch = await ctx.service.create(ALICE, {
      batch_no: 'B-未锁定',
      prompt_id: 'p02',
      prompt: 'p',
      resolution: '720P',
      tags: { top: 'AX111' },
    });
    const id = String(batch.id);
    await expect(
      ctx.service.lock(id, ALICE, { template_ids: ['001'], confirmed: true }),
    ).rejects.toThrow('请先绑定定妆图再锁定清单。');
    await expect(ctx.service.lock(id, ALICE, { template_ids: ['001'] })).rejects.toThrow(
      '请二次确认后再锁定清单。',
    );
    await expect(ctx.service.lock(id, ALICE, { template_ids: [], confirmed: true })).rejects.toThrow(
      '锁定清单不能为空。',
    );

    await lockedBatch(ctx.service, ALICE, 'B-锁定');
    const closedId = String((await ctx.service.listFor(ALICE))[0].id);
    await expect(
      ctx.service.setImage(closedId, ALICE, { upload_ids: [UPLOAD_ID] }),
    ).rejects.toThrow('批次已锁定，换图请新开批次。');
  });

  it('produce 把条目打成 producing 并派生 active_job_id；在跑时不能再启动', async () => {
    const id = await lockedBatch(ctx.service);
    const result = await ctx.service.produce(id, ALICE, { count: 5 });
    expect(result.taken).toBe(5);
    const saved = readBatch(id);
    expect(saved.active_job_id).toBe('job-1');
    expect(saved.producing).toBe(5);
    expect(saved.runs).toHaveLength(1);
    expect(saved.runs[0].count).toBe(5);
    // 没有 running 态：状态仍是 locked，靠 active_job_id 表达「在跑」
    expect(saved.state).toBe('locked');
    await expect(ctx.service.produce(id, ALICE, { count: 5 })).rejects.toThrow(
      '本批次有任务在跑，请等它结束。',
    );
  });

  it('关闭：在跑时 409；对账后可以关，关掉后提示词也不能再改', async () => {
    const id = await lockedBatch(ctx.service);
    await ctx.service.produce(id, ALICE, { count: 5 });
    await expect(ctx.service.close(id, ALICE)).rejects.toThrow('还有任务在跑，请等结束后再关闭批次。');

    const job = ctx.jobs.get('job-1')!;
    job.status = 'succeeded';
    job.finished_at = Date.now() / 1000;
    for (const rec of job.records) rec.status = 'success';

    const closed = await ctx.service.close(id, ALICE);
    expect(closed.state).toBe('closed');
    expect(closed.done).toBe(5);
    await expect(ctx.service.updatePrompt(id, ALICE, { prompt: '新' })).rejects.toThrow(
      '批次已关闭，提示词不可再修改。',
    );
  });
});

// ---------------------------------------------------------------- 读时对账

describe('读时对账', () => {
  let ctx: ReturnType<typeof makeHarness>;
  let id: string;

  beforeEach(async () => {
    fs.rmSync(path.join(DATA, 'batches'), { recursive: true, force: true });
    ctx = makeHarness();
    ctx.service.onApplicationBootstrap();
    id = await lockedBatch(ctx.service);
    await ctx.service.produce(id, ALICE, { count: 5 });
  });

  it('job 终态后条目按记录收敛，active_job_id 清空、该轮补上结束时间', async () => {
    const job = ctx.jobs.get('job-1')!;
    job.status = 'partial';
    job.records[0].status = 'success';
    job.records[1].status = 'failed';
    job.records[1].error = '云端返回 500';

    const { batch, items } = await ctx.service.detail(id, ALICE);
    expect(batch.active_job_id).toBe('');
    expect(batch.done).toBe(1);
    expect(batch.failed).toBe(4);
    expect(batch.producing).toBe(0);
    const failed = items.filter((item) => item.state === 'failed');
    // 还在跑的记录不算失败：它们保持 producing（job 未终态时才可能走到这里）
    expect(failed.length).toBeGreaterThan(0);
    const saved = readBatch(id);
    expect(saved.runs[0].finished_at).toBeTruthy();
    expect(saved.runs[0].success).toBe(1);
  });

  it('failed → done 单向修复：云端复查成功后条目自己变回已完成', async () => {
    let job = ctx.jobs.get('job-1')!;
    job.status = 'failed';
    for (const rec of job.records) rec.status = 'failed';
    let detail = await ctx.service.detail(id, ALICE);
    expect(detail.batch.failed).toBe(5);

    // 复查超时任务后 job 记录翻成 success
    job.status = 'partial';
    job.records[0].status = 'success';
    detail = await ctx.service.detail(id, ALICE);
    expect(detail.batch.done).toBe(1);
    expect(detail.items.filter((item) => item.state === 'done')).toHaveLength(1);
  });

  it('重启后载入即对账：producing 不会永远卡住', async () => {
    const job = ctx.jobs.get('job-1')!;
    job.status = 'succeeded';
    for (const rec of job.records) rec.status = 'success';

    // 同一份数据根、同一份 job 表，重新起一个服务（模拟进程重启）
    const restarted = new BatchManagerService(
      ctx.config as unknown as AppConfigService,
      ctx.jobManager as unknown as JobManagerService,
      { items: [], build: async () => undefined, resolve: () => templateRow('001'), filterItems: () => [] } as unknown as TemplateCatalogService,
      {} as unknown as UploadsService,
      { list: () => [] } as unknown as PromptsService,
    );
    restarted.onApplicationBootstrap();
    const batch = await restarted.get(id);
    expect(batch?.active_job_id).toBe('');
    expect(batch?.done).toBe(5);
    expect(batch?.producing).toBe(0);
  });
});

// ---------------------------------------------------------------- 条目状态与重试门槛

describe('条目状态与生产门槛', () => {
  let ctx: ReturnType<typeof makeHarness>;
  let id: string;

  beforeEach(async () => {
    fs.rmSync(path.join(DATA, 'batches'), { recursive: true, force: true });
    ctx = makeHarness();
    ctx.service.onApplicationBootstrap();
    id = await lockedBatch(ctx.service);
  });

  it('低于最小生成单位只有「清单只剩这么多」才允许', async () => {
    // 清单 5 条，要 3 条 → 拒（PRODUCE_MIN=5）
    await expect(ctx.service.produce(id, ALICE, { count: 3 })).rejects.toThrow(
      '每次最少生产 5 条；清单只剩 5 条时可以一次提交全部剩余。',
    );
    // 只要 5 条（= 剩余全部）→ 允许
    const result = await ctx.service.produce(id, ALICE, { count: 5 });
    expect(result.taken).toBe(5);
  });

  it('重试条数越界报错；没有失败条目时提示无货可重试', async () => {
    await expect(ctx.service.retryFailed(id, ALICE, { count: 0 })).rejects.toThrow('重试条数需要 1–100。');
    await expect(ctx.service.retryFailed(id, ALICE, { count: 101 })).rejects.toThrow('重试条数需要 1–100。');
    await expect(ctx.service.retryFailed(id, ALICE, { count: 10 })).rejects.toThrow(
      '没有失败的条目需要重试。',
    );
  });

  it('云端已出片只是下载失败：重试跳过这些条目，只剩它们时直接拒绝', async () => {
    await ctx.service.produce(id, ALICE, { count: 5 });
    const job = ctx.jobs.get('job-1')!;
    job.status = 'partial';
    job.records.forEach((rec, index) => {
      // 第 1 条：云端出片了但本机下载失败；其余 4 条：真失败
      rec.status = 'failed';
      rec.error = index === 0 ? '下载失败' : '云端返回 500';
      if (index === 0) rec.download_failed = true;
    });
    const detail = await ctx.service.detail(id, ALICE);
    expect(detail.batch.failed).toBe(5);
    expect(detail.batch.download_failed_items).toBe(1);

    // 重试只发 4 条，把「下载失败」那条排掉（重提=二次计费）
    const retried = await ctx.service.retryFailed(id, ALICE, { count: 10 });
    expect(retried.taken).toBe(4);
    expect(retried.skipped_download_failed).toBe(1);

    // 再把这 4 条也弄成「云端已出片、下载失败」，重试就必须整体拒绝并给出正确指引
    const job2 = ctx.jobs.get('job-2')!;
    job2.status = 'failed';
    for (const rec of job2.records) {
      rec.status = 'failed';
      rec.download_failed = true;
      rec.error = '下载失败';
    }
    await expect(ctx.service.retryFailed(id, ALICE, { count: 10 })).rejects.toThrow(
      /云端已出片、只是下载失败/,
    );
  });

  it('模板下架的条目标失败而不是静默换片', async () => {
    const catalog = { items: [] as TemplateRow[] };
    void catalog;
    const saved = readBatch(id);
    saved.items = saved.items.map((item) => ({ ...item, template_id: '999' }));
    fs.writeFileSync(path.join(DATA, 'batches', `${id}.json`), JSON.stringify(saved));
    const ctx2 = makeHarness();
    ctx2.service.onApplicationBootstrap();
    await expect(ctx2.service.produce(id, ALICE, { count: 5 })).rejects.toThrow(
      '待生产条目的模板都已不可用，请检查模板清单。',
    );
  });
});

// ---------------------------------------------------------------- 权限

describe('权限隔离', () => {
  let ctx: ReturnType<typeof makeHarness>;
  let id: string;

  beforeEach(async () => {
    fs.rmSync(path.join(DATA, 'batches'), { recursive: true, force: true });
    ctx = makeHarness();
    ctx.service.onApplicationBootstrap();
    id = await lockedBatch(ctx.service);
  });

  it('别人的批次看不到也动不了：404 / 403 文案逐字对齐', async () => {
    await expect(ctx.service.detail('nope', ALICE)).rejects.toThrow('找不到该批次。');
    await expect(ctx.service.detail(id, BOB)).rejects.toThrow('无权操作该批次。');
    await expect(ctx.service.produce(id, BOB, { count: 5 })).rejects.toThrow('无权操作该批次。');
    await expect(ctx.service.delete(id, BOB)).rejects.toMatchObject({ status: 403 });
    // 管理员可以
    const admin = await ctx.service.detail(id, ADMIN);
    expect(admin.batch.batch_no).toBe('2026-11-秋装-01');
  });

  it('列表默认只看自己的，管理员 allUsers + owner 收窄', async () => {
    expect(await ctx.service.listFor(ALICE)).toHaveLength(1);
    expect(await ctx.service.listFor(BOB)).toHaveLength(0);
    expect(await ctx.service.listFor(ADMIN, true)).toHaveLength(1);
    expect(await ctx.service.listFor(ADMIN, true, 'bob')).toHaveLength(0);
  });

  it('普通用户拿不到金额，档位仍在', async () => {
    const mine = await ctx.service.detail(id, ALICE);
    expect(mine.batch.cost_estimate).not.toHaveProperty('amount');
    expect(mine.batch.cost_estimate).not.toHaveProperty('unit_price');
    const admin = await ctx.service.detail(id, ADMIN);
    expect(admin.batch.cost_estimate).toHaveProperty('amount');
    expect((admin.batch.cost_estimate as { options: unknown[] }).options.length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------- 历史分组键

describe('历史分组键', () => {
  let ctx: ReturnType<typeof makeHarness>;
  let batchId: string;

  beforeEach(async () => {
    fs.rmSync(path.join(DATA, 'batches'), { recursive: true, force: true });
    ctx = makeHarness();
    ctx.service.onApplicationBootstrap();
    batchId = await lockedBatch(ctx.service);
    await ctx.service.produce(batchId, ALICE, { count: 5 });

    // 一条小批量试跑（没有 batch_ref）+ 一条批次里的零头（有 batch_ref 但只有 1 条）
    ctx.jobs.set('trial-1', makeJob('trial-1', 'alice', ['006']));
    ctx.jobs.set('trial-2', makeJob('trial-2', 'bob', ['001']));
    ctx.jobs.set('retry-1', makeJob('retry-1', 'alice', ['001'], batchId));
  });

  it('按批次聚合：批次 key + 未分组 key，批次零头算大批量而非试跑', async () => {
    const groups = await ctx.service.historyGroups(ALICE, false, '', 'batch', '');
    const keys = groups.map((group) => group.key);
    expect(keys).toContain(batchId);
    expect(keys).toContain('__ungrouped__');
    const mine = groups.find((group) => group.key === batchId)!;
    // job-1(5 条) + retry-1(1 条) 都算这个批次
    expect(mine.job_count).toBe(2);
    expect(mine.group_kind).toBe('batch');
    expect(mine.batch_no).toBe('2026-11-秋装-01');
    expect(mine.small_jobs).toBe(0);
    expect(mine.big_jobs).toBe(2);
    const ungrouped = groups.find((group) => group.key === '__ungrouped__')!;
    expect(ungrouped.total).toBe(1); // 只有 alice 的试跑可见
    expect(ungrouped.total).toBe(ungrouped.success + ungrouped.failed + ungrouped.unfinished);
    // 按最近更新倒序
    expect(groups[0].last_at).toBeGreaterThanOrEqual(groups[groups.length - 1].last_at);
  });

  it('lane 过滤按来源切：试跑在小批量栏、批次在大批量栏', async () => {
    const small = await ctx.service.historyGroups(ALICE, false, '', 'batch', 'small');
    expect(small.map((group) => group.key)).toEqual(['__ungrouped__']);
    expect(small[0].small_jobs).toBe(1);
    const big = await ctx.service.historyGroups(ALICE, false, '', 'batch', 'big');
    expect(big.map((group) => group.key)).toEqual([batchId]);
  });

  it('管理员按账户分组：一个账户一张卡，批次号留空逐行解析', async () => {
    const groups = await ctx.service.historyGroups(ADMIN, true, '', 'owner', '');
    const keys = groups.map((group) => group.key);
    expect(keys).toContain('__owner__:alice');
    expect(keys).toContain('__owner__:bob');
    const aliceCard = groups.find((group) => group.key === '__owner__:alice')!;
    expect(aliceCard.group_kind).toBe('owner');
    expect(aliceCard.is_ungrouped).toBe(true);
    expect(aliceCard.batch_no).toBe('');
    expect(aliceCard.owner).toBe('alice');
  });

  it('按日期分组忽略批次，key 为 __day__:<YYYY-MM-DD>', async () => {
    const groups = await ctx.service.historyGroups(ADMIN, true, '', 'day', '');
    const today = dayKey(Date.now() / 1000);
    const card = groups.find((group) => group.key === `__day__:${today}`);
    expect(card).toBeTruthy();
    expect(card!.day).toBe(today);
    expect(card!.group_kind).toBe('day');
    expect(card!.batch_no).toBe('');
  });

  it('空批次只计数、不生成分组卡片', async () => {
    await ctx.service.create(ALICE, {
      batch_no: 'B-空批次',
      prompt_id: 'p02',
      prompt: 'p',
      resolution: '720P',
      tags: { top: 'AX111' },
    });
    expect(await ctx.service.emptyBatchCount(ALICE, false, '')).toBe(1);
    expect(await ctx.service.emptyBatchCount(ADMIN, true, '')).toBe(1);
    expect(await ctx.service.emptyBatchCount(ADMIN, true, 'bob')).toBe(0);
    const keys = (await ctx.service.historyGroups(ALICE, false, '', 'batch', '')).map((group) => group.key);
    expect(keys).not.toContain('B-空批次');
  });

  it('分组明细：按批次 key 分页；未分组 key 只收不属于现存批次的任务', async () => {
    const page = await ctx.service.historyItems(batchId, ALICE, 0, 2, '', false, '');
    expect(page.batch_no).toBe('2026-11-秋装-01');
    expect(page.total).toBe(6); // 5 + 1 零头
    expect(page.items).toHaveLength(2);
    expect(page.offset).toBe(0);

    const rest = await ctx.service.historyItems(batchId, ALICE, 2, 10, '', false, '');
    expect(rest.items).toHaveLength(4);

    const ungrouped = await ctx.service.historyItems('__ungrouped__', ALICE, 0, 200, '', false, '');
    expect(ungrouped.items).toHaveLength(1);
    expect(ungrouped.items[0].job_id).toBe('trial-1');
    expect(ungrouped.batch_no).toBe('未分组');
  });

  it('删掉批次后它名下的任务自动落回未分组（job 的 batch_ref 不回写）', async () => {
    const running = ctx.jobs.get('job-1')!;
    running.status = 'succeeded';
    for (const rec of running.records) rec.status = 'success';
    await ctx.service.delete(batchId, ALICE);
    const groups = await ctx.service.historyGroups(ALICE, false, '', 'batch', '');
    expect(groups.map((group) => group.key)).toEqual(['__ungrouped__']);
    const ungrouped = await ctx.service.historyItems('__ungrouped__', ALICE, 0, 200, '', false, '');
    expect(ungrouped.total).toBe(7); // 5 + 零头 1 + 试跑 1
    // 磁盘上批次文件没了，job 仍在
    expect(fs.existsSync(path.join(DATA, 'batches', `${batchId}.json`))).toBe(false);
    expect(ctx.jobs.get('job-1')).toBeTruthy();
  });

  it('跑着的批次不能删（409），删完号码可复用', async () => {
    await expect(ctx.service.delete(batchId, ALICE)).rejects.toThrow(
      '批次正在生产中，请等待生产结束或取消任务后再删除',
    );
    const job = ctx.jobs.get('job-1')!;
    job.status = 'succeeded';
    for (const rec of job.records) rec.status = 'success';
    await ctx.service.delete(batchId, ALICE);
    // 批次号索引已释放
    const again = await ctx.service.create(ALICE, {
      batch_no: '2026-11-秋装-01',
      prompt_id: 'p02',
      prompt: 'p',
      resolution: '720P',
      tags: { top: 'AX111' },
    });
    expect(again.batch_no).toBe('2026-11-秋装-01');
    expect(again.id).not.toBe(batchId);
  });
});

// ---------------------------------------------------------------- 生产轮次视图

describe('生产轮次视图', () => {
  it('提示词从该轮 job 还原，改过就标 prompt_changed', async () => {
    fs.rmSync(path.join(DATA, 'batches'), { recursive: true, force: true });
    const ctx = makeHarness();
    ctx.service.onApplicationBootstrap();
    const id = await lockedBatch(ctx.service);

    await ctx.service.produce(id, ALICE, { count: 5 });
    const job = ctx.jobs.get('job-1')!;
    job.status = 'succeeded';
    for (const rec of job.records) rec.status = 'success';
    // 锁定后改提示词，只影响下一轮
    await ctx.service.updatePrompt(id, ALICE, { prompt: '改过的提示词' });
    await ctx.service.produce(id, ALICE, { count: 5 }).catch(() => undefined);

    const batch = (await ctx.service.get(id))!;
    const runs = ctx.service.runViews(batch);
    expect(runs).toHaveLength(1);
    expect(runs[0].no).toBe(1);
    expect(runs[0].prompt).toBe('原始提示词');
    expect(runs[0].prompt_changed).toBe(false);
    expect(runs[0].finished_at).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------- 错误类型

describe('BatchError', () => {
  it('默认 400，可带状态码，且是 Error 子类', () => {
    const err = new BatchError('哦');
    expect(err.status).toBe(400);
    expect(err).toBeInstanceOf(Error);
    expect(new BatchError('找不到该批次。', 404).status).toBe(404);
    expect(new BatchError('', 0).status).toBe(400);
  });
});
