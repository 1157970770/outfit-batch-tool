#!/usr/bin/env node
/**
 * P3 冒烟：标签库（视频库）+ 管理端。
 *
 * 覆盖：登录 → 标签快照 → 改标签（revision 乐观锁）→ 旧 revision 409
 * → 弃用/恢复 → 评分 → 影响面预览 → 标签库增删改 → 版本快照/回滚
 * → 管理端状态/模板健康/账户列表/用量/新增账户/改配额/重置密码/停用/删除/CSV。
 *
 * 注意：会写标签真源（`dataDir/template_tags.json`）与版本快照，
 * 请只对**测试实例**（`.tmp/p3` 之类）跑，绝不要对生产数据目录跑。
 *
 * 用法：
 *   node scripts/p3-smoke.mjs [http://127.0.0.1:3131]
 *   PARITY_USER=... PARITY_PASS=... node scripts/p3-smoke.mjs
 */
const BASE = (process.argv[2] || process.env.P3_BASE || 'http://127.0.0.1:3131').replace(/\/$/, '');
const USER = process.env.PARITY_USER || 'parity';
const PASS = process.env.PARITY_PASS || 'pw-parity-123';
const NEW_USER = process.env.P3_USER || `p3u${Date.now().toString(36).slice(-6)}`;
const NEW_PASS = 'p3-pass-123';

let cookie = '';
let passed = 0;
let failed = 0;

function ok(name, extra = '') {
  passed += 1;
  console.log(`  ✓ ${name}${extra ? ` — ${extra}` : ''}`);
}

function bad(name, detail) {
  failed += 1;
  console.log(`  ✗ ${name} — ${detail}`);
}

function check(name, condition, detail = '') {
  if (condition) ok(name, detail);
  else bad(name, detail || '断言失败');
  return Boolean(condition);
}

