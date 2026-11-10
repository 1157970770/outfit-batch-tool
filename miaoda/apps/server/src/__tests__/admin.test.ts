/**
 * 管理端接口离线回归：口径错一点，管理员就会「看不到钱 / 改不了配额 / 删错账号」。
 *
 * 全部离线：临时目录造一份 web.json + 假的任务 / 批次 / 候选集，
 * 直接 new `AdminService`（不经 Nest DI），不连网、不读真实配置。
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { HttpException } from '@nestjs/common';
import { afterAll, describe, expect, it, vi } from 'vitest';

import { WEB_VERSION } from '../api/health.controller';
import { verifyPassword } from '../auth/password';
import type { BatchManagerService } from '../batches/batch-manager.service';
import { writeJsonAtomic } from '../common/fs-json';
import type { AppConfigService } from '../config/app-config.service';
import type { Job, JobManagerService } from '../jobs/job-manager.service';
import type { TemplateCatalogService } from '../templates/template-catalog.service';

import { ARCHIVE_DISABLED_TEXT, AdminService } from '../admin/admin.service';

// ---------------------------------------------------------------- 夹具

const ROOTS: string[] = [];

/** 本地时间 2026-11-10 09:00 的秒级时间戳（和 Python `_day` 一样按本地时区算）。 */
const T0 = new Date(2026, 10, 10, 9, 0, 0).getTime() / 1000;

const ADMIN_RECORD = {
  salt: 'admin-salt',
  hash: 'admin-hash',
  rounds: 310_000,
  role: 'admin',
  enabled: true,
  version: 1,
  expires: '',
  quota: 0,
};

const ALICE_RECORD = {
  salt: 'alice-salt',
  hash: 'alice-hash',
  rounds: 310_000,
  role: 'user',
  enabled: true,
  version: 3,
  expires: '2026-12-31',
  quota: 100,
};

const ADMIN = { username: 'admin' };
const ALICE = { username: 'alice' };

interface Harness {
  root: string;
  data: string;
  webFile: string;
  web: Record<string, unknown>;
  service: AdminService;
  build: ReturnType<typeof vi.fn>;
  setJobs: (rows: Array<Record<string, unknown>>) => void;
  users: () => Record<string, Record<string, unknown>>;
}

function makeHarness(options: {
  jobs?: Array<Record<string, unknown>>;
  batches?: Record<string, string>;
  info?: Record<string, unknown>;
  items?: number;
} = {}): Harness {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-admin-'));
  ROOTS.push(root);
  const data = path.join(root, 'web-data');
  fs.mkdirSync(data, { recursive: true });
  const webFile = path.join(root, 'web.json');
  const web: Record<string, unknown> = {
    port: 8766,
    host: '127.0.0.1',
    data_root: data,
    session_secret: 'keep-me-secret',
    notice: '这条公告不能被写丢',
    users: { admin: { ...ADMIN_RECORD }, alice: { ...ALICE_RECORD } },
  };
  writeJsonAtomic(webFile, web);

  let jobRows = options.jobs ?? [];
  const info = options.info ?? {};
  const build = vi.fn(async (_force?: boolean) => undefined);
  const catalog = {
    items: Array.from({ length: options.items ?? 3 }, (_, index) => ({ id: String(index + 1).padStart(3, '0') })),
    info,
    build,
    error: '',
  };
  const jobManager = {
    list(_owner: string, _admin: boolean, ownerFilter = ''): Job[] {
      return jobRows.filter((row) => !ownerFilter || String(row.owner) === ownerFilter) as unknown as Job[];
    },
  };
  const batchMap = options.batches ?? {};
  const batches = {
    async get(batchId: string) {
      const batchNo = batchMap[batchId];
      return batchNo ? ({ id: batchId, batch_no: batchNo } as unknown as never) : undefined;
    },
  };
  const config = {
    dataDir: data,
    webFile,
    web,
    maxConcurrency: 3,
    maxActivePerUser: 2,
    smallLaneItems: 1,
    maxActiveSmallJobs: 5,
    smallLaneInflight: 12,
    submitRpm: 250,
    queryQps: 15,
    maxInflight: 100,
    unitPrice: (resolution: string) => ({
      price: resolution === '1080P' ? 1.2 : 0.6,
      source: 'config' as const,
    }),
  };

  const service = new AdminService(
    config as unknown as AppConfigService,
    jobManager as unknown as JobManagerService,
    batches as unknown as BatchManagerService,
    catalog as unknown as TemplateCatalogService,
  );

  return {
    root,
    data,
    webFile,
    web,
    service,
    build,
    setJobs: (rows) => {
      jobRows = rows;
    },
    users: () => web.users as Record<string, Record<string, unknown>>,
  };
}

