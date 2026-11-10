#!/usr/bin/env node
/**
 * P2 冒烟：批次全流程 + 历史分组 + 整批 ZIP。
 *
 * 覆盖：登录 → 上传 → 建批次（草稿）→ 绑图 → 候选清单 → 锁定 → 生产 → 轮询到终态
 * → 条目对账 → ZIP probe/下载 → 历史分组与明细 → 关闭/删除。
 *
 * 用法（假设服务已在 BASE 上跑，且 mock 百炼可用）：
 *   node scripts/p2-smoke.mjs [http://127.0.0.1:3122]
 *   PARITY_USER=... PARITY_PASS=... node scripts/p2-smoke.mjs
 */
import zlib from 'node:zlib';

const BASE = (process.argv[2] || process.env.P2_BASE || 'http://127.0.0.1:3122').replace(/\/$/, '');
const USER = process.env.PARITY_USER || 'parity';
const PASS = process.env.PARITY_PASS || 'pw-parity-123';
const BATCH_NO = process.env.P2_BATCH_NO || `P2-${Date.now().toString(36).toUpperCase()}`;

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
  return condition;
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

/** 造一张 w×h 的纯色 PNG（服务端有 240px 下限，别用 200×200）。 */
function makePng(width, height, [r, g, b]) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const rowStart = y * (width * 3 + 1);
    raw[rowStart] = 0;
    for (let x = 0; x < width; x += 1) {
      const p = rowStart + 1 + x * 3;
      raw[p] = r;
      raw[p + 1] = g;
      raw[p + 2] = b;
    }
  }
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(body) >>> 0);
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 2;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitJob(jobId, timeoutMs = 90_000) {
  const started = Date.now();
  let last = null;
  while (Date.now() - started < timeoutMs) {
    const res = await call(`/api/jobs/${jobId}`);
    if (res.status !== 200) throw new Error(`取任务失败 HTTP ${res.status}`);
    last = res.payload.job;
    if (!['queued', 'running'].includes(last.status)) return last;
    await sleep(800);
  }
  return last;
}

