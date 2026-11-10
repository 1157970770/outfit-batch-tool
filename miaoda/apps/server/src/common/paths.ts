/**
 * 数据里的绝对路径重锚。
 *
 * 任务/批次/上传记录里存的是**生成那一刻的绝对路径**，换目录、换机器后会悬空。
 * 规则与 Python 版 `app/web/paths.py::relocate` 一致：按 `web-data*` 段之后的尾部
 * 重新拼到当前 data_root 下；不在 data_root 下、且重锚目标不存在时原样返回。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

const MARKER = /(?:^|[\\/])(web-data[-A-Za-z0-9_.]*)(?=[\\/]|$)/;

/** 把 raw 路径重锚到 dataDir 下；mustExist=false 时不做存在性检查。 */
export function relocate(raw: string | null | undefined, dataDir: string, mustExist = true): string {
  const text = String(raw ?? '').trim();
  if (!text) return '';
  const normalized = text.replace(/\\/g, '/');
  const root = path.resolve(dataDir);
  const resolved = path.resolve(text);
  const inside = (candidate: string) => {
    const rel = path.relative(root, candidate);
    return !!rel && !rel.startsWith('..') && !path.isAbsolute(rel);
  };
  if (inside(resolved)) return resolved;

  const match = MARKER.exec(normalized);
  if (match && match.index >= 0) {
    const tailStart = normalized.indexOf(match[1], match.index);
    const tail = normalized.slice(tailStart + match[1].length).replace(/^[\\/]+/, '');
    if (tail) {
      const candidate = path.join(root, ...tail.split('/'));
      if (!mustExist || fs.existsSync(candidate)) return candidate;
    }
  }
  return mustExist && !fs.existsSync(resolved) ? text : resolved;
}

/** 只重锚、不碰文件系统（给前端拼 URL 用）。 */
export function relocateLoose(raw: string | null | undefined, dataDir: string): string {
  return relocate(raw, dataDir, false);
}
