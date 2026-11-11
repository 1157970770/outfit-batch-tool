import { HttpException, Injectable, Logger } from '@nestjs/common';
import * as fs from 'node:fs';
import * as path from 'node:path';
import sharp from 'sharp';

import { AppConfigService } from '../config/app-config.service';
import { writeJsonAtomic } from '../common/fs-json';
import { UploadsService } from '../uploads/uploads.service';
import {
  DEFAULT_STRENGTH,
  DepurpleBadImage,
  depurpleRgb,
  parseStrength,
  type DepurpleStats,
} from './depurple';

export interface DepurpleSidecar {
  ok: boolean;
  strength: number;
  width: number;
  height: number;
  anchors: { dark: [number, number]; bg: [number, number] };
  cast_before: number;
  cast_after: number;
  elapsed_ms: number;
  created_at: number;
}

export interface DepurpleResult extends DepurpleSidecar {
  upload_id: string;
  name: string;
  cached: boolean;
}

/**
 * PS 助手（去紫）：`app/depurple.py` 的 TS 版。
 *
 * 落盘口径与 Python 完全一致：`<dataDir>/ps/<upload_id>_s<强度>.png` + 同名 `.json` 侧车，
 * 侧车不旧于原图就算命中（重复点同一强度秒回）。
 */
@Injectable()
export class PsService {
  private readonly logger = new Logger('Ps');
  /** 同一张图的并发请求串行化，避免重复算同一份结果（Python 是全局 `_PS_LOCK`）。 */
  private queue: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly config: AppConfigService,
    private readonly uploads: UploadsService,
  ) {}

  private dir(): string {
    return path.join(this.config.dataDir, 'ps');
  }

  resultPath(uploadId: string, strength: number): string {
    return path.join(this.dir(), `${uploadId}_s${strength}.png`);
  }

  /** 结果图 + 侧车都在、且不旧于原图 → 返回侧车。 */
  private readCached(outPath: string, sourcePath: string): DepurpleSidecar | null {
    const sidecarPath = outPath.replace(/\.png$/i, '.json');
    try {
      if (!fs.existsSync(outPath) || !fs.existsSync(sidecarPath)) return null;
      const outStat = fs.statSync(outPath);
      const srcStat = fs.statSync(sourcePath);
      if (outStat.mtimeMs < srcStat.mtimeMs) return null;
      return JSON.parse(fs.readFileSync(sidecarPath, 'utf8')) as DepurpleSidecar;
    } catch {
      return null;
    }
  }

  /** 读原图 → 去紫 → 原子写 PNG + 侧车。 */
  private async processFile(
    sourcePath: string,
    outPath: string,
    strength: number,
  ): Promise<DepurpleSidecar> {
    const started = Date.now();
    let raw: { data: Buffer; info: sharp.OutputInfo };
    try {
      raw = await sharp(sourcePath)
        .rotate() // 按 EXIF 方向转正：否则前后对比会一张躺一张立
        .toColorspace('srgb')
        .removeAlpha()
        .raw()
        .toBuffer({ resolveWithObject: true });
    } catch (err) {
      throw new DepurpleBadImage(`无法读取这张图片：${(err as Error).message}`);
    }

    const width = raw.info.width;
    const height = raw.info.height;
    const result = depurpleRgb(new Uint8Array(raw.data), width, height, strength);

    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    const png = await sharp(Buffer.from(result.rgb), {
      raw: { width, height, channels: 3 },
    })
      .png()
      .toBuffer();
    const tmp = `${outPath}.tmp`;
    fs.writeFileSync(tmp, png);
    fs.renameSync(tmp, outPath);

    const stats: DepurpleSidecar = {
      ok: true,
      strength: result.stats.strength,
      width: result.stats.width,
      height: result.stats.height,
      anchors: result.stats.anchors,
      cast_before: result.stats.cast_before,
      cast_after: result.stats.cast_after,
      elapsed_ms: Date.now() - started,
      created_at: Math.floor(Date.now() / 1000),
    };
    writeJsonAtomic(outPath.replace(/\.png$/i, '.json'), stats);
    return stats;
  }

  /** 强度校验失败统一成 400（`parseStrength` 抛的是 RangeError）。 */
  private strengthOf(value: unknown): number {
    try {
      return parseStrength(value);
    } catch (err) {
      throw bad((err as Error).message);
    }
  }

  /** `POST /api/ps/depurple`：owner 严格匹配；admin 可兜底处理任意 upload。 */
  async depurple(uploadId: string, strengthInput: unknown, owner: string, isAdmin: boolean): Promise<DepurpleResult> {
    if (!/^[a-f0-9]{32}$/.test(uploadId)) throw bad('请先上传一张图片。');
    const strength = this.strengthOf(strengthInput);

    const found = this.uploads.resolve(uploadId, owner) ?? (isAdmin ? this.uploads.peek(uploadId) : null);
    if (!found) throw notFound('找不到这张图片。');
    const sourcePath = found.file;
    if (!fs.existsSync(sourcePath)) throw notFound('原图已失效，请重新上传。');

    const outPath = this.resultPath(uploadId, strength);
    let cached = this.readCached(outPath, sourcePath);
    let hit = cached !== null;
    let stats = cached;

    if (!stats) {
      const run = this.queue.then(async () => {
        // 进队后再查一次：等锁期间可能已经有人算完了
        const again = this.readCached(outPath, sourcePath);
        if (again) return { stats: again, hit: true };
        const fresh = await this.processFile(sourcePath, outPath, strength);
        return { stats: fresh, hit: false };
      });
      this.queue = run.catch(() => undefined);
      const done = await run;
      stats = done.stats;
      hit = done.hit;
    } else {
      this.logger.debug(`去紫命中缓存：${uploadId} s${strength}`);
    }

    return {
      ...stats,
      upload_id: uploadId,
      name: String(found.meta.name || ''),
      cached: hit,
    };
  }

  /** `GET /api/ps/result/<id>`：只给已算过的结果图。 */
  resultFile(uploadId: string, strengthInput: unknown, owner: string, isAdmin: boolean): { file: string; name: string } {
    const strength = this.strengthOf(strengthInput);
    const found = this.uploads.resolve(uploadId, owner) ?? (isAdmin ? this.uploads.peek(uploadId) : null);
    if (!found) throw notFound('找不到这张图片。');
    const outPath = this.resultPath(uploadId, Math.max(0, Number.isFinite(strength) ? strength : DEFAULT_STRENGTH));
    if (!fs.existsSync(outPath)) throw notFound('这张图还没有生成去紫版本。');
    return { file: outPath, name: `${String(found.meta.name || uploadId).replace(/\.[^.]+$/, '')}-去紫.png` };
  }
}

function bad(message: string): HttpException {
  return new HttpException({ ok: false, error: message }, 400);
}

function notFound(message: string): HttpException {
  return new HttpException({ ok: false, error: message }, 404);
}
