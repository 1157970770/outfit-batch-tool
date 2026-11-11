/**
 * P4 冒烟：PS 助手（去紫）端到端。
 *
 * 用法：node scripts/p4-smoke.mjs [base]   （默认 http://127.0.0.1:3131）
 * 会往实例的 uploads + ps 目录写文件，只对测试实例跑。
 */
import { deflateSync } from 'node:zlib';

const base = (process.argv[2] || 'http://127.0.0.1:3131').replace(/\/$/, '');
const USER = process.env.P4_USER || 'parity';
const PASS = process.env.P4_PASS || 'pw-parity-123';

let pass = 0;
let fail = 0;
function check(name, ok, detail = '') {
  if (ok) {
    pass += 1;
    console.log(`  ✓ ${name}${detail ? ` — ${detail}` : ''}`);
  } else {
    fail += 1;
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

function crc32(buf) {
  let table = crc32.table;
  if (!table) {
    table = crc32.table = new Int32Array(256);
    for (let i = 0; i < 256; i += 1) {
      let c = i;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[i] = c;
    }
  }
  let crc = -1;
  for (const byte of buf) crc = table[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ -1) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

/** 造一张带紫偏的 PNG（淡紫背景 + 暗块 + 暖色块）。 */
function makePng(width = 320, height = 240) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const rowStart = y * (width * 3 + 1);
    raw[rowStart] = 0;
    for (let x = 0; x < width; x += 1) {
      const i = rowStart + 1 + x * 3;
      let rgb = [238, 230, 246];
      if (x > 20 && x < 120 && y > 20 && y < height - 20) rgb = [30, 26, 38];
      if (x > 180 && x < 300 && y > 140 && y < height - 20) rgb = [214, 168, 140];
      raw[i] = rgb[0];
      raw[i + 1] = rgb[1];
      raw[i + 2] = rgb[2];
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 2;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

let cookie = '';
async function call(path, { method = 'GET', body, raw } = {}) {
  const headers = {};
  if (cookie) headers.Cookie = cookie;
  let payload;
  if (raw) {
    headers['Content-Type'] = 'application/octet-stream';
    payload = raw;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const res = await fetch(`${base}${path}`, { method, headers, body: payload });
  const setCookie = res.headers.getSetCookie?.() ?? [];
  for (const item of setCookie) {
    const pair = item.split(';')[0];
    if (pair.startsWith('outfit_sid=')) cookie = pair;
  }
  const type = res.headers.get('content-type') || '';
  const data = type.includes('application/json') ? await res.json() : Buffer.from(await res.arrayBuffer());
  return { status: res.status, headers: res.headers, payload: data, type };
}

async function main() {
  console.log(`P4 冒烟 → ${base}`);
  const login = await call('/api/login', { method: 'POST', body: { username: USER, password: PASS } });
  check('登录', login.status === 200, `HTTP ${login.status}`);
  if (login.status !== 200) return;

  const png = makePng();
  const upload = await call('/api/uploads', {
    method: 'POST',
    body: { filename: 'ps-test.png', data: `data:image/png;base64,${png.toString('base64')}` },
  });
  check('上传原图', upload.status === 201 && Boolean(upload.payload?.upload_id), `HTTP ${upload.status}`);
  const uploadId = upload.payload?.upload_id;
  if (!uploadId) return;

  const dep = await call('/api/ps/depurple', { method: 'POST', body: { upload_id: uploadId, strength: 100 } });
  check('去紫 200', dep.status === 200 && dep.payload?.ok === true, `HTTP ${dep.status} ${dep.payload?.error || ''}`);
  check('去紫返回尺寸', dep.payload?.width === 320 && dep.payload?.height === 240,
    `${dep.payload?.width}×${dep.payload?.height}`);
  const before = Number(dep.payload?.cast_before);
  const after = Number(dep.payload?.cast_after);
  check('色偏下降', Number.isFinite(before) && Number.isFinite(after) && after < before, `${before} → ${after}`);
  check('锚点存在', Array.isArray(dep.payload?.anchors?.dark) && Array.isArray(dep.payload?.anchors?.bg),
    JSON.stringify(dep.payload?.anchors));
  check('首次不是缓存', dep.payload?.cached === false, `cached=${dep.payload?.cached}`);

  const again = await call('/api/ps/depurple', { method: 'POST', body: { upload_id: uploadId, strength: 100 } });
  check('同参数命中缓存', again.status === 200 && again.payload?.cached === true, `cached=${again.payload?.cached}`);

  const badStrength = await call('/api/ps/depurple', { method: 'POST', body: { upload_id: uploadId, strength: 150 } });
  check('强度越界 400', badStrength.status === 400, `HTTP ${badStrength.status} ${badStrength.payload?.error || ''}`);

  const missing = await call('/api/ps/depurple', { method: 'POST', body: { upload_id: 'a'.repeat(32) } });
  check('不存在的图 404', missing.status === 404, `HTTP ${missing.status} ${missing.payload?.error || ''}`);

  const result = await call(`/api/ps/result/${uploadId}?strength=100`);
  check('结果图 200', result.status === 200 && result.type.includes('image/png'), `HTTP ${result.status} ${result.type}`);
  check('结果是 PNG', Buffer.isBuffer(result.payload) && result.payload.subarray(1, 4).toString('ascii') === 'PNG',
    `${Buffer.isBuffer(result.payload) ? result.payload.length : 0} 字节`);
  check('内联显示', String(result.headers.get('content-disposition') || '').startsWith('inline'), result.headers.get('content-disposition') || '');

  const download = await call(`/api/ps/result/${uploadId}?strength=100&download=1`);
  check('下载走附件', String(download.headers.get('content-disposition') || '').startsWith('attachment'),
    download.headers.get('content-disposition') || '');

  const notComputed = await call(`/api/ps/result/${uploadId}?strength=50`);
  check('未算过的强度 404', notComputed.status === 404, `HTTP ${notComputed.status} ${notComputed.payload?.error || ''}`);

  console.log(`\nP4 冒烟：${pass} 通过 / ${fail} 失败`);
  process.exitCode = fail ? 1 : 0;
}

await main();
