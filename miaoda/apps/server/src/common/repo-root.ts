import * as fs from 'node:fs';
import * as path from 'node:path';

/**
 * 从本文件所在目录向上找仓库根（含 `server/web.json` 的那一层）。
 * 编译产物在 `miaoda/apps/server/dist`，源码调试时也在同一层级，所以向上最多找 6 层。
 */
export function findRepoRoot(start: string = __dirname): string {
  let dir = path.resolve(start);
  for (let i = 0; i < 7; i += 1) {
    if (fs.existsSync(path.join(dir, 'server', 'web.json'))) return dir;
    // 没有 web.json 但确实在 miaoda 工程里：退而求其次，miaoda 的父目录就是仓库根
    if (path.basename(dir) === 'miaoda') return path.dirname(dir);
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return process.cwd();
}