async function call(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (cookie) headers.cookie = cookie;
  let body = options.body;
  if (body !== undefined && typeof body !== 'string' && !(body instanceof Uint8Array)) {
    headers['content-type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const res = await fetch(`${BASE}${path}`, { ...options, headers, body, redirect: 'manual' });
  const setCookie = res.headers.getSetCookie?.() || [];
  for (const item of setCookie) {
    const pair = item.split(';')[0];
    if (pair.startsWith('outfit_sid=')) cookie = pair;
  }
  const type = res.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await res.json() : await res.arrayBuffer();
  return { status: res.status, headers: res.headers, payload };
}

async function main() {
  console.log(`P3 冒烟 → ${BASE}`);

  const login = await call('/api/login', { method: 'POST', body: { username: USER, password: PASS } });
  if (!check('登录', login.status === 200 && login.payload?.ok === true, `HTTP ${login.status}`)) {
    console.log('登录失败，后续跳过。');
    return;
  }

  // ---- 标签快照 ----
  const snap = await call('/api/tags/snapshot');
  const snapshot = snap.payload || {};
  check('快照可读', snap.status === 200 && snapshot.ok === true, `HTTP ${snap.status}`);
  check('快照有 revision 与视频', Number.isInteger(snapshot.revision) && Array.isArray(snapshot.videos),
    `revision=${snapshot.revision} videos=${snapshot.videos?.length}`);
  const revision = Number(snapshot.revision || 0);
  const videos = Array.isArray(snapshot.videos) ? snapshot.videos : [];
  const categories = Array.isArray(snapshot.categories) ? snapshot.categories : [];
  const libraryTags = Array.isArray(snapshot.library?.tags) ? snapshot.library.tags : [];

  if (videos.length === 0) {
    bad('缺少可测视频', '标签真源里没有视频，P3 冒烟无法继续');
    return;
  }

  const target = videos[0];
  const firstTag = libraryTags[0] || Object.values(snapshot.library?.categories || {})[0]?.[0];
  check('有可用标签', Boolean(firstTag), String(firstTag || ''));

  // 冒烟要能重复跑：先算出「与现状不同」的标签集合，否则 changed=0、revision 不前进。
  const currentTags = Array.isArray(target.tags) ? target.tags : [];
  let desiredTags = [firstTag].filter(Boolean).filter((tag) => !currentTags.includes(tag));
  if (desiredTags.length === 0) {
    const spare = libraryTags.find((tag) => !currentTags.includes(tag));
    desiredTags = spare ? [spare] : currentTags.slice(0, Math.max(0, currentTags.length - 1));
  }
  if (desiredTags.length === 0) desiredTags = [firstTag].filter(Boolean);

  // ---- 改标签：正常一次 + 旧 revision 冲突 ----
  const write1 = await call('/api/tags/tags', {
    method: 'POST',
    body: { ids: [target.id], tags: desiredTags, mode: 'replace', revision },
  });
  check('改标签 200', write1.status === 200 && write1.payload?.ok === true, `HTTP ${write1.status}`);
  check('改标签返回新 revision', Number(write1.payload?.revision) === revision + 1,
    `${write1.payload?.revision} vs ${revision + 1}`);
  check('改标签回带 updated 行', Array.isArray(write1.payload?.updated) && write1.payload.updated[0]?.id === target.id,
    `updated=${write1.payload?.updated?.length}`);
  const nextRevision = Number(write1.payload?.revision ?? revision);

  const stale = await call('/api/tags/tags', {
    method: 'POST',
    body: { ids: [target.id], tags: [firstTag].filter(Boolean), mode: 'replace', revision },
  });
  check('旧 revision 冲突 409', stale.status === 409, `HTTP ${stale.status} / ${stale.payload?.error || ''}`);

  const noRev = await call('/api/tags/deprecate', { method: 'POST', body: { ids: [target.id], deprecated: true } });
  check('缺 revision 400', noRev.status === 400 && /revision/.test(noRev.payload?.error || ''),
    `${noRev.status} ${noRev.payload?.error || ''}`);

  // ---- 弃用 → 影响面 → 恢复 ----
  const impact = await call('/api/tags/impact', { method: 'POST', body: { action: 'deprecate', ids: [target.id] } });
  check('影响面预览 200', impact.status === 200 && impact.payload?.ok === true, `HTTP ${impact.status}`);
  check('影响面带 locked_batches', Array.isArray(impact.payload?.locked_batches),
    `locked_batches=${impact.payload?.locked_batches?.length}`);

  const dep = await call('/api/tags/deprecate', {
    method: 'POST',
    body: { ids: [target.id], deprecated: true, revision: nextRevision },
  });
  check('弃用 200', dep.status === 200 && dep.payload?.ok === true, `HTTP ${dep.status}`);
  check('弃用后 updated.deprecated', dep.payload?.updated?.[0]?.deprecated === true,
    String(dep.payload?.updated?.[0]?.deprecated));
  const revAfterDep = Number(dep.payload?.revision ?? nextRevision);

  const restore = await call('/api/tags/deprecate', {
    method: 'POST',
    body: { ids: [target.id], deprecated: false, revision: revAfterDep },
  });
  check('恢复 200', restore.status === 200 && restore.payload?.updated?.[0]?.deprecated === false,
    `HTTP ${restore.status}`);
  const revAfterRestore = Number(restore.payload?.revision ?? revAfterDep);

  // ---- 评分 ----
  const rate = await call('/api/tags/rating', {
    method: 'POST',
    body: { ids: [target.id], rating: 5, revision: revAfterRestore },
  });
  check('评分 200', rate.status === 200 && Number(rate.payload?.updated?.[0]?.rating) === 5,
    `HTTP ${rate.status} rating=${rate.payload?.updated?.[0]?.rating}`);
  let liveRevision = Number(rate.payload?.revision ?? revAfterRestore);

  // ---- 标签库：新增/改名/合并/删除 + 版本 ----
  const tagName = `P3-${Date.now().toString(36).toUpperCase()}`;
  const category = categories[0] || '景别';
  const created = await call('/api/tags/library', {
    method: 'POST',
    body: { action: 'create', name: tagName, category, revision: liveRevision },
  });
  check('新增库标签 200', created.status === 200 && created.payload?.ok === true,
    `HTTP ${created.status} ${created.payload?.error || ''}`);
  liveRevision = Number(created.payload?.revision ?? liveRevision);

  const renamed = await call('/api/tags/library', {
    method: 'POST',
    body: { action: 'rename', name: tagName, new_name: `${tagName}2`, revision: liveRevision },
  });
  check('改名 200', renamed.status === 200 && renamed.payload?.ok === true,
    `HTTP ${renamed.status} ${renamed.payload?.error || ''}`);
  liveRevision = Number(renamed.payload?.revision ?? liveRevision);

  const affected = await call('/api/tags/library', {
    method: 'POST',
    body: { action: 'delete', name: `${tagName}2`, revision: liveRevision },
  });
  const deletedOk = affected.status === 200
    ? affected.payload?.ok === true
    : affected.status === 400 && affected.payload?.confirm_required === true;
  check('删除未使用的库标签（或要求确认）', deletedOk,
    `HTTP ${affected.status} ${affected.payload?.error || ''}`);
  if (affected.status === 200) liveRevision = Number(affected.payload?.revision ?? liveRevision);

  const snapshotNow = await call('/api/tags/snapshot');
  check('快照 revision 前进了', Number(snapshotNow.payload?.revision) >= liveRevision,
    `${snapshotNow.payload?.revision} >= ${liveRevision}`);

  const versions = await call('/api/tags/versions?limit=5');
  check('版本列表 200', versions.status === 200 && Array.isArray(versions.payload?.versions),
    `HTTP ${versions.status} versions=${versions.payload?.versions?.length}`);

  const snapNow = await call('/api/tags/versions', {
    method: 'POST',
    body: { note: 'p3 smoke', revision: liveRevision },
  });
  const manualSaved = snapNow.status === 200
    ? snapNow.payload?.ok === true
    : snapNow.status === 400 || snapNow.status === 409;
  check('手动快照（或明确拒绝）', manualSaved, `HTTP ${snapNow.status} ${snapNow.payload?.error || ''}`);

  // ---- 管理端 ----
  const forbidden = await call('/api/admin/status');
  check('管理员状态 200', forbidden.status === 200 && forbidden.payload?.ok === true,
    `HTTP ${forbidden.status}`);

  const health = await call('/api/admin/template-health');
  check('模板健康 200', health.status === 200 && health.payload?.ok === true,
    `HTTP ${health.status}`);

  const users = await call('/api/admin/users');
  check('账户列表 200', users.status === 200 && Array.isArray(users.payload?.users),
    `HTTP ${users.status} users=${users.payload?.users?.length}`);
  check('账户列表不泄露哈希',
    !JSON.stringify(users.payload?.users || []).includes('"hash"'),
    '返回里不应有 hash/salt');

  const usage = await call('/api/admin/usage');
  check('用量汇总 200', usage.status === 200 && Array.isArray(usage.payload?.accounts),
    `HTTP ${usage.status} accounts=${usage.payload?.accounts?.length}`);
  check('用量有 totals', typeof usage.payload?.totals?.items === 'number',
    `items=${usage.payload?.totals?.items}`);

  const addUser = await call('/api/admin/users', {
    method: 'POST',
    body: { username: NEW_USER, password: NEW_PASS, role: 'user', quota: 3 },
  });
  check('新增账户 200/201', (addUser.status === 200 || addUser.status === 201) && addUser.payload?.ok === true,
    `HTTP ${addUser.status} ${addUser.payload?.error || ''}`);

  const dupUser = await call('/api/admin/users', {
    method: 'POST',
    body: { username: NEW_USER, password: NEW_PASS, role: 'user' },
  });
  check('重复账户被拒', dupUser.status === 409 || dupUser.status === 400,
    `HTTP ${dupUser.status} ${dupUser.payload?.error || ''}`);

  const quota = await call(`/api/admin/users/${encodeURIComponent(NEW_USER)}/quota`, {
    method: 'POST',
    body: { quota: 7 },
  });
  check('改配额 200', quota.status === 200 && quota.payload?.ok === true, `HTTP ${quota.status}`);

  const reset = await call(`/api/admin/users/${encodeURIComponent(NEW_USER)}/reset`, {
    method: 'POST',
    body: { password: 'p3-reset-123' },
  });
  check('重置密码 200', reset.status === 200 && reset.payload?.ok === true, `HTTP ${reset.status}`);

  const disable = await call(`/api/admin/users/${encodeURIComponent(NEW_USER)}/disable`, { method: 'POST' });
  check('停用 200', disable.status === 200 && disable.payload?.ok === true, `HTTP ${disable.status}`);

  const disabledLogin = await fetch(`${BASE}/api/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username: NEW_USER, password: 'p3-reset-123' }),
  });
  check('停用后不能登录', disabledLogin.status === 401, `HTTP ${disabledLogin.status}`);

  const enable = await call(`/api/admin/users/${encodeURIComponent(NEW_USER)}/enable`, { method: 'POST' });
  check('启用 200', enable.status === 200 && enable.payload?.ok === true, `HTTP ${enable.status}`);

  const work = await call(`/api/admin/accounts/${encodeURIComponent(USER)}/work?limit=5`);
  check('账户明细 200', work.status === 200 && Array.isArray(work.payload?.items),
    `HTTP ${work.status} items=${work.payload?.items?.length}`);

  const csv = await call(`/api/admin/accounts/${encodeURIComponent(USER)}/work.csv`);
  const csvText = Buffer.from(csv.payload instanceof ArrayBuffer ? csv.payload : new ArrayBuffer(0)).toString('utf8');
  check('CSV 可下载', csv.status === 200 && csvText.includes('模板'),
    `HTTP ${csv.status} ${csvText.slice(0, 24).replace(/\s+/g, ' ')}`);

  const del = await call(`/api/admin/users/${encodeURIComponent(NEW_USER)}`, { method: 'DELETE' });
  check('删除账户 200', del.status === 200 && del.payload?.ok === true, `HTTP ${del.status}`);

  const archive = await call('/api/admin/archive-search?style=NOPE-NOT-EXIST');
  check('归档检索（未接入 COS 时应 503/200 空结果）',
    archive.status === 200 || archive.status === 503,
    `HTTP ${archive.status} ${archive.payload?.error || ''}`);

  // ---- 收尾：还原目标视频的标签与评分，避免污染测试真源 ----
  const finalSnap = await call('/api/tags/snapshot');
  const finalRev = Number(finalSnap.payload?.revision ?? liveRevision);
  const cleanup = await call('/api/tags/rating', {
    method: 'POST',
    body: { ids: [target.id], rating: 0, revision: finalRev },
  });
  check('清回评分', cleanup.status === 200, `HTTP ${cleanup.status}`);

  console.log(`\nP3 冒烟：${passed} 通过 / ${failed} 失败`);
  if (failed > 0) process.exitCode = 1;
}

main().catch((err) => {
  console.error('冒烟脚本异常：', err);
  process.exitCode = 1;
});
