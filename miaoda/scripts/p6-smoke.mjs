/**
 * P6 自由视频冒烟：校验接口口径 + 跑通「建任务 → mock 出片 → 下载 → 取消/删除」。
 *
 * 用法：node scripts/p6-smoke.mjs [base]
 * 需要：mock 百炼（tests/mock_dashscope.py 18090）+ 指向它的隔离实例。
 */
import zlib from 'node:zlib';
const base = (process.argv[2] || process.env.SMOKE_BASE || 'http://127.0.0.1:3141').replace(/\/$/, '');const user = process.env.SMOKE_USER || 'parity';
const pass = process.env.SMOKE_PASS || 'pw-parity-123';

let cookie = '';
let passCount = 0;
let failCount = 0;

function ok(name, extra = '') {
  passCount += 1;
  console.log(`  ✓ ${name}${extra ? ` — ${extra}` : ''}`);
}

function bad(name, detail) {
  failCount += 1;
  console.log(`  ✗ ${name} — ${detail}`);
}

function check(name, cond, detail = '') {
  if (cond) ok(name, detail);
  else bad(name, detail || '断言不成立');
  return cond;
}

async function call(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (cookie) headers.Cookie = cookie;
  let body = opts.body;
  if (body !== undefined && !(body instanceof Uint8Array) && typeof body !== 'string') {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const res = await fetch(`${base}${path}`, {
    method: opts.method || (body === undefined ? 'GET' : 'POST'),
    headers,
    body,
  });
  const setCookie = res.headers.get('set-cookie');
  if (setCookie) cookie = setCookie.split(';')[0];
  const type = res.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await res.json() : null;
  const buf = payload ? null : Buffer.from(await res.arrayBuffer());
  return { status: res.status, headers: res.headers, payload, buf };
}

/** 造一个 800×1000 的纯色 PNG（服务端有 240px 下限，硬编码小图会被拒）。 */
function makePng(w = 800, h = 1000) {
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y += 1) {
    const row = y * (w * 3 + 1);
    raw[row] = 0;
    for (let x = 0; x < w; x += 1) {
      raw[row + 1 + x * 3] = 180;
      raw[row + 2 + x * 3] = 176;
      raw[row + 3 + x * 3] = 170;
    }
  }
  const idat = zlib.deflateSync(raw);
  const chunks = [
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', Buffer.from([0, 0, 0x03, 0x20, 0, 0, 0x03, 0xe8, 8, 2, 0, 0, 0])),
    chunk('IDAT', idat),
    chunk('IEND', Buffer.alloc(0)),
  ];
  return Buffer.concat(chunks);
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body) >>> 0);
  return Buffer.concat([len, body, crc]);
}

const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i += 1) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return c ^ -1;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitTerminal(id, timeoutMs = 60000) {
  const started = Date.now();
  let last = null;
  while (Date.now() - started < timeoutMs) {
    const res = await call(`/api/videogen/tasks/${id}`);
    last = res.payload?.task;
    if (last && !last.active) return last;
    await sleep(700);
  }
  return last;
}