function makeJob(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'job-1',
    owner: 'alice',
    status: 'succeeded',
    created_at: T0,
    finished_at: T0 + 600,
    batch_id: '',
    batch_ref: '',
    lane: 'big',
    resolution: '720P',
    duration: 7,
    total: 2,
    templates: [
      { id: '001', name: '场景 001' },
      { id: '002', name: '场景 002' },
    ],
    records: [
      { id: '001', status: 'success', seconds: 7, billable_seconds: 7, estimated_cost: 4.2 },
      { id: '002', status: 'failed', seconds: 3, billable_seconds: 0, error: '引擎报错' },
    ],
    tags: {},
    style_no: '',
    ...overrides,
  };
}

/** 断言某次调用抛 HttpException，并取回状态码与中文文案。 */
async function fails(fn: () => unknown | Promise<unknown>): Promise<{ status: number; error: string }> {
  try {
    await fn();
  } catch (err) {
    expect(err).toBeInstanceOf(HttpException);
    const exception = err as HttpException;
    const response = exception.getResponse() as { ok: boolean; error: string };
    expect(response.ok).toBe(false);
    return { status: exception.getStatus(), error: response.error };
  }
  throw new Error('期望抛出 HttpException，但没有抛');
}

function readWeb(file: string): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>;
}

afterAll(() => {
  for (const root of ROOTS) {
    try {
      fs.rmSync(root, { recursive: true, force: true });
    } catch {
      /* 临时目录清理失败不影响结论 */
    }
  }
});

// ---------------------------------------------------------------- 状态 / 模板

describe('admin status', () => {
  it('下发版本、任务计数、上限与账户一览，且不含密码材料', () => {
    const harness = makeHarness({
      jobs: [
        makeJob({ id: 'job-1', status: 'running', lane: 'small' }),
        makeJob({ id: 'job-2', status: 'queued', lane: 'big' }),
        makeJob({ id: 'job-3', status: 'running', lane: 'big' }),
      ],
    });
    const body = harness.service.status();
    expect(body.ok).toBe(true);
    expect(body.version).toBe(WEB_VERSION);
    expect(body.templates).toBe(3);
    expect(body.queued).toBe(1);
    expect(body.active).toBe(2);
    expect(body.small_active).toBe(1);
    expect(body.big_active).toBe(1);
    expect(typeof body.free_gb).toBe('number');
    expect(body.max_concurrency).toBe(3);
    expect(body.max_active_per_user).toBe(2);
    expect(body.small_lane).toEqual({ items: 1, max_jobs: 5, inflight: 12 });
    expect(body.limits).toEqual({ query_rpm: 250, query_rps: 15, max_inflight: 100, small_max_inflight: 12 });
    expect(body.users).toEqual([
      { username: 'admin', role: 'admin', enabled: true },
      { username: 'alice', role: 'user', enabled: true },
    ]);
    expect(JSON.stringify(body)).not.toContain('admin-salt');
    expect(JSON.stringify(body)).not.toContain('admin-hash');
  });
});

