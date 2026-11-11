/**
 * 构建「妙搭套壳」静态包：只打前端，后端仍是本机 Python 服务。
 *
 * 用法：
 *   node scripts/build-shell.mjs https://你的后端入口.example.com
 *   VITE_API_BASE=https://... node scripts/build-shell.mjs
 *
 * 产物：miaoda/dist-miaoda/（index.html + assets/，可直接上传到妙搭静态站点）
 * 注意：后端必须是 HTTPS。HTTPS 页面请求 http:// 属于「混合内容」，浏览器会直接拦掉。
 */
import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const webDir = join(root, 'apps', 'web');
const dist = join(webDir, 'dist');
const out = join(root, 'dist-miaoda');

const apiBase = String(process.argv[2] || process.env.VITE_API_BASE || '')
  .trim()
  .replace(/\/+$/, '');

if (!apiBase) {
  console.error(
    '缺少后端地址。用法：node scripts/build-shell.mjs https://backend.example.com\n' +
      '（留空会打成同源版本，只能由本机 Python 服务托管，不能放到妙搭。）',
  );
  process.exit(1);
}
if (apiBase.startsWith('http://') && !/^http:\/\/(127\.0\.0\.1|localhost)/.test(apiBase)) {
  console.warn(
    '⚠ 后端是本机 http:// 地址：妙搭页面若走 HTTPS，浏览器会按混合内容拦截。\n' +
      '  请给本机服务配 HTTPS 入口（反向代理 / Tunnel）后再填 https:// 地址。',
  );
}

const env = { ...process.env, VITE_API_BASE: apiBase, TEMP: join(root, '.tmp'), TMP: join(root, '.tmp') };
mkdirSync(env.TEMP, { recursive: true });

console.log(`▶ 构建前端（VITE_API_BASE=${apiBase}）…`);
const build = spawnSync('npm', ['run', 'build', '-w', '@outfit/web'], {
  cwd: root,
  env,
  stdio: 'inherit',
  shell: process.platform === 'win32',
});
if (build.status !== 0) process.exit(build.status ?? 1);

if (!existsSync(dist)) {
  console.error('构建产物不存在：' + dist);
  process.exit(1);
}

rmSync(out, { recursive: true, force: true });

// 不用 fs.cpSync：本机 Node 22 上它会让进程 fast-fail（exit 0xC0000409，无错误输出）。
function copyTree(from, to) {
  mkdirSync(to, { recursive: true });
  for (const name of readdirSync(from)) {
    const src = join(from, name);
    const dst = join(to, name);
    if (statSync(src).isDirectory()) copyTree(src, dst);
    else copyFileSync(src, dst);
  }
}

copyTree(dist, out);

function sizeOf(dir) {
  let total = 0;
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const info = statSync(full);
    total += info.isDirectory() ? sizeOf(full) : info.size;
  }
  return total;
}

console.log(`✓ 套壳静态包已生成：${out}（${(sizeOf(out) / 1024).toFixed(1)} KB）`);
console.log('  上传到妙搭静态站点即可；接口全部走 ' + apiBase);
