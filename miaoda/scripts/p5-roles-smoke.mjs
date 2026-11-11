/**
 * P5 权限冒烟：管理员 vs 普通账号（对应 Python 版「按 owner 隔离」）。
 *
 * 覆盖：普通账号只能看到自己的任务/批次；`?scope=all` 仅管理员；别人的任务 403；
 * 管理员能看所有人的；管理端接口对普通账号 403。
 *
 * 用法：node scripts/p5-roles-smoke.mjs [base]
 * 前置：隔离实例 + mock 百炼（见 scripts/p1-smoke.mjs 顶部说明）。
 * 管理员账号默认 parity / pw-parity-123，可用 PARITY_USER / PARITY_PASS 覆盖。
 */
import { randomUUID } from 'node:crypto';
import zlib from 'node:zlib';

const base = (process.argv[2] || 'http://127.0.0.1:3131').replace(/\/$/, '');
const ADMIN = { username: process.env.PARITY_USER || 'parity', password: process.env.PARITY_PASS || 'pw-parity-123' };
const USER_A = { username: 'p5a', password: 'P5-pass-1234' };
const USER_B = { username: 'p5b', password: 'P5-pass-1234' };

let passed = 0;
const failures = [];

function check(name, ok, detail = '') {
  if (ok) {
    passed += 1;
    console.log(`  ✓ ${name}`);
  } else {
    failures.push(`${name}${detail ? ` — ${detail}` : ''}`);
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

function cookieOf(res) {
  const raw = res.headers.getSetCookie?.() || [];
  const first = raw.find((c) => c.startsWith('outfit_sid='));
  return first ? first.split(';')[0] : '';
}

async function call(path, { method = 'GET', body, cookie, raw } = {}) {
  const headers = {};
  if (cookie) headers.Cookie = cookie;
  let payload;
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  } else if (raw) {
    payload = raw;
  }
  const res = await fetch(`${base}${path}`, { method, headers, body: payload });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    json = null;
  }
  return { status: res.status, headers: res.headers, json, text };
}

async function login(account) {
  const res = await call('/api/login', { method: 'POST', body: account });
  if (res.status !== 200) throw new Error(`登录 ${account.username} 失败：${res.status} ${res.text}`);
  return cookieOf(res);
}

/** 800×1000 PNG：低于 240px 会被服务端拒掉。 */
function makePng(width = 800, height = 1000) {
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(zlib.crc32 ? zlib.crc32(body) : crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 2;
  const raw = Buffer.alloc((width * 3 + 1) * height, 128);
  for (let y = 0; y < height; y += 1) raw[y * (width * 3 + 1)] = 0;
  const idat = zlib.deflateSync(raw);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', idat),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

let crcTable;
function crc32(buf) {
  if (!crcTable) {
    crcTable = new Int32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c;
    }
  }
  let crc = -1;
  for (const byte of buf) crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ -1) >>> 0;
}

async function ensureUser(adminCookie, account) {
  const created = await call('/api/admin/users', {
    method: 'POST',
    cookie: adminCookie,
    body: { username: account.username, password: account.password, role: 'user' },
  });
  if (created.status === 201) return 'created';
  if (created.status === 409) {
    const reset = await call(`/api/admin/users/${encodeURIComponent(account.username)}/reset`, {
      method: 'POST',
      cookie: adminCookie,
      body: { password: account.password },
    });
    return `exists(${reset.status})`;
  }
  return `unexpected(${created.status}: ${created.text})`;
}