describe('admin template-health', () => {
  it('按候选集口径换算比例，并把 manifest 警告带出来', () => {
    const harness = makeHarness({
      info: {
        workbook_rows: 10,
        matched_templates: 8,
        source_ready_templates: 7,
        manifest_unmatched: 2,
        untagged_skipped: 1,
        manifest_warning: '缺 2 个视频',
      },
    });
    const body = harness.service.templateHealth();
    expect(body).toEqual({
      ok: true,
      workbook_rows: 10,
      tagged_rows: 8,
      source_ready: 7,
      unmatched: 2,
      untagged: 1,
      ready_ratio: 0.7,
      manifest_warning: '缺 2 个视频',
    });
  });

  it('没有任何表格行时比例给 0，而不是 NaN', () => {
    const body = makeHarness({ info: {} }).service.templateHealth();
    expect(body.ready_ratio).toBe(0);
    expect(body.workbook_rows).toBe(0);
    expect(body.manifest_warning).toBe('');
  });
});

describe('admin reload-templates', () => {
  it('强制重建候选集，返回计数与体检字段', async () => {
    const harness = makeHarness({
      items: 4,
      info: {
        source_ready_templates: 3,
        manifest_unmatched: 1,
        untagged_skipped: 2,
        manifest_warning: '清单里有 1 个对不上',
      },
    });
    const body = await harness.service.reloadTemplates();
    expect(harness.build).toHaveBeenCalledWith(true);
    expect(body).toEqual({
      ok: true,
      count: 4,
      source_ready_templates: 3,
      manifest_unmatched: 1,
      untagged_skipped: 2,
      manifest_warning: '清单里有 1 个对不上',
    });
  });

  it('重建抛错时转成 503 与「重载失败：」前缀', async () => {
    const harness = makeHarness();
    harness.build.mockRejectedValueOnce(new Error('manifest 拉不下来'));
    const failure = await fails(() => harness.service.reloadTemplates());
    expect(failure.status).toBe(503);
    expect(failure.error).toBe('重载失败：manifest 拉不下来');
  });
});

// ---------------------------------------------------------------- 用量

describe('admin usage', () => {
  it('按账户聚合条数、计费秒与金额，并按金额倒序', () => {
    const harness = makeHarness({
      jobs: [
        makeJob(),
        makeJob({
          id: 'job-2',
          owner: 'bob',
          records: [{ id: '001', status: 'success', seconds: 10, billable_seconds: 10 }],
          templates: [{ id: '001', name: '场景 001' }],
        }),
      ],
    });
    const body = harness.service.usage({});
    const accounts = body.accounts as Array<Record<string, unknown>>;
    // web.json 里的账户即便没有任务也会占一行（Python usage_for 同口径）
    expect(accounts.map((row) => row.owner)).toEqual(['bob', 'alice', 'admin']);
    const bob = accounts[0];
    // 没记 estimated_cost 的老数据按当前单价回算：10 秒 × 0.6 = 6
    expect(bob.estimated_cost).toBe(6);
    expect(bob.success_rate).toBe(1);
    const alice = accounts[1];
    expect(alice).toMatchObject({
      role: 'user',
      enabled: true,
      jobs: 1,
      items: 2,
      success: 1,
      failed: 1,
      unfinished: 0,
      billable_seconds: 7,
      estimated_cost: 4.2,
    });
    const totals = body.totals as Record<string, number>;
    expect(totals).toMatchObject({
      jobs: 2,
      items: 3,
      success: 2,
      failed: 1,
      unfinished: 0,
      billable_seconds: 17,
      estimated_cost: 10.2,
      accounts: 3,
    });
    expect(body.ok).toBe(true);
    expect(body.owner).toBe('');
    expect(body.from).toBe('');
    expect(body.to).toBe('');
  });

  it('按 owner 与 from/to 过滤（to 含结束当天）', () => {
    const harness = makeHarness({
      jobs: [
        makeJob({ id: 'job-1', created_at: T0 }),
        makeJob({ id: 'job-2', owner: 'bob', created_at: T0 }),
      ],
    });
    const onlyAlice = harness.service.usage({ owner: 'alice' });
    expect((onlyAlice.accounts as Array<Record<string, unknown>>).map((row) => row.owner)).toEqual(['alice', 'admin']);
    expect((onlyAlice.totals as Record<string, number>).jobs).toBe(1);

    const sameDay = harness.service.usage({ from: '2026-11-10', to: '2026-11-10' });
    expect((sameDay.totals as Record<string, number>).jobs).toBe(2);

    const dayBefore = harness.service.usage({ to: '2026-11-09' });
    expect((dayBefore.totals as Record<string, number>).jobs).toBe(0);
  });
});

