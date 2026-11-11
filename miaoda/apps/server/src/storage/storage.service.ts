/**
 * 云端存储门面：Key 规则 + COS 操作 + 本地降级。
 *
 * 对应 Python `app/storage.py`：`object_key()` / `file_fingerprint()` / `guess_content_type()`
 * 与 `CosStorage` 的 put/get/stat/sign/delete/list。没有 COS 时只暴露 `enabled=false`，
 * 调用方自己决定降级方式（例如标签上传落本地并回 `warning`）。
 */
import { Injectable, Logger } from '@nestjs/common';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { AppConfigService } from '../config/app-config.service';
import { CosClient, CosConfig, CosError, pathSegment, slug } from './cos-client';

const FINGERPRINT_CHUNK = 1024 * 1024;
const CONTENT_TYPES: Record<string, string> = {
  '.mp4': 'video/mp4',
  '.m4v': 'video/mp4',
  '.mov': 'video/quicktime',
  '.mp3': 'audio/mpeg',
  '.wav': 'audio/wav',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.json': 'application/json',
  '.csv': 'text/csv; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
};

export interface ObjectKeyOptions {
  batch?: string;
  prefix?: string;
  when?: Date;
  digest?: string;
  ext?: string | null;
  style?: string;
}

@Injectable()
export class StorageService {
  private readonly logger = new Logger('Storage');
  readonly cos: CosClient;
  readonly cfg: Required<Pick<CosConfig, 'enabled' | 'bucket' | 'region'>> & { prefix: string; domain: string };

  constructor(private readonly config: AppConfigService) {
    const raw = (config.cosConfig || {}) as Record<string, unknown>;
    const cosConfig: CosConfig = {
      enabled: Boolean(raw.enabled),
      bucket: String(raw.bucket || ''),
      region: String(raw.region || ''),
      secretId: String(raw.secret_id || ''),
      secretKey: String(raw.secret_key || ''),
    };
    this.cos = new CosClient(cosConfig);
    this.cfg = {
      enabled: cosConfig.enabled,
      bucket: cosConfig.bucket,
      region: cosConfig.region,
      prefix: String(raw.prefix || ''),
      domain: String(raw.domain || ''),
    };
    if (this.cfg.enabled && !this.cos.enabled) {
      this.logger.warn('COS 已开启但 bucket/region/密钥不完整，云端功能将不可用');
    }
  }

  get enabled(): boolean {
    return this.cos.enabled;
  }

  /** 与 Python `cfg.clean_prefix()` 一致：去掉首尾斜杠。 */
  get cleanPrefix(): string {
    return this.cfg.prefix.replace(/^\/+|\/+$/g, '');
  }

  /** `{prefix}/{kind}/{YYYY-MM-DD}[/{款号}]/{batch}/{文件名}-{指纹8}.{ext}` */
  objectKey(kind: string, filename: string, options: ObjectKeyOptions = {}): string {
    const when = options.when || new Date();
    const stamp = `${when.getFullYear()}-${String(when.getMonth() + 1).padStart(2, '0')}-${String(when.getDate()).padStart(2, '0')}`;
    const parsed = path.parse(String(filename || 'file'));
    const suffixText = options.ext === null ? '' : options.ext !== undefined ? options.ext : parsed.ext;
    const suffix = slug(String(suffixText || '').replace(/^\./, ''), '', 8);
    const stem = slug(parsed.name || 'file');
    let name = options.digest ? `${stem}-${options.digest.slice(0, 8)}` : stem;
    if (suffix) name = `${name}.${suffix}`;
    const parts = [
      this.cleanPrefix,
      slug(kind, 'misc'),
      stamp,
      options.style ? pathSegment(options.style) : '',
      options.batch ? slug(options.batch, '', 32) : '',
    ].filter(Boolean);
    return [...parts, name].join('/');
  }

  /** 文件指纹：长度 + 首尾各 1MB 的 sha256（前 16 位），用于上传幂等。 */
  fingerprint(file: string): string {
    const stat = fs.statSync(file);
    const hash = createHash('sha256');
    hash.update(String(stat.size));
    const fd = fs.openSync(file, 'r');
    try {
      const head = Buffer.alloc(Math.min(FINGERPRINT_CHUNK, stat.size));
      fs.readSync(fd, head, 0, head.length, 0);
      hash.update(head);
      if (stat.size > 2 * FINGERPRINT_CHUNK) {
        const tail = Buffer.alloc(FINGERPRINT_CHUNK);
        fs.readSync(fd, tail, 0, tail.length, stat.size - FINGERPRINT_CHUNK);
        hash.update(tail);
      }
    } finally {
      fs.closeSync(fd);
    }
    return hash.digest('hex').slice(0, 16);
  }

  contentType(name: string): string {
    return CONTENT_TYPES[path.extname(String(name || '')).toLowerCase()] || 'application/octet-stream';
  }

  async putFile(file: string, key: string, contentType?: string): Promise<void> {
    await this.ensureEnabled();
    const body = fs.readFileSync(file);
    await this.cos.putObject(key, body, contentType || this.contentType(file));
  }

  async putBuffer(body: Buffer, key: string, contentType = 'application/octet-stream'): Promise<void> {
    await this.ensureEnabled();
    await this.cos.putObject(key, body, contentType);
  }

  async getObject(key: string, range?: string): Promise<Response> {
    await this.ensureEnabled();
    return this.cos.getObject(key, range);
  }

  async exists(key: string): Promise<boolean> {
    if (!this.enabled) return false;
    return (await this.cos.stat(key)) !== null;
  }

  async size(key: string): Promise<number | null> {
    if (!this.enabled) return null;
    const stat = await this.cos.stat(key);
    return stat ? stat.size : null;
  }

  async remove(key: string): Promise<void> {
    if (!this.enabled) return;
    await this.cos.deleteObject(key);
  }

  async listPrefix(prefix: string, limit = 1000): Promise<{ key: string; size: number }[]> {
    await this.ensureEnabled();
    return this.cos.listPrefix(prefix, limit);
  }

  /** 预签名下载地址；配了 `domain` 时优先走 CDN 域名（不加签名）。 */
  signUrl(key: string, expiresSeconds = 3600): string {
    const domain = this.cfg.domain.replace(/^https?:\/\//, '').replace(/\/+$/, '');
    if (domain) return `https://${domain}/${key.split('/').map(encodeURIComponent).join('/')}`;
    return this.cos.presignGet(key, expiresSeconds);
  }

  /** 云端对象的对外地址：有域名用域名，否则给预签名 URL。 */
  uri(key: string, expiresSeconds = 3600): string {
    return this.signUrl(key, expiresSeconds);
  }

  private async ensureEnabled(): Promise<void> {
    if (!this.enabled) throw new CosError('云端存储未启用（COS 未配置）', 503, 'NoCos');
  }
}
