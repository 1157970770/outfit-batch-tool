/**
 * `manifest_overlay.json` 追加：网页上传的视频不在云端 manifest 里，用本地 overlay 补 url/key。
 *
 * 与 Python 版同格式（数组；也兼容 `{items:[...]}`）。写盘走 tmp + rename 原子替换，
 * 同一 key/name 的旧条目先剔除，避免重复上传后越堆越多。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

export interface OverlayEntry {
  id?: string;
  name: string;
  filename?: string;
  relative_path?: string;
  url?: string;
  key?: string;
  size_mb?: number;
  fingerprint?: string;
  duration?: number | null;
  tags?: string[];
  uploaded?: boolean;
}

export function readOverlay(dataDir: string): OverlayEntry[] {
  const file = path.join(dataDir, 'manifest_overlay.json');
  try {
    if (!fs.existsSync(file)) return [];
    const raw = fs.readFileSync(file, 'utf8').trim();
    if (!raw) return [];
    const data = JSON.parse(raw) as unknown;
    const list = Array.isArray(data) ? data : ((data as { items?: unknown[] }).items ?? []);
    return Array.isArray(list) ? (list as OverlayEntry[]) : [];
  } catch {
    return [];
  }
}

export function appendOverlay(dataDir: string, entry: OverlayEntry): void {
  const file = path.join(dataDir, 'manifest_overlay.json');
  fs.mkdirSync(dataDir, { recursive: true });
  const current = readOverlay(dataDir).filter(
    (item) => item.name !== entry.name && (!entry.key || item.key !== entry.key),
  );
  current.push(entry);
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(current, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}
