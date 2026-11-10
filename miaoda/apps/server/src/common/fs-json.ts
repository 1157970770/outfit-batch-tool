import * as fs from 'node:fs';
import * as path from 'node:path';

/**
 * 原子写 JSON：先写同目录临时文件再 `rename`。
 * 与 Python `tools/template_store.py` 的落盘方式一致，避免断电/并发读到半个文件。
 */
export function writeJsonAtomic(file: string, data: unknown): void {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = path.join(dir, `.${path.basename(file)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}

export function readJsonFile<T>(file: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8')) as T;
  } catch {
    return fallback;
  }
}

/** 文件名清洗：非法字符换成下划线，与 Python `path_segment()` 同口径。 */
export function pathSegment(raw: string, fallback = 'item'): string {
  const cleaned = String(raw || '')
    .replace(/[\\/:*?"<>|\x00-\x1f]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\.+$/, '');
  return cleaned || fallback;
}
