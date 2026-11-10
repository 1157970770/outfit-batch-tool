/**
 * 妙搭版 parity 冒烟测试：全部离线，直接打 HTTP，不依赖浏览器。
 *
 *   node scripts/parity.mjs                      # 默认 http://127.0.0.1:3101
 *   node scripts/parity.mjs http://127.0.0.1:3001
 *   PARITY_USER=parity PARITY_PASS=pw-parity-123 node scripts/parity.mjs
 *
 * 检查的是「与 Python 版同口径」的部分：状态码、错误文案、cookie 属性、
 * 安全头、Host/Origin 校验，以及登录后 bootstrap 的字段形状。
 */
import { request as httpRequest } from 'node:http';

const BASE = (process.argv[2] || process.env.PARITY_BASE || 'http://127.0.0.1:3101').replace(/\/$/, '');
const USER = process.env.PARITY_USER || 'parity';
const PASS = process.env.PARITY_PASS || 'pw-parity-123';

let failures = 0;
let checks = 0;

function ok(name, pass, detail = '') {
  checks += 1;
  if (pass) {
    console.log(`  ✓ ${name}${detail ? `  (${detail})` : ''}`);
  } else {
    failures += 1;
    console.log(`  ✗ ${name}${detail ? `  (${detail})` : ''}`);
  }
}

async function call(path, options = {}) {
  const res = await fetch(BASE + path, {
    redirect: 'manual',
    ...options,
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  });
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  return { status: res.status, headers: res.headers, body, text };
}

/**
 * 用 node:http 发一个带自定义 Host 的请求。
 * 为什么不用 fetch：`Host` 是 forbidden header，fetch 会静默忽略它，测不出 421。
 */
function callWithHost(path, host) {
  const url = new URL(BASE + path);
  return new Promise((resolvePromise, reject) => {
    const req = httpRequest(
      { hostname: url.hostname, port: url.port, path: url.pathname, method: 'GET', headers: { Host: host, Accept: 'application/json' } },
      (res) => {
        res.resume();
        res.on('end', () => resolvePromise({ status: res.statusCode, headers: res.headers }));
      },
    );
    req.on('error', reject);
    req.end();
  });
}

async function main() {
  console.log(`parity 目标：${BASE}\n`);

  console.log('[1] 基础探活');
  const health = await call('/healthz');
  ok('/healthz 200', health.status === 200, String(health.status));
  ok('/healthz 带 version 与 dev', Boolean(health.body?.version) && typeof health.body?.dev === 'boolean', JSON.stringify(health.body));

  console.log('\n[2] 未登录语义');
  const me = await call('/api/me');
  ok('/api/me 未登录 401', me.status === 401, String(me.status));
  ok('错误体是 {ok:false,error}', me.body?.ok === false && typeof me.body?.error === 'string', me.body?.error);

  console.log('\n[3] 安全头与 Host/Origin 校验');
  ok('Cache-Control: no-store', (health.headers.get('cache-control') || '').includes('no-store'), health.headers.get('cache-control'));
  ok('X-Frame-Options: DENY', health.headers.get('x-frame-options') === 'DENY', health.headers.get('x-frame-options'));
  ok('CSP 含 frame-ancestors none', (health.headers.get('content-security-policy') || '').includes("frame-ancestors 'none'"));
  const badHost = await callWithHost('/healthz', 'evil.example.com');
  ok('未知 Host → 421', badHost.status === 421, String(badHost.status));
  const badOrigin = await call('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: 'http://evil.example.com' },
    body: JSON.stringify({ username: USER, password: PASS }),
  });
  ok('未知 Origin → 403', badOrigin.status === 403, `${badOrigin.status} ${badOrigin.body?.error || ''}`);

  console.log('\n[4] 登录（Python 生成的 PBKDF2 哈希必须在 Node 侧验过）');
  const wrong = await call('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: USER, password: 'definitely-wrong' }),
  });
  ok('错误密码 401', wrong.status === 401, String(wrong.status));
  ok('错误密码文案一致', wrong.body?.error === '账号或密码不正确。', wrong.body?.error);

  const right = await call('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: USER, password: PASS }),
  });
  ok('正确密码 200', right.status === 200, `${right.status} ${right.text?.slice(0, 120)}`);
  const setCookie = right.headers.get('set-cookie') || '';
  ok('Set-Cookie 名为 outfit_sid', setCookie.includes('outfit_sid='), setCookie.slice(0, 60));
  ok('Cookie 带 HttpOnly/SameSite=Lax/Path=/', /HttpOnly/i.test(setCookie) && /SameSite=Lax/i.test(setCookie) && /Path=\//.test(setCookie));
  const cookie = setCookie.split(';')[0];

  console.log('\n[5] 登录后的会话与 bootstrap');
  const me2 = await call('/api/me', { headers: { Cookie: cookie } });
  ok('/api/me 200', me2.status === 200, String(me2.status));
  ok('/api/me 返回用户名', me2.body?.user?.username === USER, JSON.stringify(me2.body?.user));

  const boot = await call('/api/bootstrap', { headers: { Cookie: cookie } });
  const service = boot.body?.service || {};
  ok('/api/bootstrap 200', boot.status === 200, String(boot.status));
  ok('service.templates_count 是数字', typeof service.templates_count === 'number', String(service.templates_count));
  ok('service.max_images = 10', service.max_images === 10, String(service.max_images));
  ok('service.produce_choices ⊆ [5,10,20,50,100]', Array.isArray(service.produce_choices) && service.produce_choices.every((n) => [5, 10, 20, 50, 100].includes(n)));
  ok('prompts 已下发且不含 p01/服装定妆', Array.isArray(boot.body?.prompts) && boot.body.prompts.every((p) => p.id !== 'p01' && p.title !== '服装定妆'), `${boot.body?.prompts?.length} 条`);

  console.log('\n[6] 登出');
  const out = await call('/api/logout', { method: 'POST', headers: { Cookie: cookie, Origin: BASE } });
  ok('/api/logout 200', out.status === 200, String(out.status));
  ok('登出清 cookie', (out.headers.get('set-cookie') || '').includes('Max-Age=0'), out.headers.get('set-cookie') || '');

  console.log(`\n结果：${checks - failures}/${checks} 通过`);
  if (failures) process.exitCode = 1;
}

main().catch((err) => {
  console.error(`parity 运行失败：${err?.message || err}`);
  process.exit(1);
});