// ---------------------------------------------------------------- 账户列表

describe('admin users', () => {
  it('下发配额与已用条数，绝不返回 salt/hash', () => {
    const harness = makeHarness({ jobs: [makeJob()] });
    const body = harness.service.listUsers();
    expect(body.users).toEqual([
      {
        username: 'admin',
        role: 'admin',
        enabled: true,
        quota: 0,
        used: 0,
        expires: '',
      },
      {
        username: 'alice',
        role: 'user',
        enabled: true,
        quota: 100,
        used: 1,
        expires: '2026-12-31',
      },
    ]);
    const serialized = JSON.stringify(body);
    expect(serialized).not.toContain('alice-salt');
    expect(serialized).not.toContain('alice-hash');
  });

  it('配额脏数据（空串 / 负数 / 非数字）一律按 0 处理', () => {
    const harness = makeHarness();
    harness.users().alice.quota = 'abc';
    harness.users().admin.quota = -5;
    const rows = harness.service.listUsers().users as Array<Record<string, unknown>>;
    expect(rows.find((row) => row.username === 'alice')?.quota).toBe(0);
    expect(rows.find((row) => row.username === 'admin')?.quota).toBe(0);
  });
});

// ---------------------------------------------------------------- 新增 / 重置 / 配额

describe('admin add user', () => {
  it('写入 web.json：新账号可校验密码，其它配置项原样保留', async () => {
    const harness = makeHarness();
    const body = await harness.service.addUser({ username: 'carol', password: 'pw-123456', role: 'admin' });
    expect(body).toEqual({ ok: true, username: 'carol' });

    const saved = readWeb(harness.webFile);
    // users 之外的字段不能被写丢
    expect(saved.notice).toBe('这条公告不能被写丢');
    expect(saved.session_secret).toBe('keep-me-secret');
    const carol = (saved.users as Record<string, Record<string, unknown>>).carol;
    expect(carol.role).toBe('admin');
    expect(carol.enabled).toBe(true);
    expect(carol.version).toBe(1);
    expect(carol.expires).toBe('');
    expect(carol.rounds).toBe(310_000);
    expect(await verifyPassword('pw-123456', carol as unknown as { salt: string; hash: string; rounds: number })).toBe(
      true,
    );
    expect(await verifyPassword('pw-1234567', carol as unknown as { salt: string; hash: string; rounds: number })).toBe(
      false,
    );
  });

  it('用户名 / 重名 / 密码长度 / 角色四类校验文案与状态码', async () => {
    const harness = makeHarness();
    expect(await fails(() => harness.service.addUser({ username: 'a', password: 'pw-123456' }))).toEqual({
      status: 400,
      error: '账号请使用 2–32 位字母、数字、下划线、点或短横线。',
    });
    expect(await fails(() => harness.service.addUser({ username: 'bad name', password: 'pw-123456' }))).toEqual({
      status: 400,
      error: '账号请使用 2–32 位字母、数字、下划线、点或短横线。',
    });
    expect(await fails(() => harness.service.addUser({ username: 'alice', password: 'pw-123456' }))).toEqual({
      status: 409,
      error: '账号已存在。',
    });
    expect(await fails(() => harness.service.addUser({ username: 'dave', password: 'short' }))).toEqual({
      status: 400,
      error: '密码至少需要 8 位。',
    });
    expect(
      await fails(() => harness.service.addUser({ username: 'dave', password: 'pw-123456', role: 'root' })),
    ).toEqual({ status: 400, error: '用户角色无效。' });
  });
});