async function main() {
  console.log(`目标 ${base}\n`);

  const adminCookie = await login(ADMIN);
  check('管理员登录', Boolean(adminCookie));

  for (const account of [USER_A, USER_B]) {
    const state = await ensureUser(adminCookie, account);
    check(`准备账号 ${account.username}`, state.startsWith('created') || state.startsWith('exists'), state);
  }

  const aCookie = await login(USER_A);
  const bCookie = await login(USER_B);
  check('两个普通账号都能登录', Boolean(aCookie) && Boolean(bCookie));

  // 普通账号不许进管理端
  const adminDenied = await call('/api/admin/status', { cookie: aCookie });
  check('普通账号调管理端 → 403', adminDenied.status === 403, `${adminDenied.status} ${adminDenied.text.slice(0, 60)}`);

  const scopeDenied = await call('/api/jobs?scope=all', { cookie: aCookie });
  check('普通账号查 ?scope=all → 403', scopeDenied.status === 403, `${scopeDenied.status}`);

  // 管理员看得到全量，普通账号只看自己
  const adminJobs = await call('/api/jobs?scope=all', { cookie: adminCookie });
  check('管理员 ?scope=all 200', adminJobs.status === 200 && Array.isArray(adminJobs.json?.jobs));

  // A 传图 + 建任务
  const png = makePng();
  const uploadRes = await fetch(`${base}/api/uploads`, {
    method: 'POST',
    headers: { Cookie: aCookie, 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: 'p5a.png', data: png.toString('base64') }),
  });
  const upload = await uploadRes.json();
  check('A 上传图片 201', uploadRes.status === 201 && Boolean(upload.upload_id), `${uploadRes.status} ${JSON.stringify(upload).slice(0, 80)}`);

  const templates = await call('/api/templates', { cookie: aCookie });
  const ready = (templates.json?.items || []).filter((item) => item.ready).slice(0, 1);
  check('A 能读模板候选集', ready.length === 1, `ready=${(templates.json?.items || []).filter((i) => i.ready).length}`);

  const jobRes = await call('/api/jobs', {
    method: 'POST',
    cookie: aCookie,
    body: {
      upload_ids: [upload.upload_id],
      selection: 'selected',
      template_id: ready[0]?.id,
      tags: { top: 'P5A-001' },
      resolution: '480P',
      duration: 5,
    },
  });
  const job = jobRes.json?.job;
  check('A 建任务 201', jobRes.status === 201 && Boolean(job?.id), `${jobRes.status} ${jobRes.text.slice(0, 100)}`);

  const listA = await call('/api/jobs', { cookie: aCookie });
  check('A 的列表里有自己的任务', (listA.json?.jobs || []).some((j) => j.id === job?.id));

  const listB = await call('/api/jobs', { cookie: bCookie });
  check('B 的列表里没有 A 的任务', !(listB.json?.jobs || []).some((j) => j.id === job?.id));

  const listAll = await call('/api/jobs?scope=all', { cookie: adminCookie });
  check('管理员全量里有 A 的任务', (listAll.json?.jobs || []).some((j) => j.id === job?.id));

  const readB = await call(`/api/jobs/${job?.id}`, { cookie: bCookie });
  check('B 读 A 的任务 → 403', readB.status === 403, `${readB.status} ${readB.text.slice(0, 60)}`);

  const readAdmin = await call(`/api/jobs/${job?.id}`, { cookie: adminCookie });
  check('管理员读 A 的任务 200', readAdmin.status === 200 && readAdmin.json?.job?.id === job?.id);

  const cancelB = await call(`/api/jobs/${job?.id}/cancel`, { method: 'POST', cookie: bCookie });
  check('B 取消 A 的任务 → 403', cancelB.status === 403, `${cancelB.status}`);

  const usage = await call('/api/admin/usage', { cookie: adminCookie });
  check('管理员用量接口 200', usage.status === 200 && Array.isArray(usage.json?.accounts));

  // 批次隔离（批次必须带有效 prompt_id，与 Python 同口径）
  const boot = await call('/api/bootstrap', { cookie: aCookie });
  const promptId = boot.json?.prompts?.[0]?.id;
  profileCheck: {
    check('A 能读到提示词库', Boolean(promptId), `bootstrap status ${boot.status}`);
  }
  const batchRes = await call('/api/batches', {
    method: 'POST',
    cookie: aCookie,
    body: {
      batch_no: `P5-${randomUUID().slice(0, 6)}`,
      tags: { top: 'P5A-001' },
      prompt_id: promptId,
      resolution: '480P',
      duration: 5,
    },
  });
  const batch = batchRes.json?.batch;
  check('A 建批次 201', batchRes.status === 201 && Boolean(batch?.id), `${batchRes.status} ${batchRes.text.slice(0, 100)}`);

  const batchB = await call(`/api/batches/${batch?.id}`, { cookie: bCookie });
  check('B 读 A 的批次 → 403/404', batchB.status === 403 || batchB.status === 404, `${batchB.status}`);

  const batchAll = await call('/api/batches?scope=all', { cookie: adminCookie });
  check('管理员全量批次里有它', (batchAll.json?.batches || []).some((b) => b.id === batch?.id));

  const batchScopeDenied = await call('/api/batches?scope=all', { cookie: aCookie });
  check('普通账号查批次 ?scope=all → 403', batchScopeDenied.status === 403, `${batchScopeDenied.status}`);

  // 收尾：删批次 + 取消任务（避免留下跑完的假任务占额度）
  await call(`/api/jobs/${job?.id}/cancel`, { method: 'POST', cookie: aCookie });
  const del = await call(`/api/batches/${batch?.id}`, { method: 'DELETE', cookie: aCookie });
  check('A 删除自己的批次 200', del.status === 200, `${del.status}`);

  console.log(`\n结果：${passed} 通过 / ${failures.length} 失败`);
  for (const failure of failures) console.log(`  - ${failure}`);
  process.exit(failures.length === 0 ? 0 : 1);
}

await main();
