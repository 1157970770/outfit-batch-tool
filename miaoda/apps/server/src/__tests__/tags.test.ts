/**
 * 标签真源（TemplateStore）回归：全部离线、只写系统临时目录，绝不碰仓库里的真实数据。
 *
 * 覆盖：装载/归一化、revision 乐观锁、打标/弃用/打分、标签库与大类维护、影响预览、
 * 版本快照与回滚、文件锁与原子写。口径以 `tools/template_store.py` 为准。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterAll, describe, expect, it } from 'vitest';

import {
  NotFound,
  RevisionConflict,
  StoreError,
  StoreUnavailable,
  TemplateStore,
  cleanTags,
  clampRating,
  normalizeId,
} from '../tags/store/template-store';

const tempDirs: string[] = [];

afterAll(() => {
  for (const dir of tempDirs) {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {
      /* 临时目录删不掉不影响结果 */
    }
  }
});

/** 建一个空库（文件还没落盘），返回 store 与路径。 */
function emptyStore(): { store: TemplateStore; file: string } {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-tags-'));
  tempDirs.push(dir);
  const file = path.join(dir, 'template_tags.json');
  return { store: TemplateStore.empty(file), file };
}

/** 建一个带两个标签、两条视频的库；返回当前 revision。 */
async function seededStore(): Promise<{ store: TemplateStore; file: string; revision: number }> {
  const { store, file } = emptyStore();
  await store.libraryCreate('近景', '景别', { user: 'tester' });
  await store.libraryCreate('站立', '姿势', { user: 'tester' });
  const first = await store.addVideo({ name: '1400.mp4', tags: ['近景'], user: 'tester' });
  const second = await store.addVideo({ name: '1401.mp4', tags: ['近景', '站立'], user: 'tester' });
  expect(first.row?.id).toBe('001');
  expect(second.row?.id).toBe('002');
  return { store, file, revision: store.revision };
}

/** 把 Promise 的拒绝值当普通值取回来，方便断言错误类型与字段。 */
async function capture(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
    return null;
  } catch (err) {
    return err;
  }
}

describe('template-store 纯函数', () => {
  it('normalizeId 数字补三位、其余原样', () => {
    expect(normalizeId('7')).toBe('007');
    expect(normalizeId(7)).toBe('007');
    expect(normalizeId(' 12 ')).toBe('012');
    expect(normalizeId('a01')).toBe('a01');
    expect(normalizeId('')).toBe('');
    expect(normalizeId(null)).toBe('');
  });

  it('cleanTags 支持分号字符串、去空去重保序', () => {
    expect(cleanTags('近景; 站立 ;近景;')).toEqual(['近景', '站立']);
    expect(cleanTags(['a', '', 'a', 'b'])).toEqual(['a', 'b']);
    expect(cleanTags(undefined)).toEqual([]);
  });

  it('clampRating 钳到 0-5，非法值归 0', () => {
    expect(clampRating(5)).toBe(5);
    expect(clampRating(9)).toBe(5);
    expect(clampRating(-3)).toBe(0);
    expect(clampRating('3')).toBe(3);
    expect(clampRating('3.5')).toBe(0);
    expect(clampRating(null)).toBe(0);
  });
});

