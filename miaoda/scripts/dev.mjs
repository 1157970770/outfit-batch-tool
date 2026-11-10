/**
 * 开发/构建编排：先编 shared（server 与 web 都要它的类型和常量），再并发起服务。
 * 为什么要自己写：本机没有 pnpm，npm workspaces 没有 `--parallel`；
 * 而且 Windows 下 vite / nest 模板各自 spawn，交给一个脚本更好收拾进程。
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const isWin = process.platform === 'win32';

function run(command, args, options = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, {
      stdio: 'inherit',
      shell: isWin,
      cwd: options.cwd || root,
      env: { ...process.env, ...options.env },
    });
    child.on('exit', (code) => (code === 0 ? resolvePromise() : reject(new Error(`${command} 退出码 ${code}`))));
    child.on('error', reject);
  });
}

function runConcurrent(commands) {
  const children = commands.map(({ command, args, cwd, env }) =>
    spawn(command, args, { stdio: 'inherit', shell: isWin, cwd: cwd || root, env: { ...process.env, ...env } }),
  );
  const shutdown = () => {
    for (const child of children) {
      if (!child.killed) child.kill();
    }
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  return Promise.all(
    children.map(
      (child) =>
        new Promise((resolvePromise) => {
          child.on('exit', (code) => resolvePromise(code ?? 0));
        }),
    ),
  );
}

async function main() {
  const mode = process.argv[2] || 'dev';
  mkdirSync(join(root, '.tmp'), { recursive: true });
  process.env.TEMP = process.env.TEMP || join(root, '.tmp');
  process.env.TMP = process.env.TMP || join(root, '.tmp');

  if (!existsSync(join(root, 'node_modules'))) {
    console.error('依赖还没装：先在 miaoda/ 下执行 npm install');
    process.exit(1);
  }

  console.log('[miaoda] 编译 packages/shared …');
  await run('npm', ['run', 'build', '--workspace', '@outfit/shared']);

  if (mode === 'build') {
    await run('npm', ['run', 'build', '--workspace', '@outfit/server']);
    await run('npm', ['run', 'build', '--workspace', '@outfit/web']);
    return;
  }

  console.log('[miaoda] 启动后端 3001 / 前端 5173（Ctrl+C 全部退出）');
  const codes = await runConcurrent([
    { command: 'npm', args: ['run', 'dev', '--workspace', '@outfit/server'] },
    { command: 'npm', args: ['run', 'dev', '--workspace', '@outfit/web'] },
  ]);
  process.exit(codes.some((code) => code !== 0) ? 1 : 0);
}

main().catch((err) => {
  console.error(`[miaoda] ${err.message || err}`);
  process.exit(1);
});
