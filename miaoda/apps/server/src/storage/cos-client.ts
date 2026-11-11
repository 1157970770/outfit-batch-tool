/**
 * 腾讯云 COS 客户端（无依赖，手写 v5 签名）。
 *
 * Python 版用官方 `qcloud_cos` SDK；这里只用 `fetch` + `crypto`，避免再引一个包。
 * 只实现项目用到的五个动作：PUT / GET（含 Range）/ HEAD / DELETE / 按前缀列举。
 *
 * 签名口径（COS v5）：
 *   keyTime   = "<startSec>;<endSec>"
 *   signKey   = HMAC-SHA1(secret_key, keyTime)
 *   HttpString= method\npathname\nquery\nheaders\n
 *   StringToSign = "sha1\n" + keyTime + "\n" + SHA1(HttpString) + "\n"
 *   signature = HMAC-SHA1(signKey, StringToSign)
 */
import { createHash, createHmac } from 'node:crypto';

export interface CosConfig {
  enabled: boolean;
  bucket: string;
  region: string;
  secretId: string;
  secretKey: string;
}

export class CosError extends Error {
  constructor(
    message: string,
    readonly status = 0,
    readonly code = '',
  ) {
    super(message);
  }
}

function sha1Hex(text: string): string {
  return createHash('sha1').update(text, 'utf8').digest('hex');
}

function hmacSha1Hex(key: string, text: string): string {
  return createHmac('sha1', key).update(text, 'utf8').digest('hex');
}

