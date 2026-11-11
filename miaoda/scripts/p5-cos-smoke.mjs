/**
 * P5 真实 COS 冒烟：上传 → 建任务（mock 出片）→ 自动归档 → 账本检索 → 预签名下载 → Range。
 *
 * 前置：
 *   1. 服务实例的 `server/config.json` 里 `cos.enabled=true` 且密钥可用；
 *   2. 百炼侧走本地 mock（`MOCK_WAN=1`），否则会真的计费；
 *   3. 归档对象会写进桶里 `{cos.prefix}/output/...`，跑完用 `.tmp/cos-clean.mjs` 或
 *      控制台按前缀清理（脚本本身不删云端对象）。
 *
 * 用法：node scripts/p5-cos-smoke.mjs [base] [style]
 *   base  默认 http://127.0.0.1:3141
 *   style 默认 COS-SMOKE（归档路径里的款号段，同时用来做账本检索）
 * 环境变量：PARITY_USER / PARITY_PASS（默认 parity / pw-parity-123）
 */
import zlib from 'node:zlib';

const BASE = (process.argv[2] || 'http://127.0.0.1:3141').replace(/\/+$/, '');
const STYLE = process.argv[3] || 'COS-SMOKE';
const USER = process.env.PARITY_USER || 'parity';
const PASS = process.env.PARITY_PASS || 'pw-parity-123';

let cookie = '';
let passed = 0;
const failed = [];
function check(name, ok, extra = '') {
  if (ok) {
    passed += 1;
    console.log(`  ✓ ${name}${extra ? '  ' + extra : ''}`);
  } else {
    failed.push(name);
    console.log(`  ✗ ${name}${extra ? '  ' + extra : ''}`);
  }
}

function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i += 1) {
    c ^= buf[i];
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  }
  return (c ^ -1) >>> 0;
}

/** 现造一张 800×1000 PNG（服务端下限 240px）。 */
function png(width, height) {
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length, 0);
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(body), 0);
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 2;
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const row = y * (width * 3 + 1);
    raw[row] = 0;
    for (let x = 0; x < width; x += 1) {
      const p = row + 1 + x * 3;
      raw[p] = (x * 7 + y * 3) % 256;
      raw[p + 1] = (y * 5) % 256;
      raw[p + 2] = 200;
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

async function call(path, { method = 'GET', body } = {}) {
  const res = await fetch(BASE + path, {
    method,
    headers: {
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...(cookie ? { Cookie: cookie } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return { status: res.status, json, text, headers: res.headers };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

console.log(`▶ COS 冒烟 ${BASE}（款号 ${STYLE}）`);
const login = await call('/api/login', { method: 'POST', body: { username: USER, password: PASS } });
cookie = ((login.headers.getSetCookie?.() || []).find((c) => c.startsWith('outfit_sid=')) || '').split(';')[0];
check('登录', login.status === 200 && Boolean(cookie), 'status=' + login.status);

const boot = await call('/api/bootstrap');
const prompts = boot.json?.prompts || [];
check('bootstrap 有提示词', prompts.length > 0, `prompts=${prompts.length}`);
const promptId = prompts[0]?.id;

const up = await call('/api/uploads', {
  method: 'POST',
  body: { filename: 'cos-smoke.png', data: 'data:image/png;base64,' + png(800, 1000).toString('base64') },
});
check('上传图片', up.status === 201 && Boolean(up.json?.upload_id), `status=${up.status}`);

const created = await call('/api/jobs', {
  method: 'POST',
  body: {
    upload_ids: [up.json?.upload_id],
    selection: 'selected',
    template_id: '001',
    prompt_id: promptId,
    resolution: '480P',
    duration: 7,
    tags: { top: STYLE },
  },
});
check('建任务', created.status === 201, `status=${created.status} ${created.json?.error || ''}`);
const jobId = created.json?.job?.id;

let job = null;
for (let i = 0; i < 60; i += 1) {
  await sleep(1000);
  job = (await call(`/api/jobs/${jobId}`)).json?.job;
  if (job && ['succeeded', 'partial', 'failed', 'canceled', 'interrupted'].includes(job.status)) break;
}
check('任务终态', job?.status === 'succeeded', `status=${job?.status} msg=${job?.message || ''}`);

const rec = job?.records?.[0] || {};
check('记录成功', rec.status === 'success', `status=${rec.status} error=${rec.error || ''}`);
check('有 cos_key', typeof rec.cos_key === 'string' && rec.cos_key.length > 0, rec.cos_key || '(空)');

// Python 的 public 记录白名单不含 cos_url：归档链接要从管理端 archive-sign 拿
const search = await call(`/api/admin/archive-search?style=${encodeURIComponent(STYLE)}`);
const entries = search.json?.entries || [];
const hit = entries.find((e) => e.key === rec.cos_key) || entries[0];
check('归档检索命中', search.status === 200 && Boolean(hit), `source=${search.json?.source} total=${entries.length}`);

if (hit) {
  const sign = await call('/api/admin/archive-sign', { method: 'POST', body: { keys: [hit.key] } });
  const url = sign.json?.links?.[0]?.url || '';
  check('预签名链接', sign.status === 200 && url.includes('q-sign-algorithm=sha1'), url.slice(0, 64) + '…');

  if (url) {
    const ranged = await fetch(url, { headers: { Range: 'bytes=0-1023' } });
    const part = Buffer.from(await ranged.arrayBuffer());
    const total = Number((ranged.headers.get('content-range') || '').split('/')[1]) || 0;
    check('Range 206', ranged.status === 206 && part.length === 1024, `status=${ranged.status} len=${part.length} cr=${ranged.headers.get('content-range')}`);

    const whole = await fetch(url);
    const bytes = Buffer.from(await whole.arrayBuffer());
    check('预签名下载 200', whole.status === 200, `status=${whole.status} ctype=${whole.headers.get('content-type')} len=${bytes.length}`);
    check('全文与 Range 总长一致', total > 0 && bytes.length === total, `len=${bytes.length} total=${total}`);
  }
}

console.log(`\n结果：${passed} 通过 / ${failed.length} 失败`);
if (failed.length) {
  console.log('失败项：' + failed.join('、'));
  process.exit(1);
}
console.log('cos_key=' + rec.cos_key);
console.log('记得清理云端：只删该前缀下的对象，别碰生产 prod/');
