/**
 * P1 主链路冒烟：登录 → 上传图片 → 建任务 → 轮询到终态 → 下载成片 / CSV / ZIP。
 *
 * 用法（先起服务，见 docs/妙搭部署.md）：
 *   node scripts/p1-smoke.mjs [baseUrl] [imagePath]
 *
 * 默认 baseUrl=http://127.0.0.1:3121、账号取 PARITY_USER/PARITY_PASS。
 * 依赖 `MOCK_WAN=1` 或真实 Key——真实 Key 会**产生费用**，默认只跑 mock。
 */
import fs from 'node:fs';
import path from 'node:path';

const base = (process.argv[2] || process.env.P1_BASE || 'http://127.0.0.1:3121').replace(/\/+$/, '');
const user = process.env.PARITY_USER || 'parity';
const pass = process.env.PARITY_PASS || 'pw-parity-123';
const imagePath =
  process.argv[3] ||
  process.env.P1_IMAGE ||
  path.resolve('..', 'web-data-dev', 'uploads', '01', '012eedbfdca9467eabf8b84302fa1cd7.jpg');

let cookie = '';
const results = [];

function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
}

async function call(url, init = {}) {
  const res = await fetch(base + url, {
    ...init,
    headers: { ...(init.headers || {}), ...(cookie ? { cookie } : {}) },
    redirect: 'manual',
  });
  const setCookie = res.headers.get('set-cookie');
  if (setCookie) cookie = setCookie.split(';')[0];
  return res;
}

async function json(url, init) {
  const res = await call(url, init);
  const text = await res.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = { raw: text.slice(0, 200) };
  }
  return { status: res.status, body, res };
}

const login = await json('/api/login', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ username: user, password: pass }),
});
check('登录', login.status === 200 && login.body.ok === true, `HTTP ${login.status}`);

const raw = fs.readFileSync(imagePath);
const upload = await json('/api/uploads', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ filename: path.basename(imagePath), data: raw.toString('base64') }),
});
const uploadId = upload.body.upload_id;
check(
  '上传定妆图',
  upload.status === 201 && !!uploadId,
  upload.status === 201 ? `${upload.body.width}x${upload.body.height}` : JSON.stringify(upload.body),
);

// 隔离环境里只有少数模板在假 manifest 中「云端就绪」，随机抽会抽到没有 url 的场景；
// 先问 /api/templates 拿 ready 的编号，再用 selected 精确挑选（生产环境两者都成立）。
const templates = await json('/api/templates');
const readyIds = (templates.body.items || []).filter((item) => item.ready).map((item) => item.id);
check('模板候选集', templates.status === 200 && readyIds.length > 0, `ready=${readyIds.length}`);
const picked = readyIds.slice(0, 2);

const created = await json('/api/jobs', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({
    upload_ids: [uploadId],
    selection: 'selected',
    template_id: picked.join(','),
    count: picked.length,
    resolution: '480P',
    duration: 5,
    tags: { top: 'A001' },
  }),
});
const jobId = created.body.job?.id;
check(
  '建任务',
  created.status === 201 && !!jobId,
  created.status === 201
    ? `status=${created.body.job?.status} lane=${created.body.job?.lane}`
    : JSON.stringify(created.body),
);

let job = created.body.job;
const deadline = Date.now() + 120_000;
const terminal = new Set(['succeeded', 'partial', 'failed', 'canceled', 'interrupted']);
while (jobId && !terminal.has(job.status) && Date.now() < deadline) {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const polled = await json(`/api/jobs/${jobId}`);
  job = polled.body.job || job;
}
check('任务到达终态', job && job.status === 'succeeded', `status=${job?.status} done=${job?.done}/${job?.total} msg=${job?.message || ''}`);

const success = (job?.records || []).filter((rec) => rec.status === 'success');
check('成片条数', success.length === (job?.total || 0), `${success.length}/${job?.total}`);

if (success.length) {
  const file = await call(`/api/jobs/${jobId}/download/${success[0].id}`, { headers: { 'X-Playback': '1' } });
  const bytes = Buffer.from(await file.arrayBuffer());
  check(
    '下载成片',
    file.status === 200 && bytes.length > 0,
    `HTTP ${file.status} ${bytes.length}B type=${file.headers.get('content-type')}`,
  );
}

const csv = await call(`/api/jobs/${jobId}/report.csv`);
const csvText = await csv.text();
check('report.csv', csv.status === 200 && csvText.includes('模板编号'), `${csvText.split('\n').length} 行`);

const zip = await call(`/api/jobs/${jobId}/files.zip`);
const zipBuf = Buffer.from(await zip.arrayBuffer());
check('files.zip', zip.status === 200 && zipBuf.length > 22, `HTTP ${zip.status} ${zipBuf.length}B`);

const list = await json('/api/jobs');
check('任务列表', list.status === 200 && Array.isArray(list.body.jobs), `${list.body.jobs?.length} 条`);

const failed = results.filter((item) => !item.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length ? 1 : 0);
