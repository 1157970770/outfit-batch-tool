/**
 * 套壳模式冒烟：验证本机 Python 服务对「妙搭前端」的跨域支持。
 *
 * 前置：`server/web.json` 里 `cors_origins` 填了下面的 ORIGIN，且服务已重启。
 * 用法：
 *   node scripts/cors-smoke.mjs
 *   OUTFIT_CORS_BASE=http://127.0.0.1:8767 OUTFIT_CORS_ORIGIN=https://demo.feishu.cn \
 *     PARITY_USER=corsuser PARITY_PASS=cors-pass-123 node scripts/cors-smoke.mjs
 *
 * 只读 + 一次登录，不写业务数据。
 */
import http from 'node:http';

const BASE = (process.env.OUTFIT_CORS_BASE || 'http://127.0.0.1:8767').replace(/\/+$/, '');
const ORIGIN = (process.env.OUTFIT_CORS_ORIGIN || 'https://demo.feishu.cn').replace(/\/+$/, '');
const USER = process.env.PARITY_USER || 'corsuser';
const PASS = process.env.PARITY_PASS || 'cors-pass-123';
const url = new URL(BASE);

function call(method, path, { headers = {}, body = null } = {}) {
  const payload = body == null ? null : Buffer.from(JSON.stringify(body), 'utf8');
  return new Promise((resolve, reject) => {
    const merged = { Host: url.host, ...headers };
    if (payload) merged['Content-Length'] = String(payload.length);
    const req = http.request(
      { hostname: url.hostname, port: url.port, path, method, headers: merged },
      (res) => {
        let raw = '';
        res.on('data', (chunk) => (raw += chunk));
        res.on('end', () => {
          let data = null;
          try {
            data = raw ? JSON.parse(raw) : null;
          } catch {
            data = null;
          }
          resolve({ status: res.statusCode, headers: res.headers, data });
        });
      },
    );
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

let pass = 0;
let fail = 0;
function check(name, cond, detail = '') {
  if (cond) {
    pass += 1;
    console.log(`  ✓ ${name}`);
  } else {
    fail += 1;
    console.log(`  ✗ ${name} ${detail}`);
  }
}

console.log(`套壳冒烟：${BASE} ← ${ORIGIN}`);

const preflight = await call('OPTIONS', '/api/me', {
  headers: {
    Origin: ORIGIN,
    'Access-Control-Request-Method': 'GET',
    'Access-Control-Request-Headers': 'authorization',
  },
});
check('预检返回 204', preflight.status === 204, `status=${preflight.status}`);
check('回显 Allow-Origin', preflight.headers['access-control-allow-origin'] === ORIGIN);
check(
  '允许 Authorization 请求头',
  String(preflight.headers['access-control-allow-headers'] || '').toLowerCase().includes('authorization'),
);

const blocked = await call('OPTIONS', '/api/me', { headers: { Origin: 'https://evil.example.com' } });
check('非白名单来源被拒（403）', blocked.status === 403, `status=${blocked.status}`);

const anonymous = await call('GET', '/api/me', { headers: { Origin: ORIGIN } });
check('无凭证 401', anonymous.status === 401, `status=${anonymous.status}`);
check('401 也带 CORS 头', anonymous.headers['access-control-allow-origin'] === ORIGIN);

const login = await call('POST', '/api/login', {
  headers: { Origin: ORIGIN, 'Content-Type': 'application/json' },
  body: { username: USER, password: PASS },
});
check('跨域登录 200', login.status === 200, `status=${login.status}`);
check('登录下发 token', typeof login.data?.token === 'string' && login.data.token.length > 10);
check('仍设置 Cookie（同源回退）', String(login.headers['set-cookie'] || '').includes('outfit_sid='));

const token = login.data?.token || '';
const me = await call('GET', '/api/me', {
  headers: { Origin: ORIGIN, Authorization: `Bearer ${token}` },
});
check('Bearer 令牌可访问 /api/me', me.status === 200 && me.data?.user?.username === USER, `status=${me.status}`);

const media = await call('GET', `/api/me?access_token=${encodeURIComponent(token)}`, {
  headers: { Origin: ORIGIN },
});
check(
  '查询串令牌可访问 /api/me（<img>/<video> 用）',
  media.status === 200 && media.data?.user?.username === USER,
  `status=${media.status}`,
);

const badMedia = await call('GET', '/api/me?access_token=nope', { headers: { Origin: ORIGIN } });
check('错误查询串令牌 401', badMedia.status === 401, `status=${badMedia.status}`);

const sameOrigin = await call('POST', '/api/login', {
  headers: { 'Content-Type': 'application/json' },
  body: { username: USER, password: PASS },
});
check('同源模式不下发 token（向后兼容）', sameOrigin.status === 200 && sameOrigin.data?.token === undefined);

console.log(`\n${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