describe('template-store 装载与归一化', () => {
  it('空库 revision 为 0，默认四个大类，统计全 0', () => {
    const { store } = emptyStore();
    expect(store.revision).toBe(0);
    expect(store.rows()).toEqual([]);
    expect(store.categoryList()).toEqual(['景别', '适合季节', '场景', '姿势']);
    expect(store.stats()).toEqual({
      total: 0,
      active: 0,
      deprecated: 0,
      untagged: 0,
      missing: 0,
      changed: 0,
      uploaded: 0,
    });
  });

  it('读旧文件：未知字段保留、缺省字段补齐、视频按数字 ID 排序', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-tags-'));
    tempDirs.push(dir);
    const file = path.join(dir, 'template_tags.json');
    fs.writeFileSync(
      file,
      JSON.stringify({
        schema: 3,
        revision: 7,
        library: { tags: ['近景'], categories: { 近景: '景别' }, category_order: ['景别', '适合季节', '场景', '姿势'] },
        videos: [
          { id: '12', name: 'b.mp4', tags: ['近景'], custom_field: 'keep-me' },
          { id: '2', name: 'a.mp4', tags: [] },
        ],
        extra_top: 'keep',
      }),
      'utf8',
    );
    const store = TemplateStore.load(file);
    expect(store.revision).toBe(7);
    expect(store.rows().map((row) => row.id)).toEqual(['002', '012']);
    expect(store.row('012')?.custom_field).toBe('keep-me');
    expect((store.data as Record<string, unknown>).extra_top).toBe('keep');
    // 缺省字段补齐：评分/弃用三件套一定存在
    expect(store.row('002')?.rating).toBe(0);
    expect(store.row('002')?.deprecated).toBe(false);
  });

  it('文件损坏时从 .bak 恢复并置 recoveredFromBak', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-tags-'));
    tempDirs.push(dir);
    const file = path.join(dir, 'template_tags.json');
    const payload = {
      schema: 3,
      revision: 3,
      library: { tags: [], categories: {}, category_order: ['景别'] },
      videos: [{ id: '001', name: 'a.mp4', tags: [] }],
    };
    fs.writeFileSync(`${file}.bak`, JSON.stringify(payload), 'utf8');
    fs.writeFileSync(file, '{ 坏掉的 JSON', 'utf8');
    const store = TemplateStore.load(file);
    expect(store.recoveredFromBak).toBe(true);
    expect(store.revision).toBe(3);
    expect(store.rows()).toHaveLength(1);
  });

  it('文件损坏且没有可用备份时抛 StoreUnavailable(503)', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-tags-'));
    tempDirs.push(dir);
    const file = path.join(dir, 'template_tags.json');
    fs.writeFileSync(file, 'not json', 'utf8');
    let error: unknown = null;
    try {
      TemplateStore.load(file);
    } catch (err) {
      error = err;
    }
    expect(error).toBeInstanceOf(StoreUnavailable);
    expect((error as StoreUnavailable).status).toBe(503);
    expect((error as StoreUnavailable).message).toContain('标签真源文件损坏');
  });
});