describe('admin reset password', () => {
  it('换掉 salt/hash 并 bump version，旧会话被踢下线', async () => {
    const harness = makeHarness();
    const body = await harness.service.resetPassword('alice', { password: 'new-pw-123' });
    expect(body).toEqual({ ok: true });
    const saved = (readWeb(harness.webFile).users as Record<string, Record<string, unknown>>).alice;
    expect(saved.version).toBe(4);
    expect(saved.salt).not.toBe('alice-salt');
    expect(saved.hash).not.toBe('alice-hash');
    expect(await verifyPassword('new-pw-123', saved as unknown as { salt: string; hash: string; rounds: number })).toBe(
      true,
    );
    // 配额等其它字段不受影响
    expect(saved.quota).toBe(100);
    expect(saved.expires).toBe('2026-12-31');
  });

  it('账号不存在 404、新密码太短 400', async () => {
    const harness = makeHarness();
    expect(await fails(() => harness.service.resetPassword('nobody', { password: 'new-pw-123' }))).toEqual({
      status: 404,
      error: '账号不存在。',
    });
    expect(await fails(() => harness.service.resetPassword('alice', { password: 'short' }))).toEqual({
      status: 400,
      error: '新密码至少需要 8 位。',
    });
  });
});

describe('admin set quota', () => {
  it('改上限并给出「已用 N 条」的提示，不 bump version', () => {
    const harness = makeHarness({ jobs: [makeJob()] });
    const body = harness.service.setQuota('alice', { quota: 20 });
    expect(body).toEqual({
      ok: true,
      username: 'alice',
      quota: 20,
      used: 1,
      note: '该账户已用 1 条，新上限即时生效。',
    });
    const saved = (readWeb(harness.webFile).users as Record<string, Record<string, unknown>>).alice;
    expect(saved.quota).toBe(20);
    expect(saved.version).toBe(3);
  });

  it('0 表示不限；非整数与越界分别是两条不同文案', () => {
    const harness = makeHarness();
    const unlimited = harness.service.setQuota('alice', { quota: 0 });
    expect(unlimited.note).toBe('该账户已用 0 条，已改为不限。');

    expect(harness.service.setQuota('alice', { quota: 15.9 }).quota).toBe(15);
    expect(harness.service.setQuota('alice', { quota: '30' }).quota).toBe(30);

    expect(() => harness.service.setQuota('alice', { quota: 'abc' })).toThrow(HttpException);
    expect(() => harness.service.setQuota('alice', { quota: -1 })).toThrow(HttpException);
    expect(() => harness.service.setQuota('alice', { quota: 1_000_001 })).toThrow(HttpException);
    expect(() => harness.service.setQuota('nobody', { quota: 1 })).toThrow(HttpException);
  });
});

// ---------------------------------------------------------------- 停用 / 启用 / 删除

