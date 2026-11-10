/**
 * 图片上传（`/api/uploads`）。
 *
 * 与 Python 版口径一致：
 * - 请求体是 JSON `{filename, data}`，`data` 是 base64（允许带 `data:image/png;base64,` 前缀）；
 * - 落盘 `uploads/<id 前两位>/<id><ext>`，元数据 `uploads/meta/<id>.json`；
 * - 扩展名与真实格式不符时按文件头纠正；
 * - 校验口径见 `media/image-info.ts`（20MB / 240–8000px / 8:1）。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomBytes } from 'node:crypto';
import { Injectable, Logger } from '@nestjs/common';
import { AppConfigService } from '../config/app-config.service';
import { relocate } from '../common/paths';
import { sniffExtension, validateImageBuffer, type ImageInfo } from '../media/image-info';

export interface UploadMeta {
  id: string;
  owner: string;
  path: string;
  name: string;
  created_at: number;
  info: Partial<ImageInfo>;
}

export const UPLOAD_ID_RE = /^[a-f0-9]{32}$/;

@Injectable()
export class UploadsService {
  private readonly logger = new Logger(UploadsService.name);

  constructor(private readonly config: AppConfigService) {}

  get uploadsDir(): string {
    return path.join(this.config.dataDir, 'uploads');
  }

  get metaDir(): string {
    return path.join(this.uploadsDir, 'meta');
  }

  readMeta(uploadId: string): UploadMeta | null {
    if (!UPLOAD_ID_RE.test(uploadId)) return null;
    try {
      const raw = fs.readFileSync(path.join(this.metaDir, `${uploadId}.json`), 'utf8');
      const meta = JSON.parse(raw) as UploadMeta;
      return meta && typeof meta === 'object' ? meta : null;
    } catch {
      return null;
    }
  }

  /** 归属校验：owner 不符或文件已丢就返回 null（与 Python `resolve_upload` 同义）。 */
  resolve(uploadId: string, owner: string): { meta: UploadMeta; file: string } | null {
    const meta = this.readMeta(uploadId);
    if (!meta || meta.owner !== owner) return null;
    const file = relocate(meta.path, this.config.dataDir);
    if (!file || !fs.existsSync(file) || !fs.statSync(file).isFile()) return null;
    return { meta: { ...meta, path: file }, file };
  }

  /** admin 兜底读取（不校验 owner）。 */
  peek(uploadId: string): { meta: UploadMeta; file: string } | null {
    const meta = this.readMeta(uploadId);
    if (!meta) return null;
    const file = relocate(meta.path, this.config.dataDir);
    if (!file || !fs.existsSync(file)) return null;
    return { meta: { ...meta, path: file }, file };
  }

  create(filename: string, base64: string, owner: string): { meta: UploadMeta } {
    const safeName = path.basename(String(filename || 'outfit.jpg')).slice(0, 120) || 'outfit.jpg';
    let ext = path.extname(safeName).toLowerCase();
    if (!['.jpg', '.jpeg', '.png', '.webp', '.bmp'].includes(ext)) {
      throw Object.assign(new Error('请上传 JPG、PNG 或 WebP 图片；iPhone HEIC 请先另存为 JPG。'), { status: 400 });
    }
    let encoded = String(base64 || '');
    if (encoded.startsWith('data:') && encoded.includes(',')) encoded = encoded.split(',', 2)[1];
    let buf: Buffer;
    try {
      buf = Buffer.from(encoded, 'base64');
    } catch {
      throw Object.assign(new Error('图片上传失败，请重新选择图片。'), { status: 400 });
    }
    const maxBytes = Math.round(this.config.maxUploadMb * 1024 * 1024);
    if (!buf.length || buf.length > maxBytes) {
      throw Object.assign(new Error(`图片为空或超过 ${Math.round(maxBytes / 1024 / 1024)}MB。`), { status: 413 });
    }
    const uploadId = randomBytes(16).toString('hex');
    const folder = path.join(this.uploadsDir, uploadId.slice(0, 2));
    let file = path.join(folder, `${uploadId}${ext}`);
    try {
      fs.mkdirSync(folder, { recursive: true });
      fs.writeFileSync(file, buf);
    } catch (err) {
      throw Object.assign(
        new Error(`图片存不进 uploads 目录：${(err as Error).message}（检查磁盘空间与目录权限后重试）`),
        { status: 500 },
      );
    }
    let info: ImageInfo;
    try {
      info = validateImageBuffer(buf, safeName);
      const actual = sniffExtension(buf);
      if (!actual) throw new Error('图片格式无法识别');
      if (actual !== ext) {
        const corrected = file.slice(0, file.length - ext.length) + actual;
        fs.renameSync(file, corrected);
        file = corrected;
        ext = actual;
      }
    } catch (err) {
      fs.rmSync(file, { force: true });
      throw Object.assign(err as Error, { status: 400 });
    }
    const meta: UploadMeta = {
      id: uploadId,
      owner,
      path: file,
      name: safeName,
      created_at: Date.now() / 1000,
      info: { ...info },
    };
    fs.mkdirSync(this.metaDir, { recursive: true });
    fs.writeFileSync(path.join(this.metaDir, `${uploadId}.json`), JSON.stringify(meta), 'utf8');
    this.logger.log(`上传 ${uploadId} (${info.width}×${info.height}, ${info.sizeMb.toFixed(2)}MB) → ${path.basename(file)}`);
    return { meta };
  }
}