describe('template-store 写入与乐观锁', () => {
  it('加视频：编号顺延、字段落盘、自动版本生成', async () => {
    const { store, file } = await seededStore();
    expect(store.revision).toBe(4); // 2 次建标签 + 2 次加视频
    expect(store.rows().map((row) => row.name)).toEqual(['1400.mp4', '1401.mp4']);
    expect(store.row('001')?.uploaded).toBe(true);
    expect(store.row('001')?.uploaded_at).toBeGreaterThan(0);
    expect(store.stats().total).toBe(2);
    expect(fs.existsSync(file)).toBe(true);
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>;
    expect(parsed.revision).toBe(4);
    expect((parsed.videos as unknown[]).length).toBe(2);
    // 每次成功写入都留一份自动版本
    expect(store.listVersions(100).filter((item) => item.kind === 'auto').length).toBeGreaterThan(0);
  });

  it('重名自动加 -2，autoNumber 按清单数字顺延', async () => {
    const { store } = await seededStore();
    await store.addVideo({ name: '1400.mp4', tags: ['近景'], user: 'tester' });
    expect(store.row('003')?.name).toBe('1400-2.mp4');
    await store.addVideo({ name: 'whatever.MOV', tags: ['近景'], autoNumber: true, user: 'tester' });
    expect(store.row('004')?.name).toBe('1402.mov');
  });

  it('setTags 用旧 revision 写入抛 RevisionConflict 并带最新 revision', async () => {
    const { store } = await seededStore();
    const stale = store.revision - 1;
    const error = await capture(store.setTags(['001'], ['站立'], { revision: stale, user: 'tester' }));
    expect(error).toBeInstanceOf(RevisionConflict);
    expect((error as RevisionConflict).status).toBe(409);
    expect((error as RevisionConflict).revision).toBe(store.revision);
    expect((error as RevisionConflict).message).toBe('标签数据已被其他人更新，请刷新后重试');
  });

  it('revision 不是整数时抛 StoreError', async () => {
    const { store } = await seededStore();
    const error = await capture(store.setTags(['001'], ['站立'], { revision: 'abc' as unknown as number }));
    expect(error).toBeInstanceOf(StoreError);
    expect((error as StoreError).message).toBe('revision 必须是整数');
  });

  it('setTags 支持 replace / add / remove 三种模式', async () => {
    const { store } = await seededStore();
    let revision = store.revision;

    await store.setTags(['001'], ['站立'], { mode: 'replace', revision, user: 'tester' });
    revision = store.revision;
    expect(store.row('001')?.tags).toEqual(['站立']);

    await store.setTags(['001'], ['近景'], { mode: 'add', revision, user: 'tester' });
    revision = store.revision;
    expect(store.row('001')?.tags).toEqual(['站立', '近景']);

    const result = await store.setTags(['001'], ['站立'], { mode: 'remove', revision, user: 'tester' });
    expect(result.changed).toBe(1);
    expect(store.row('001')?.tags).toEqual(['近景']);
    expect(store.row('001')?.touched_by).toBe('tester');
  });

  it('未知标签、未知视频、非法打标方式都按 Python 文案报错', async () => {
    const { store } = await seededStore();
    const revision = store.revision;
    const unknownTag = await capture(store.setTags(['001'], ['不存在'], { revision, user: 'tester' }));
    expect((unknownTag as StoreError).message).toBe('标签不存在：不存在');

    const unknownId = await capture(store.setTags(['999'], ['近景'], { revision, user: 'tester' }));
    expect(unknownId).toBeInstanceOf(NotFound);
    expect((unknownId as NotFound).message).toBe('视频不存在：999');

    const badMode = await capture(store.setTags(['001'], ['近景'], { mode: 'merge', revision, user: 'tester' }));
    expect((badMode as StoreError).message).toBe('打标方式只能是 replace / add / remove');

    const noIds = await capture(store.setTags([], ['近景'], { revision, user: 'tester' }));
    expect((noIds as StoreError).message).toBe('请提供要操作的视频编号列表');
  });

  it('弃用是软删除：rows() 仍能看到，rows(false) 排除，恢复后清空弃用信息', async () => {
    const { store } = await seededStore();
    let revision = store.revision;
    await store.deprecate(['001'], true, { revision, reason: '素材过期', user: 'tester' });
    revision = store.revision;

    expect(store.rows()).toHaveLength(2);
    expect(store.rows(false).map((row) => row.id)).toEqual(['002']);
    expect(store.row('001')?.deprecated).toBe(true);
    expect(store.row('001')?.deprecated_by).toBe('tester');
    expect(store.row('001')?.deprecated_reason).toBe('素材过期');
    expect(store.stats().deprecated).toBe(1);
    expect(store.stats().active).toBe(1);

    await store.deprecate(['001'], false, { revision, user: 'tester' });
    expect(store.row('001')?.deprecated).toBe(false);
    expect(store.row('001')?.deprecated_at).toBe(0);
    expect(store.row('001')?.deprecated_by).toBe('');
    expect(store.row('001')?.tags).toEqual(['近景']); // 弃用不动标签，恢复即可用
  });

  it('setRating：0/1/5 都记在行上，越界与非整数拒绝', async () => {
    const { store } = await seededStore();
    let revision = store.revision;

    await store.setRating(['001'], 5, { revision, user: 'tester' });
    revision = store.revision;
    expect(store.row('001')?.rating).toBe(5);
    expect(store.row('001')?.rating_by).toBe('tester');

    await store.setRating(['002'], 1, { revision, user: 'tester' });
    revision = store.revision;
    expect(store.row('002')?.rating).toBe(1);

    const cleared = await store.setRating(['001'], 0, { revision, user: 'tester' });
    expect(cleared.changed).toBe(1);
    expect(store.row('001')?.rating).toBe(0);
    expect(store.row('001')?.rating_at).toBe(0);
    expect(store.row('001')?.rating_by).toBe('');

    const over = await capture(store.setRating(['001'], 6, { revision: store.revision, user: 'tester' }));
    expect((over as StoreError).message).toBe('评分必须是 0-5 的整数（0 为清除）');

    const notInt = await capture(store.setRating(['001'], 'x', { revision: store.revision, user: 'tester' }));
    expect((notInt as StoreError).message).toBe('评分必须是 0-5 的整数');
  });

  it('审计按条追加：记 action / user / revision，且有上限裁剪', async () => {
    const { store } = await seededStore();
    const tail = store.auditTail(10);
    const actions = tail.map((entry) => entry.action);
    expect(actions).toContain('upload_video');
    expect(actions).toContain('library_create');
    const last = tail[tail.length - 1];
    expect(last.user).toBe('tester');
    expect(last.revision).toBe(store.revision);
    expect(store.auditTail(1)).toHaveLength(1);
  });
});

