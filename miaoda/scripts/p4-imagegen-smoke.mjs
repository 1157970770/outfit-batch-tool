/**
 * P4 冒烟（生图）：只验接口与选项口径，**不真的生成图片**（真实调用会计费）。
 *
 * 用法：node scripts/p4-imagegen-smoke.mjs [base]
 */
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

let cookie = '';
async function call(path, { method = 'GET', body } = {}) {
  const headers = {};
  if (cookie) headers.Cookie = cookie;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(`${base}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  for (const item of res.headers.getSetCookie?.() ?? []) {
    const pair = item.split(';')[0];
    if (pair.startsWith('outfit_sid=')) cookie = pair;
  }
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text.slice(0, 80) };
  }
  return { status: res.status, payload: data };
}

async function main() {
  console.log(`生图冒烟 → ${base}`);
  const login = await call('/api/login', { method: 'POST', body: { username: USER, password: PASS } });
  check('登录', login.status === 200, `HTTP ${login.status}`);
  if (login.status !== 200) return;

  const boot = await call('/api/bootstrap');
  const options = boot.payload?.service?.imagegen;
  check('bootstrap 下发 imagegen', Boolean(options), JSON.stringify(options?.configured));
  check('型号列表非空', Array.isArray(options?.models) && options.models.length > 0,
    `${options?.models?.length ?? 0} 个型号`);
  const models = options?.models || [];
  check('每个型号都有尺寸与质量档',
    models.every((m) => (m.sizes?.length ?? 0) > 0 && (m.qualities?.length ?? 0) > 0),
    models.map((m) => `${m.id}:${m.sizes?.length}/${m.qualities?.length}`).join(' '));
  check('nano banana 有比例@档位尺寸',
    models.some((m) => m.provider === 'gemini' && (m.sizes || []).some((s) => s.value.includes('@'))),
    models.find((m) => m.provider === 'gemini')?.sizes?.slice(0, 3).map((s) => s.value).join(',') || '');

  const list = await call('/api/imagegen/tasks?limit=5');
  check('任务列表 200', list.status === 200 && Array.isArray(list.payload?.items), `HTTP ${list.status}`);

  const badModel = await call('/api/imagegen/tasks', {
    method: 'POST',
    body: { prompt: 'test', model: 'no-such-model' },
  });
  check('未知型号 400', badModel.status === 400, `HTTP ${badModel.status} ${badModel.payload?.error || ''}`);

  const empty = await call('/api/imagegen/tasks', { method: 'POST', body: { prompt: '  ' } });
  check('空提示词 400', empty.status === 400 && /提示词/.test(String(empty.payload?.error || '')),
    `${empty.status} ${empty.payload?.error || ''}`);

  const missing = await call('/api/imagegen/tasks/deadbeef', { method: 'DELETE' });
  check('删除不存在的任务 404', missing.status === 404, `HTTP ${missing.status} ${missing.payload?.error || ''}`);

  console.log(`\n生图冒烟：${pass} 通过 / ${fail} 失败`);
  process.exitCode = fail ? 1 : 0;
}

await main();