async function main() {
  console.log(`\n== P6 自由视频冒烟 @ ${base} ==\n`);

  // 1. 登录 + bootstrap
  const login = await call('/api/login', { method: 'POST', body: { username: user, password: pass } });
  check('登录 200', login.status === 200, `status=${login.status}`);

  const boot = await call('/api/bootstrap');
  const vg = boot.payload?.service?.videogen;
  check('bootstrap 下发 videogen', Boolean(vg), vg ? `configured=${vg.configured}` : '缺失');
  check(
    'videogen 选项字段齐全',
    Boolean(vg && Array.isArray(vg.modes) && vg.modes.length === 3 && Array.isArray(vg.durations) && vg.limits),
    vg ? `modes=${vg.modes.map((m) => m.value).join('/')} durations=${vg.durations.map((d) => d.value).join('/')}` : '',
  );
  check('limits 与 Python 同口径', vg?.limits?.max_count === 4 && vg?.limits?.max_images === 10 && vg?.limits?.video_max_seconds === 15, JSON.stringify(vg?.limits || {}));

  // 2. 列表
  const list = await call('/api/videogen/tasks');
  check('任务列表 200 且是数组', list.status === 200 && Array.isArray(list.payload?.items), `items=${list.payload?.items?.length}`);

  // 3. 上传参考素材的错误口径
  const badKind = await call('/api/videogen/upload', {
    method: 'POST',
    headers: { 'X-Kind': 'image', 'X-File-Name': 'a.png', 'Content-Type': 'application/octet-stream' },
    body: new Uint8Array([1, 2, 3]),
  });
  check('非法素材类型 400', badKind.status === 400, badKind.payload?.error || '');

  const badSuffix = await call('/api/videogen/upload', {
    method: 'POST',
    headers: { 'X-Kind': 'video', 'X-File-Name': encodeURIComponent('参考.txt'), 'Content-Type': 'application/octet-stream' },
    body: new Uint8Array([1, 2, 3]),
  });
  check('错误后缀 400 文案', badSuffix.status === 400 && String(badSuffix.payload?.error || '').includes('视频只收 mp4/mov/m4v'), badSuffix.payload?.error || '');

  // 4. 参数校验
  const emptyPrompt = await call('/api/videogen/tasks', { body: { prompt: '   ' } });
  check('空提示词 400', emptyPrompt.status === 400 && String(emptyPrompt.payload?.error || '').includes('请填写提示词'), emptyPrompt.payload?.error || '');

  const editNoVideo = await call('/api/videogen/tasks', { body: { prompt: '把背景换成海边', mode: 'edit' } });
  check('视频编辑缺参考视频 400', editNoVideo.status === 400 && String(editNoVideo.payload?.error || '').includes('「视频编辑」至少需要一段参考视频'), editNoVideo.payload?.error || '');

  const tooMany = await call('/api/videogen/tasks', { body: { prompt: '海边走秀', ratio: '16:9', count: 9 } });
  check('数量越界 400', tooMany.status === 400 && String(tooMany.payload?.error || '').includes('生成数量需在 1–4 之间'), tooMany.payload?.error || '');

  const adaptive = await call('/api/videogen/tasks', { body: { prompt: '海边走秀', mode: 'reference', ratio: 'adaptive', count: 1 } });
  check('参考生视频不许 adaptive 400', adaptive.status === 400 && String(adaptive.payload?.error || '').includes('参考生视频需要具体画面比例'), adaptive.payload?.error || '');

  // 5. 正常建任务（图片参考 + mock 立即出片）
  const png = makePng();
  const up = await call('/api/uploads', {
    body: { filename: 'outfit.png', data: png.toString('base64') },
  });
  check('上传参考图 201', up.status === 201 && Boolean(up.payload?.upload_id), `id=${up.payload?.upload_id}`);
  const uploadId = up.payload?.upload_id;

  const created = await call('/api/videogen/tasks', {
    body: {
      prompt: '海边走秀，镜头缓慢推进',
      mode: 'reference',
      resolution: '720P',
      ratio: '16:9',
      duration: 5,
      count: 1,
      upload_ids: [uploadId],
    },
  });
  const task = created.payload?.task;
  check('建任务 201', created.status === 201 && Boolean(task?.id), `id=${task?.id} status=${task?.status}`);
  check('初始状态是排队中或生成中', ['排队中', '生成中'].includes(task?.status), task?.status || '');
  check('参考图写进 refs', (task?.refs?.images || []).length === 1, JSON.stringify(task?.refs?.images?.map((r) => r.name) || []));

  const done = await waitTerminal(task.id);
  check('任务跑到终态', done && !done.active, `status=${done?.status}`);
  check('分镜成功', (done?.shots || [])[0]?.status === '已完成', JSON.stringify((done?.shots || []).map((s) => s.status)));
  check('分镜带下载地址', Boolean((done?.shots || [])[0]?.download_url), (done?.shots || [])[0]?.download_url || '');

  // 分镜与参考图的下标是 1 起的（与 Python `shot-1.mp4` 同口径），用响应里的 index。
  const shotIndex = (done?.shots || [])[0]?.index ?? 1;
  const video = await call(`/api/videogen/tasks/${task.id}/videos/${shotIndex}`);
  check('成片下载 200 video/mp4', video.status === 200 && String(video.headers.get('content-type') || '').includes('video/mp4'), `status=${video.status} bytes=${video.buf?.length}`);
  check('成片内容非空', (video.buf?.length || 0) > 10000, `${video.buf?.length} 字节`);

  const videoDl = await call(`/api/videogen/tasks/${task.id}/videos/${shotIndex}?download=1`);
  check(
    '下载模式是附件',
    String(videoDl.headers.get('content-disposition') || '').startsWith('attachment'),
    videoDl.headers.get('content-disposition') || '',
  );

  const refIndex = (done?.refs?.images || [])[0]?.index ?? 1;
  const refImg = await call(`/api/videogen/tasks/${task.id}/refs/images/${refIndex}`);
  check('参考图可回读', refImg.status === 200 && String(refImg.headers.get('content-type') || '').includes('image/'), `status=${refImg.status} bytes=${refImg.buf?.length}`);

  const cancelDone = await call(`/api/videogen/tasks/${task.id}/cancel`, { method: 'POST' });
  check('已结束的任务取消 409', cancelDone.status === 409 && String(cancelDone.payload?.error || '').includes('已经结束'), cancelDone.payload?.error || '');

  // 6. 删除
  const del = await call(`/api/videogen/tasks/${task.id}`, { method: 'DELETE' });
  check('删除任务 200', del.status === 200 && del.payload?.ok === true, `status=${del.status}`);
  const gone = await call(`/api/videogen/tasks/${task.id}`);
  check('删除后 404', gone.status === 404, `status=${gone.status} ${gone.payload?.error || ''}`);

  // 7. 排队中取消（用一个长任务模拟：mock 会立即完成，所以只验证 404/权限分支）
  const missing = await call('/api/videogen/tasks/not-a-task');
  check('不存在的任务 404', missing.status === 404, `status=${missing.status}`);

  console.log(`\n结果：${passCount} 通过 / ${failCount} 失败\n`);
  if (failCount) process.exitCode = 1;
}

main().catch((err) => {
  console.error('冒烟脚本异常：', err);
  process.exitCode = 1;
});