describe('admin enable / disable / delete', () => {
  it('不能停用自己，也不能停用最后一个可用管理员', async () => {
    const harness = makeHarness();
    expect(await fails(() => harness.service.disableUser('admin', ADMIN))).toEqual({
      status: 409,
      error: '不能停用当前登录的管理员账号。',
    });

    // 只剩一个启用的管理员时，停用它要拦下来（actor 是另一个已停用的管理员）
    harness.users().admin.enabled = false;
    harness.users().root = { ...ADMIN_RECORD, role: 'admin', enabled: false };

    const single = makeHarness();
    single.users().root = { ...ADMIN_RECORD, role: 'admin', enabled: false };
    expect(await fails(() => single.service.disableUser('admin', { username: 'root' }))).toEqual({
      status: 409,
      error: '至少保留一个可用管理员。',
    });

    expect(await fails(() => harness.service.enableUser('ghost'))).toEqual({ status: 404, error: '账号不存在。' });
  });

  it('停用普通账号会 bump version，启用后恢复', () => {
    const harness = makeHarness();
    expect(harness.service.disableUser('alice', { username: 'admin' })).toEqual({ ok: true });
    let saved = (readWeb(harness.webFile).users as Record<string, Record<string, unknown>>).alice;
    expect(saved.enabled).toBe(false);
    expect(saved.version).toBe(4);

    expect(harness.service.enableUser('alice')).toEqual({ ok: true });
    saved = (readWeb(harness.webFile).users as Record<string, Record<string, unknown>>).alice;
    expect(saved.enabled).toBe(true);
    expect(saved.version).toBe(5);
  });

  it('删除账号：不能删自己、不存在 404、真删后从 web.json 消失', () => {
    const harness = makeHarness();
    expect(() => harness.service.deleteUser('admin', { username: 'admin' })).toThrow(HttpException);
    expect(() => harness.service.deleteUser('nobody', { username: 'admin' })).toThrow(HttpException);
    expect(harness.service.deleteUser('alice', { username: 'admin' })).toEqual({ ok: true, username: 'alice' });
    const saved = readWeb(harness.webFile);
    expect((saved.users as Record<string, unknown>).alice).toBeUndefined();
    expect((saved.users as Record<string, unknown>).admin).toBeDefined();
    expect(saved.notice).toBe('这条公告不能被写丢');
  });
});

// ---------------------------------------------------------------- 归档

describe('admin archive', () => {
  it('按款号命中本机账本：带批次号、来源 ledger、按时间倒序', async () => {
    const harness = makeHarness({
      batches: { 'batch-1': '20261110-01' },
      jobs: [
        makeJob({
          tags: { style: 'AB-12' },
          batch_ref: 'batch-1',
          records: [
            { id: '001', status: 'success', cos_key: 'out/AB-12/1.mp4' },
            { id: '002', status: 'failed', cos_key: 'out/AB-12/2.mp4' },
          ],
        }),
        makeJob({ id: 'job-0', created_at: T0 - 3600, finished_at: T0 - 1800, style_no: 'ab-12' }),
      ],
    });
    const body = await harness.service.archiveSearch({ q: 'ab-12' });
    expect(body.ok).toBe(true);
    expect(body.style).toBe('ab-12');
    expect(body.source).toBe('ledger');
    expect(body.total).toBe(1);
    const entries = body.entries as Array<Record<string, unknown>>;
    expect(entries[0]).toMatchObject({
      key: 'out/AB-12/1.mp4',
      job_id: 'job-1',
      record_id: '001',
      template_id: '001',
      owner: 'alice',
      batch_ref: 'batch-1',
      batch_no: '20261110-01',
      created_at: T0 + 600,
    });
  });

  it('空款号 400、超长 400；账本没命中时按 COS 未配置 503（不伪造结果）', async () => {
    const harness = makeHarness();
    expect(await fails(() => harness.service.archiveSearch({}))).toEqual({
      status: 400,
      error: '请输入要搜索的款号。',
    });
    expect(await fails(() => harness.service.archiveSearch({ style: 'x'.repeat(61) }))).toEqual({
      status: 400,
      error: '款号过长。',
    });
    expect(await fails(() => harness.service.archiveSearch({ style: 'ZZ-99' }))).toEqual({
      status: 503,
      error: ARCHIVE_DISABLED_TEXT,
    });
  });

  it('archive-sign：缺 keys 400、路径不可用 400、其余统一 503', async () => {
    const harness = makeHarness();
    expect(await fails(() => harness.service.archiveSign({}))).toEqual({
      status: 400,
      error: '请先勾选要生成链接的视频。',
    });
    expect(await fails(() => harness.service.archiveSign({ keys: [] }))).toEqual({
      status: 400,
      error: '请先勾选要生成链接的视频。',
    });
    expect(await fails(() => harness.service.archiveSign({ keys: ['../etc/passwd'] }))).toEqual({
      status: 400,
      error: '没有可用的对象路径。',
    });
    expect(await fails(() => harness.service.archiveSign({ keys: ['/out/a.mp4'] }))).toEqual({
      status: 503,
      error: '云端归档未启用，无法生成下载链接。',
    });
  });
});