describe('template-store 标签库与大类', () => {
  it('libraryCreate：重复标签、未知分类都拒绝', async () => {
    const { store } = await seededStore();
    const revision = store.revision;
    const duplicate = await capture(store.libraryCreate('近景', '景别', { revision, user: 'tester' }));
    expect((duplicate as StoreError).message).toBe('这个标签已经存在');

    const badCategory = await capture(store.libraryCreate('新标签', '不存在的大类', { revision, user: 'tester' }));
    expect((badCategory as StoreError).message).toBe('分类不存在：不存在的大类（请先在标签库里新增大类）');

    const empty = await capture(store.libraryCreate('   ', '景别', { revision, user: 'tester' }));
    expect((empty as StoreError).message).toBe('标签名称不能为空');
  });

  it('libraryRename 级联改行上的标签；撞名要求改用合并', async () => {
    const { store } = await seededStore();
    let revision = store.revision;
    const result = await store.libraryRename('近景', '特写', { revision, user: 'tester' });
    revision = store.revision;
    expect(result.affected).toBe(2);
    expect(store.library().tags).toContain('特写');
    expect(store.library().tags).not.toContain('近景');
    expect(store.row('001')?.tags).toEqual(['特写']);
    expect(store.row('002')?.tags).toEqual(['特写', '站立']);
    // 分类跟着标签走
    expect(store.categories()['特写']).toBe('景别');

    const conflict = await capture(store.libraryRename('站立', '特写', { revision, user: 'tester' }));
    expect((conflict as StoreError).message).toBe('新名称已存在，请改用合并');
  });

  it('libraryMerge 合并去重、删源标签、返回受影响条数', async () => {
    const { store } = await seededStore();
    const revision = store.revision;
    const result = await store.libraryMerge('站立', '近景', { revision, user: 'tester' });
    expect(result.affected).toBe(1);
    expect(result.becomes_untagged).toBe(0);
    expect(store.library().tags).not.toContain('站立');
    expect(store.row('002')?.tags).toEqual(['近景']);
    expect(store.usage()['近景']).toBe(2);

    const self = await capture(store.libraryMerge('近景', '近景', { revision: store.revision, user: 'tester' }));
    expect((self as StoreError).message).toBe('不能把标签合并到自己');
  });

  it('libraryDelete 删标签并统计会变成未打标的行数', async () => {
    const { store } = await seededStore();
    // 001 只有「近景」一个标签，删掉后变未打标
    const result = await store.libraryDelete('近景', { revision: store.revision, user: 'tester' });
    expect(result.affected).toBe(2);
    expect(result.becomes_untagged).toBe(1);
    expect(store.row('001')?.tags).toEqual([]);
    expect(store.stats().untagged).toBe(1);
  });

  it('大类可增改删，但必须保留一个、且不能删还有标签的', async () => {
    const { store } = await seededStore();
    let revision = store.revision;
    await store.categoryCreate('数据', { revision, user: 'tester' });
    revision = store.revision;
    expect(store.categoryList()).toContain('数据');

    const duplicate = await capture(store.categoryCreate('数据', { revision, user: 'tester' }));
    expect((duplicate as StoreError).message).toBe('这个大类已经存在');

    await store.categoryRename('数据', '素材类型', { revision, user: 'tester' });
    revision = store.revision;
    expect(store.categoryList()).toContain('素材类型');

    const inUse = await capture(store.categoryDelete('景别', { revision, user: 'tester' }));
    expect((inUse as StoreError).message).toContain('请先改分类或删除标签');

    await store.categoryDelete('素材类型', { revision, user: 'tester' });
    expect(store.categoryList()).not.toContain('素材类型');

    const tooLong = await capture(store.categoryCreate('一二三四五六七八九十一二三四五六七八九十一', { user: 'tester' }));
    expect((tooLong as StoreError).message).toBe('大类名称最多 20 个字');
  });
});

describe('template-store 影响预览', () => {
  it('delete 统计受影响的视频、会变未打标的行数，并给最多 5 条样例', async () => {
    const { store } = await seededStore();
    const preview = store.impactPreview('delete', { name: '近景' });
    expect(preview.action).toBe('delete');
    expect(preview.affected).toBe(2);
    expect(preview.becomes_untagged).toBe(1);
    expect((preview.samples as unknown[]).length).toBe(2);
    expect(preview.revision).toBe(store.revision);
    // 纯推演：不动 revision、不写盘
    expect(store.revision).toBe(preview.revision);
  });

  it('deprecate 只算未弃用的行；set_tags 按 ids 条数；未知动作报错', async () => {
    const { store } = await seededStore();
    expect(store.impactPreview('deprecate', { ids: ['001', '999'] }).affected).toBe(1);
    expect(store.impactPreview('set_tags', { ids: ['001', '002'] }).affected).toBe(2);
    expect(() => store.impactPreview('unknown', {})).toThrow('不支持的影响预览动作');
    expect(() => store.impactPreview('delete', { name: '不存在' })).toThrow('标签不存在：不存在');
  });
});

