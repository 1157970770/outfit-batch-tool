/**
 * ⚠ 套壳构建已迁到独立工程：`../miaoda-shell/`（与本工程分开，产物/文档互不覆盖）。
 *
 * 这里只保留一个转发，避免旧命令静默失效：
 *   node scripts/build-shell.mjs https://后端      →  ../miaoda-shell/scripts/build.mjs
 *
 * 新用法见 `../miaoda-shell/README.md`。
 */
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const target = resolve(here, '..', '..', 'miaoda-shell', 'scripts', 'build.mjs');

console.warn('[已迁移] 套壳构建在 miaoda-shell/：node ../miaoda-shell/scripts/build.mjs <后端地址>');
const result = spawnSync(process.execPath, [target, ...process.argv.slice(2)], {
  stdio: 'inherit',
});
process.exit(result.status ?? 1);