async function main() {
  console.log(`\nP2 冒烟 → ${BASE}（批次号 ${BATCH_NO}）\n`);

  // 1. 登录
  const login = await call('/api/login', {
    method: 'POST',
    body: { username: USER, password: PASS },
  });
  check('登录 200', login.status === 200, `HTTP ${login.status}`);
  check('拿到会话 Cookie', Boolean(cookie));

  // 2. 上传两张图（1 定妆 + 1 细节）
  const uploads = [];
  for (const [index, color] of [
    [0, [200, 60, 60]],
    [1, [60, 120, 200]],
  ]) {
    const png = makePng(800, 1000, color);
    const res = await call('/api/uploads', {
      method: 'POST',
      body: { filename: `p2-${index}-${Date.now()}.png`, data: `data:image/png;base64,${png.toString('base64')}` },
    });
    if (res.status !== 201 && res.status !== 200) {
      bad('上传图片', `HTTP ${res.status} ${JSON.stringify(res.payload)}`);
      continue;
    }
    uploads.push(res.payload.upload_id);
  }
  check('上传两张图', uploads.length === 2, `ids=${uploads.join(',')}`);
  if (uploads.length < 2) return finish();

  // 3. 建批次（草稿允许 0 图）
  const created = await call('/api/batches', {
    method: 'POST',
    body: {
      batch_no: BATCH_NO,
      title: 'P2 冒烟批次',
      prompt_id: 'p02',
      prompt: '把图1的人物换上图2的衣服，保持动作',
      resolution: '480P',
      duration: 7,
      tags: { top: 'A001', pants: 'P002' },
    },
  });
  check('建批次 201', created.status === 201, `HTTP ${created.status} ${JSON.stringify(created.payload).slice(0, 160)}`);
  const batch = created.payload?.batch;
  if (!batch) return finish();
  check('批次初始为草稿', batch.state === 'draft', batch.state);

  // 4. 绑图（定妆 + 细节）
  const bound = await call(`/api/batches/${batch.id}/images`, {
    method: 'POST',
    body: { op: 'add', upload_ids: uploads },
  });
  check(
    '绑定两张图',
    bound.status === 200 && (bound.payload?.batch?.image_urls?.length || 0) === 2,
    `image_urls=${bound.payload?.batch?.image_urls?.length}`,
  );

  // 5. 候选清单
  const cand = await call(`/api/batches/${batch.id}/candidates`, {
    method: 'POST',
    body: { limit: 60, hide_used: true },
  });
  const candItems = cand.payload?.items || [];
  check('候选清单有结果', cand.status === 200 && candItems.length > 0, `${candItems.length} 条`);
  const ready = candItems.filter((item) => item.ready).map((item) => item.id).slice(0, 5);
  check('候选里至少有 5 条就绪', ready.length === 5, ready.join(','));

  // 6. 锁定
  const locked = await call(`/api/batches/${batch.id}/lock`, {
    method: 'POST',
    body: { template_ids: ready, confirmed: true },
  });
  check('锁定 200', locked.status === 200, `HTTP ${locked.status} ${JSON.stringify(locked.payload).slice(0, 160)}`);
  check('锁定后 state=locked', locked.payload?.batch?.state === 'locked', locked.payload?.batch?.state);
  check('锁定 5 条', (locked.payload?.batch?.total_items || 0) === 5, String(locked.payload?.batch?.total_items));

  // 7. 未确认时不能锁第二遍
  const relock = await call(`/api/batches/${batch.id}/lock`, {
    method: 'POST',
    body: { template_ids: ready, confirmed: true },
  });
  check('重复锁定 409', relock.status === 409, `HTTP ${relock.status}`);

  // 8. 生产（锁了 5 条，正好满足最小 5）
  const produced = await call(`/api/batches/${batch.id}/produce`, {
    method: 'POST',
    body: { count: 5 },
  });
  check('生产 201', produced.status === 201, `HTTP ${produced.status} ${JSON.stringify(produced.payload).slice(0, 200)}`);
  const job = produced.payload?.job;
  if (!job) return finish();

  // 9. 等终态
  const done = await waitJob(job.id);
  check('任务到终态', done && !['queued', 'running'].includes(done.status), done?.status);
  check('任务成功', done?.status === 'succeeded', `${done?.status} ${done?.message || ''}`);
  check('完成 5/5', done?.done === 5 && done?.total === 5, `${done?.done}/${done?.total}`);

  // 10. 批次对账
  const detail = await call(`/api/batches/${batch.id}`);
  const items = detail.payload?.items || [];
  check('批次条目全部 done', items.length === 5 && items.every((item) => item.state === 'done'), items.map((i) => i.state).join(','));
  check('批次 active_job_id 清空', !detail.payload?.batch?.active_job_id, String(detail.payload?.batch?.active_job_id));
  const withUrl = items.filter((item) => item.download_url).length;
  check('条目带下载地址', withUrl === 5, `${withUrl}/5`);

  // 11. 整批 ZIP
  const probe = await call(`/api/batches/${batch.id}/files.zip?probe=1`);
  check('ZIP probe 200', probe.status === 200, `HTTP ${probe.status}`);
  check('ZIP 计数为 5', probe.payload?.count === 5, String(probe.payload?.count));
  const zip = await call(`/api/batches/${batch.id}/files.zip`);
  check('ZIP 下载 200', zip.status === 200, `HTTP ${zip.status}`);
  const zipBytes = zip.payload instanceof ArrayBuffer ? zip.payload.byteLength : 0;
  check('ZIP 有内容', zipBytes > 1000, `${zipBytes} 字节`);
  if (zipBytes > 4) {
    const head = Buffer.from(zip.payload.slice(0, 4)).toString('latin1');
    check('ZIP 魔数 PK', head.startsWith('PK'), head);
  }

  // 12. 历史分组
  const groups = await call('/api/history/groups?scope=mine&group_by=batch');
  const cards = groups.payload?.groups || [];
  const card = cards.find((item) => item.batch_no === BATCH_NO);
  check('历史里能查到该批次', Boolean(card), `groups=${cards.length}`);
  if (card) {
    check('分组计数 5 成功', card.success === 5 && card.total === 5, `${card.success}/${card.total}`);
    const rows = await call(
      `/api/history/groups/${encodeURIComponent(card.key)}?offset=0&limit=100&scope=mine`,
    );
    const list = rows.payload?.items || [];
    check('明细 5 行', list.length === 5, `${list.length} 行`);
    check('明细都成功且有下载地址', list.every((row) => row.status === 'success' && row.download_url), '');
    check('明细带轮次信息', list.every((row) => row.run_no >= 1), '');
  }

  // 13. 关闭批次后不能再改图
  const closed = await call(`/api/batches/${batch.id}/close`, { method: 'POST', body: {} });
  check('关闭批次 200', closed.status === 200, `HTTP ${closed.status}`);
  const addAfterClose = await call(`/api/batches/${batch.id}/images`, {
    method: 'POST',
    body: { op: 'add', upload_ids: [uploads[1]] },
  });
  check('关闭后改图 409', addAfterClose.status === 409, `HTTP ${addAfterClose.status}`);

  // 14. 删除批次（只删批次 JSON，视频仍在）
  const removed = await call(`/api/batches/${batch.id}`, { method: 'DELETE' });
  check('删除批次 200', removed.status === 200, `HTTP ${removed.status}`);
  const gone = await call(`/api/batches/${batch.id}`);
  check('删除后 404', gone.status === 404, `HTTP ${gone.status}`);

  return finish();
}

function finish() {
  console.log(`\n结果：${passed} 通过 / ${failed} 失败\n`);
  if (failed) process.exitCode = 1;
}

main().catch((err) => {
  console.error('\n脚本异常：', err);
  process.exitCode = 1;
});