// ---------------------------------------------------------------- 账户明细 / CSV

describe('admin account work', () => {
  it('分页、排序、计数与金额汇总都按 Python 口径', async () => {
    const harness = makeHarness({
      batches: { 'batch-1': '20261110-01' },
      jobs: [
        makeJob({ batch_ref: 'batch-1' }),
        makeJob({ id: 'job-2', created_at: T0 + 3000, finished_at: T0 + 3600 }),
      ],
    });
    const body = await harness.service.accountWork('alice', {});
    expect(body.ok).toBe(true);
    expect(body.owner).toBe('alice');
    expect(body.total).toBe(4);
    expect(body.success).toBe(2);
    expect(body.failed).toBe(2);
    expect(body.unfinished).toBe(0);
    expect(body.offset).toBe(0);
    expect(body.limit).toBe(100);
    expect(body.total_cost).toBe(8.4);
    expect(body.total_billable_seconds).toBe(14);
    expect(body.status).toBe('');
    const items = body.items as Array<Record<string, unknown>>;
    expect(items).toHaveLength(4);
    // 新的在前
    expect(items[0].job_id).toBe('job-2');
    expect(items[3].job_id).toBe('job-1');
    expect(items[3]).toMatchObject({
      batch_no: '20261110-01',
      template_name: '场景 001',
      status: 'success',
      billable_seconds: 7,
      estimated_cost: 4.2,
      cost_source: 'record',
      resolution: '720P',
      duration: 7,
    });
    // 失败的那条没记成本 → 无单价可回算 → 金额留空
    expect(items[2]).toMatchObject({ status: 'failed', estimated_cost: null, cost_source: 'missing' });

    const page = await harness.service.accountWork('alice', { offset: '1', limit: '2' });
    expect((page.items as unknown[]).length).toBe(2);
    expect(page.total).toBe(4);
    expect(page.offset).toBe(1);
  });

  it('status 白名单内的筛选生效，范围外的按不筛选处理；分页参数非整数 400', async () => {
    const harness = makeHarness({ jobs: [makeJob()] });
    const failed = await harness.service.accountWork('alice', { status: 'failed' });
    expect(failed.total).toBe(1);
    const bogus = await harness.service.accountWork('alice', { status: 'timeout' });
    expect(bogus.total).toBe(2);
    expect(bogus.status).toBe('');
    expect(await fails(() => harness.service.accountWork('alice', { offset: 'abc' }))).toEqual({
      status: 400,
      error: '分页参数无效。',
    });
  });

  it('CSV：带 BOM、表头逐字一致、公式注入被转义、文件名按 UTF-8 编码', async () => {
    const harness = makeHarness({
      batches: { 'batch-1': '20261110-01' },
      jobs: [
        makeJob({
          batch_ref: 'batch-1',
          templates: [
            { id: '001', name: '=cmd|calc' },
            { id: '002', name: '场景 002' },
          ],
        }),
      ],
    });
    const csv = await harness.service.accountCsv('alice', {});
    expect(csv.filename).toBe('account-alice-work.csv');
    expect(csv.body.startsWith('\uFEFF')).toBe(true);
    const lines = csv.body.replace(/^\uFEFF/, '').split('\r\n');
    expect(lines[0]).toBe('时间,批次号,模板ID,模板名称,状态,计费秒,预估金额(元),金额来源,分辨率,输出时长(秒),任务ID');
    // 排序倒序：002 在前
    expect(lines[1]).toBe('2026-11-10 09:00:00,20261110-01,002,场景 002,失败,0,,,720P,7,job-1');
    expect(lines[2]).toContain("'=cmd|calc");
    expect(lines[2]).toContain('成功');
    expect(lines[2]).toContain('4.2');
    expect(lines[2]).toContain('实测');
    // 全量导出：3 行表头 + 2 行数据 + 结尾换行
    expect(lines).toHaveLength(4);
    expect(lines[3]).toBe('');
  });
});