describe('template-store 版本快照', () => {
  it('手动版本进列表；回滚整库恢复视频与标签，并留回滚前备份', async () => {
    const { store } = await seededStore();
    const before = store.rows().map((row) => ({ id: row.id, tags: [...row.tags] }));
    const meta = await store.createVersion('打标签前基线', { user: 'tester' });
    expect(meta.kind).toBe('manual');
    expect(meta.count).toBe(2);
    expect(meta.note).toBe('打标签前基线');

    // 改乱：删标签 + 加一条视频
    await store.libraryDelete('站立', { revision: store.revision, user: 'tester' });
    await store.addVideo({ name: '1500.mp4', tags: ['近景'], user: 'tester' });
    expect(store.stats().total).toBe(3);

    const restored = await store.restoreVersion(meta.name, { revision: store.revision, user: 'tester' });
    expect(restored.restored).toBe(2);
    expect(restored.version).toBe(meta.name);
    expect(restored.backup).toBeTruthy();
    // 当前有、目标版本没有 → added_since=1（那条 1500.mp4 回滚后消失）；反向为 0
    expect(restored.added_since).toBe(1);
    expect(restored.removed_since).toBe(0);
    expect(store.stats().total).toBe(2);
    expect(store.library().tags).toContain('站立');
    expect(store.rows().map((row) => ({ id: row.id, tags: [...row.tags] }))).toEqual(before);

    // 回滚前备份也是一份 manual 版本，能再撤回
    const versions = store.listVersions(100);
    const backup = versions.find((item) => item.action === 'before_restore');
    expect(backup?.note).toBe(`回滚前备份（${meta.name}）`);
  });

  it('回滚不带 revision 直接拒绝；版本名非法或不存在分别报错', async () => {
    const { store } = await seededStore();
    const meta = await store.createVersion('基线', { user: 'tester' });

    const noRevision = await capture(store.restoreVersion(meta.name, { user: 'tester' }));
    expect((noRevision as StoreError).message).toBe('回滚整库必须带 revision：请先刷新拿到最新版本号再回滚');

    const badName = await capture(store.restoreVersion('../../etc/passwd', { revision: 1, user: 'tester' }));
    expect((badName as StoreError).message).toBe('版本文件名无效');

    const missing = await capture(
      store.restoreVersion('v000099-1700000000-manual-abcd.json.gz', { revision: 1, user: 'tester' }),
    );
    expect(missing).toBeInstanceOf(NotFound);
    expect((missing as NotFound).message).toBe('找不到该版本');
  });

  it('版本快照是 gzip，文件名符合 Python 的命名规则', async () => {
    const { store } = await seededStore();
    const meta = await store.createVersion('检查格式', { user: 'tester' });
    expect(meta.name).toMatch(/^v\d{6}-\d+-(auto|manual)-[0-9a-f]{4}\.json\.gz$/);
    const file = path.join(store.versionsDir(), meta.name);
    const raw = fs.readFileSync(file);
    expect(raw[0]).toBe(0x1f); // gzip magic
    expect(raw[1]).toBe(0x8b);
    const payload = store.readVersion(meta.name);
    expect(payload.schema).toBe(1);
    expect(payload.kind).toBe('manual');
    expect(Array.isArray(payload.videos)).toBe(true);
    expect(fs.existsSync(`${file}.meta.json`)).toBe(true);
  });
});

describe('template-store 文件锁与原子写', () => {
  it('写入成功后锁目录被清掉，且留下 .bak 备份', async () => {
    const { store, file } = await seededStore();
    await store.setTags(['001'], ['站立'], { revision: store.revision, user: 'tester' });
    expect(fs.existsSync(`${file}.lock`)).toBe(false);
    expect(fs.existsSync(`${file}.tmp`)).toBe(false);
    expect(fs.existsSync(`${file}.bak`)).toBe(true);
    const backup = JSON.parse(fs.readFileSync(`${file}.bak`, 'utf8')) as { revision: number };
    expect(backup.revision).toBe(store.revision - 1);
  });

  it('锁目录被占时超时报 StoreUnavailable（5 秒）', async () => {
    const { store, file } = await seededStore();
    fs.mkdirSync(`${file}.lock`);
    const started = Date.now();
    const error = await capture(store.setTags(['001'], ['站立'], { revision: store.revision, user: 'tester' }));
    const elapsed = Date.now() - started;
    expect(error).toBeInstanceOf(StoreUnavailable);
    expect((error as StoreUnavailable).message).toBe('标签真源正在被其他程序写入，请稍后重试');
    expect(elapsed).toBeGreaterThanOrEqual(4900);
    fs.rmdirSync(`${file}.lock`);
  }, 15000);
});