function encodeSegment(text: string): string {
  return encodeURIComponent(text).replace(/[!'()*]/g, (ch) => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`);
}

/** 对象 Key → 请求路径：逐段编码，保留 `/`。 */
export function encodeKeyPath(key: string): string {
  return `/${String(key).replace(/^\/+/, '').split('/').map(encodeSegment).join('/')}`;
}

function encodeParam(text: string): string {
  return encodeURIComponent(text).replace(/[!'()*]/g, (ch) => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`);
}

function canonicalQuery(params: Record<string, string>): { query: string; list: string } {
  const keys = Object.keys(params)
    .filter((k) => params[k] !== undefined && params[k] !== null && params[k] !== '')
    .sort();
  return {
    query: keys.map((k) => `${encodeParam(k)}=${encodeParam(params[k])}`).join('&'),
    list: keys.map((k) => encodeParam(k)).join(';'),
  };
}

export class CosClient {
  constructor(private readonly cfg: CosConfig) {}

  get enabled(): boolean {
    return Boolean(this.cfg.enabled && this.cfg.bucket && this.cfg.region && this.cfg.secretId && this.cfg.secretKey);
  }

  get bucket(): string {
    return this.cfg.bucket;
  }

  host(bucket = this.cfg.bucket): string {
    return `${bucket}.cos.${this.cfg.region}.myqcloud.com`;
  }

  /** 生成 Authorization 头。`query` 为空表示没有子资源参数。 */
  private authHeader(method: string, key: string, query: Record<string, string>, extraHeaders: Record<string, string>): string {
    const now = Math.floor(Date.now() / 1000);
    const keyTime = `${now};${now + 900}`;
    const signKey = hmacSha1Hex(this.cfg.secretKey, keyTime);
    const headers: Record<string, string> = { host: this.host(), ...extraHeaders };
    const lower: Record<string, string> = {};
    for (const [name, value] of Object.entries(headers)) lower[name.toLowerCase()] = String(value);
    const headerKeys = Object.keys(lower).sort();
    const headerString = headerKeys.map((k) => `${encodeParam(k)}=${encodeParam(lower[k])}`).join('&');
    const { query: queryString, list: paramList } = canonicalQuery(query);
    const httpString = `${method.toLowerCase()}\n${encodeKeyPath(key)}\n${queryString}\n${headerString}\n`;
    const signature = hmacSha1Hex(signKey, `sha1\n${keyTime}\n${sha1Hex(httpString)}\n`);
    return [
      'q-sign-algorithm=sha1',
      `q-ak=${this.cfg.secretId}`,
      `q-sign-time=${keyTime}`,
      `q-key-time=${keyTime}`,
      `q-header-list=${headerKeys.map((k) => encodeParam(k)).join(';')}`,
      `q-url-param-list=${paramList}`,
      `q-signature=${signature}`,
    ].join('&');
  }

  /** 预签名 GET（下载/预览/客户端直连），过期时间按秒。 */
  presignGet(key: string, expiresSeconds = 3600): string {
    const now = Math.floor(Date.now() / 1000);
    const keyTime = `${now};${now + Math.max(60, expiresSeconds)}`;
    const signKey = hmacSha1Hex(this.cfg.secretKey, keyTime);
    const path = encodeKeyPath(key);
    const httpString = `get\n${path}\n\nhost=${encodeParam(this.host())}\n`;
    const signature = hmacSha1Hex(signKey, `sha1\n${keyTime}\n${sha1Hex(httpString)}\n`);
    const params = [
      'q-sign-algorithm=sha1',
      `q-ak=${this.cfg.secretId}`,
      `q-sign-time=${keyTime}`,
      `q-key-time=${keyTime}`,
      'q-header-list=host',
      'q-url-param-list=',
      `q-signature=${signature}`,
    ].join('&');
    return `https://${this.host()}${path}?${params}`;
  }

  private url(key: string, query: Record<string, string> = {}): string {
    const { query: q } = canonicalQuery(query);
    return `https://${this.host()}${encodeKeyPath(key)}${q ? `?${q}` : ''}`;
  }

  private async fail(res: Response, action: string): Promise<never> {
    let detail = '';
    try {
      detail = (await res.text()).slice(0, 300);
    } catch {
      detail = '';
    }
    const code = /<Code>([^<]+)<\/Code>/.exec(detail)?.[1] || '';
    throw new CosError(`COS ${action}失败（HTTP ${res.status}${code ? ` ${code}` : ''}）`, res.status, code);
  }

  async putObject(key: string, body: Buffer, contentType = 'application/octet-stream'): Promise<void> {
    if (!this.enabled) throw new CosError('云端存储未启用（COS 未配置）');
    const headers: Record<string, string> = {
      host: this.host(),
      'content-type': contentType,
      'content-length': String(body.length),
    };
    if (body.length) headers['content-md5'] = createHash('md5').update(body).digest('base64');
    const res = await fetch(this.url(key), {
      method: 'PUT',
      headers: {
        Authorization: this.authHeader('put', key, {}, headers),
        'Content-Type': contentType,
        'Content-Length': String(body.length),
        ...(body.length ? { 'Content-MD5': headers['content-md5'] } : {}),
      },
      body,
    });
    if (!res.ok) await this.fail(res, '上传');
  }

  /** 取对象；带 `range` 时透传 Range（预览要 206）。 */
  async getObject(key: string, range?: string): Promise<Response> {
    if (!this.enabled) throw new CosError('云端存储未启用（COS 未配置）');
    const headers: Record<string, string> = { host: this.host() };
    if (range) headers.range = range;
    const res = await fetch(this.url(key), {
      method: 'GET',
      headers: { Authorization: this.authHeader('get', key, {}, headers), ...(range ? { Range: range } : {}) },
    });
    if (!res.ok && res.status !== 206) await this.fail(res, '读取');
    return res;
  }

  async stat(key: string): Promise<{ size: number } | null> {
    if (!this.enabled) return null;
    const res = await fetch(this.url(key), {
      method: 'HEAD',
      headers: { Authorization: this.authHeader('head', key, {}, { host: this.host() }) },
    });
    if (res.status === 404) return null;
    if (!res.ok) await this.fail(res, '查询');
    return { size: Number(res.headers.get('content-length') || 0) };
  }

  async deleteObject(key: string): Promise<void> {
    if (!this.enabled) return;
    const res = await fetch(this.url(key), {
      method: 'DELETE',
      headers: { Authorization: this.authHeader('delete', key, {}, { host: this.host() }) },
    });
    if (!res.ok && res.status !== 404) await this.fail(res, '删除');
  }

  /** 按前缀列举（最多 1000 条，够本项目用）。 */
  async listPrefix(prefix: string, limit = 1000): Promise<{ key: string; size: number }[]> {
    if (!this.enabled) throw new CosError('云端存储未启用（COS 未配置）');
    const query = { prefix, 'max-keys': String(Math.min(1000, Math.max(1, limit))) };
    const res = await fetch(this.url('', query), {
      method: 'GET',
      headers: { Authorization: this.authHeader('get', '', query, { host: this.host() }) },
    });
    if (!res.ok) await this.fail(res, '列举');
    const xml = await res.text();
    const items: { key: string; size: number }[] = [];
    const re = /<Contents>([\s\S]*?)<\/Contents>/g;
    let match: RegExpExecArray | null;
    while ((match = re.exec(xml))) {
      const key = /<Key>([\s\S]*?)<\/Key>/.exec(match[1])?.[1];
      const size = /<Size>(\d+)<\/Size>/.exec(match[1])?.[1];
      if (key) items.push({ key: decodeXml(key), size: Number(size || 0) });
    }
    return items;
  }
}

function decodeXml(text: string): string {
  return text
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

/** 与 Python `app/storage.py::_slug` 同口径：URL 安全小写片段，中文退化成默认值。 */
export function slug(text: string, fallback = 'file', limit = 48): string {
  const ascii = String(text || '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/\.{2,}/g, '.')
    .toLowerCase();
  const cleaned = ascii.replace(/^[._-]+|[._-]+$/g, '');
  return (cleaned || fallback).slice(0, limit);
}

/** 与 Python `app/storage.py::path_segment` 同口径：保留中文与大小写，只替换禁用字符。 */
export function pathSegment(text: string, limit = 32): string {
  return String(text || '')
    .replace(/[\\/:*?"<>|\r\n\t]+/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[-._]+|[-._]+$/g, '')
    .slice(0, limit);
}
