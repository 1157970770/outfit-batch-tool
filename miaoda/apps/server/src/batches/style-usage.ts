/**
 * 款号-视频模板记账本（v0.11.0，1:1 移植自 `app/web/style_usage.py`）。
 *
 * 需求：同一个款号跨批次生成时不重复使用视频模板（软拦截：界面标记 + 可强制）。
 * - 记账时机：**批次生产提交时**（`BatchManager._start_run`）；小批量试跑不计入。
 * - 记账键：上衣款号（tags.top，与 COS 归档路径同源；老批次回退 style_no）。
 * - 存储：`data_dir/style_template_usage.json`，原子写，重启不丢。
 *
 * 文件结构：
 * ```json
 * {
 *   "AX111": {
 *     "T001": { "batch_no": "2026-11-秋装-01", "batch_id": "b_xxx", "at": 1730540000.0 }
 *   }
 * }
 * ```
 * 同款号同模板重复提交时覆盖旧记录（保留最近一次），保证查询语义是
 * 「这个款号最近一次用这个模板是哪一批」。
 *
 * 与 Python 的差异：Python 用 `threading.RLock` 串行化读写；Node 是单线程 + 同步 fs，
 * 而且本类的每个公开方法体内没有任何 `await`，所以「读-改-写」天然在同一个同步块里完成，
 * 不需要额外的锁对象。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

export interface StyleUsageRecord {
  batch_no: string;
  batch_id: string;
  at: number;
}

/** `{款号: {模板ID: 最近一次使用记录}}`。 */
export type StyleUsageData = Record<string, Record<string, StyleUsageRecord>>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export class StyleUsageLedger {
  readonly path: string;
  private data: StyleUsageData;

  constructor(dataDir: string) {
    this.path = path.join(dataDir, 'style_template_usage.json');
    this.data = this.load();
  }

  // ------------------------------------------------------------------ 存储

  private load(): StyleUsageData {
    try {
      const raw = JSON.parse(fs.readFileSync(this.path, 'utf8').replace(/^\uFEFF/, '')) as unknown;
      return isRecord(raw) ? (raw as StyleUsageData) : {};
    } catch {
      // 文件不存在 / 不是合法 JSON：当作空账本，绝不因此让服务起不来。
      return {};
    }
  }

  private saveLocked(): void {
    fs.mkdirSync(path.dirname(this.path), { recursive: true });
    // Python 是 `path.with_suffix(".tmp")`：style_template_usage.tmp
    const tmp = this.path.replace(/\.json$/, '.tmp');
    fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2), 'utf8');
    fs.renameSync(tmp, this.path);
  }

  // ------------------------------------------------------------------ 读写

  /** 把一次批次生产用到的模板记到款号名下。top 为空直接忽略。 */
  record(top: string, templateIds: readonly unknown[] | null | undefined, batchNo = '', batchId = ''): void {
    const style = String(top || '').trim();
    const ids = (templateIds ?? [])
      .map((value) => String(value ?? '').trim())
      .filter((value) => value.length > 0);
    if (!style || !ids.length) return;
    const now = Date.now() / 1000;
    const styles = (this.data[style] ??= {});
    for (const templateId of ids) {
      styles[templateId] = {
        batch_no: String(batchNo || ''),
        batch_id: String(batchId || ''),
        at: now,
      };
    }
    this.saveLocked();
  }

  /** 查某款号已用过的模板：`{template_id: {batch_no, batch_id, at}}`。top 为空返回空表。 */
  used(top: string): Record<string, StyleUsageRecord> {
    const style = String(top || '').trim();
    if (!style) return {};
    const styles = this.data[style];
    return isRecord(styles) ? { ...(styles as Record<string, StyleUsageRecord>) } : {};
  }
}
