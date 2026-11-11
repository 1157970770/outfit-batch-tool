/**
 * 构建「妙搭套壳」静态包：只有前端，后端仍是本机 Python 服务。
 *
 * 与 `miaoda/`（NestJS 全栈）分开存放：前端源码共用 `../miaoda/apps/web`，
 * 但产物、脚本、文档全部落在本工程里，互不覆盖。
 *
 * 用法：
 *   node scripts/build.mjs https://backend.example.com          # 默认 core（5 个模块）
 *   node scripts/build.mjs https://backend.example.com all      # 11 个视图全量
 *   VITE_API_BASE=https://... VITE_VIEW_SCOPE=core node scripts/build.mjs
 */
import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const shellRoot = resolve(here, '..');
const repoRoot = resolve(shellRoot, '..');
const miaodaRoot = join(repoRoot, 'miaoda');
const webDir = join(miaodaRoot, 'apps', 'web');
const viteBin = join(miaodaRoot, 'node_modules', 'vite', 'bin', 'vite.js');
const tmpRoot = join(shellRoot, '.tmp');
const tmpOut = join(tmpRoot, 'web-dist');
const out = join(shellRoot, 'dist');

const args = process.argv.slice(2);
const scopeArg = args.find((a) => a === 'core' || a === 'all');
const apiBase = String(args.find((a) => /^https?:\/\//.test(a)) || process.env.VITE_API_BASE || '')
  .trim()
  .replace(/\/+$/, '');
const scope = String(scopeArg || process.env.VITE_VIEW_SCOPE || 'core').toLowerCase();

if (!apiBase) {
  console.error(
    '缺少后端地址。用法：node scripts/build.mjs https://backend.example.com [core|all]\n' +
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
if (!existsSync(viteBin)) {
  console.error(
    '找不到 vite：' + viteBin + '\n先把 miaoda 的依赖装好：cd miaoda && npm install --ignore-scripts',
  );
  process.exit(1);
}

mkdirSync(tmpRoot, { recursive: true });
rmSync(tmpOut, { recursive: true, force: true });

const env = {
  ...process.env,
  VITE_API_BASE: apiBase,
  VITE_VIEW_SCOPE: scope,
  TEMP: tmpRoot,
  TMP: tmpRoot,
};

console.log(`▶ 构建前端（api=${apiBase}，scope=${scope}）…`);
const build = spawnSync(
  process.execPath,
  [viteBin, 'build', '--outDir', tmpOut, '--emptyOutDir'],
  { cwd: webDir, env, stdio: 'inherit' },
);
if (build.status !== 0) {
  console.error('✗ 前端构建失败（退出码 ' + build.status + '）');
  process.exit(build.status ?? 1);
}
if (!existsSync(join(tmpOut, 'index.html'))) {
  console.error('✗ 构建产物里没有 index.html：' + tmpOut);
  process.exit(1);
}

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

function sizeOf(dir) {
  let total = 0;
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const info = statSync(full);
    total += info.isDirectory() ? sizeOf(full) : info.size;
  }
  return total;
}

rmSync(out, { recursive: true, force: true });
copyTree(tmpOut, out);
rmSync(tmpOut, { recursive: true, force: true });

console.log(`✓ 套壳静态包已生成：${out}（${(sizeOf(out) / 1024).toFixed(1)} KB，scope=${scope}）`);
console.log('  下一步：node scripts/pack.mjs 打成 zip，或直接把 dist/ 上传到妙搭静态站点。');
console.log('  接口全部走 ' + apiBase);
