import { Injectable, Logger } from '@nestjs/common';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as dotenv from 'dotenv';

import { findRepoRoot } from '../common/repo-root';

export interface PasswordRecord {
  salt: string;
  hash: string;
  rounds?: number;
}

export interface UserRecord {
  /** 真实 web.json 是扁平结构：salt/hash/rounds 直接挂在用户上。 */
  salt?: string;
  hash?: string;
  rounds?: number;
  /** 兼容历史上嵌套写法 `{password:{salt,hash,rounds}}`。 */
  password?: PasswordRecord;
  role?: string;
  version?: number;
  enabled?: boolean;
  expires?: string;
  quota?: number;
  [key: string]: unknown;
}

export interface WebJson {
  host?: string;
  port?: number;
  session_secret?: string;
  session_hours?: number;
  max_active_jobs?: number;
  max_active_per_user?: number;
  small_lane_items?: number;
  max_active_small_jobs?: number;
  small_lane_inflight?: number;
  max_job_items?: number;
  max_batch_items?: number;
  max_batch_run_items?: number;
  max_upload_mb?: number;
  confirm_over_items?: number;
  dev_mode?: boolean;
  data_root?: string;
  material?: { url?: string };
  users?: Record<string, UserRecord>;
  [key: string]: unknown;
}

export interface WanJson {
  provider?: string;
  api_key?: string;
  model?: string;
  base_url?: string;
  region?: string;
  default_mode?: string;
  default_duration?: number;
  default_ratio?: string;
  poll_interval?: number;
  poll_timeout?: number;
  max_concurrency?: number;
  query_qps?: number;
  query_rpm?: number;
  query_rps?: number;
  max_inflight?: number;
  price_per_second?: Record<string, number | null>;
  [key: string]: unknown;
}

export interface AppJson {
  wan?: WanJson;
  rh_api_key?: string;
  manifest_url?: string;
  cos?: Record<string, unknown>;
  notice?: string;
  [key: string]: unknown;
}

export interface ImagegenJson {
  [key: string]: unknown;
}

function readJson<T>(file: string, what: string): T {
  let raw: string;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch {
    throw new Error(`读不到${what}：${file}`);
  }
  try {
    return JSON.parse(raw.replace(/^\uFEFF/, '')) as T;
  } catch (err) {
    throw new Error(`${what} 不是合法 JSON：${file}（${(err as Error).message}）`);
  }
}

function clampInt(value: unknown, fallback: number, min: number, max: number): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(n)));
}

/** 与 Python 版 `_local_hosts()` 等价：本机名、全部网卡地址、通配地址。 */
function detectLocalHosts(): Set<string> {
  const hosts = new Set<string>(['localhost', '127.0.0.1', '::1', '0.0.0.0', '::']);
  hosts.add(os.hostname().toLowerCase());
  for (const list of Object.values(os.networkInterfaces())) {
    for (const item of list ?? []) {
      const address = String(item.address || '').split('%')[0];
      if (address) hosts.add(address.toLowerCase());
    }
  }
  return hosts;
}

/**
 * 唯一配置入口：读现有的 `server/web.json` / `server/config.json` / `server/imagegen.json`，
 * 字段口径与 `app/web/settings.py` 保持一致（含上下限钳制），不改现有文件格式。
 */
@Injectable()
export class AppConfigService {
  private readonly logger = new Logger('Config');

  readonly repoRoot: string;
  readonly serverDir: string;
  readonly dataDir: string;
  readonly port: number;
  readonly host: string;
  readonly devMode: boolean;
  readonly web: WebJson;
  readonly app: AppJson;
  readonly imagegen: ImagegenJson;
  readonly webFile: string;
  readonly allowedHosts: Set<string>;

  constructor() {
    this.repoRoot = findRepoRoot();

    // .env 只补缺省值，不覆盖真实环境变量（妙搭上用平台环境变量）
    const envFile = path.join(this.repoRoot, 'miaoda', '.env');
    if (fs.existsSync(envFile)) dotenv.config({ path: envFile });

    this.serverDir = path.resolve(
      process.env.OUTFIT_SERVER_DIR
        ? path.resolve(this.repoRoot, process.env.OUTFIT_SERVER_DIR)
        : path.join(this.repoRoot, 'server'),
    );
    this.webFile = path.join(this.serverDir, 'web.json');
    this.web = readJson<WebJson>(this.webFile, '网页配置 web.json');
    this.app = readJson<AppJson>(path.join(this.serverDir, 'config.json'), '服务配置 config.json');

    const imagegenFile = path.join(this.serverDir, 'imagegen.json');
    this.imagegen = fs.existsSync(imagegenFile) ? readJson<ImagegenJson>(imagegenFile, '生图配置 imagegen.json') : {};

    const dataRoot = process.env.OUTFIT_DATA_ROOT
      ? path.resolve(this.repoRoot, process.env.OUTFIT_DATA_ROOT)
      : String(this.web.data_root || 'web-data-dev');
    const candidate = path.resolve(this.repoRoot, dataRoot);
    this.dataDir = fs.existsSync(candidate) ? candidate : path.join(this.repoRoot, 'web-data-dev');

    this.port = clampInt(process.env.PORT, 3001, 1, 65535);
    this.host = process.env.HOST || '127.0.0.1';
    this.devMode = Boolean(this.web.dev_mode);

    const extraHosts = String(process.env.OUTFIT_ALLOWED_HOSTS || '')
      .split(',')
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean);
    this.allowedHosts = extraHosts.length ? new Set(extraHosts) : detectLocalHosts();
    for (const item of extraHosts) this.allowedHosts.add(item);

    if (!this.web.session_secret) {
      throw new Error('web.json 缺少 session_secret，请先在 Python 侧执行 `python run_web.py --init`。');
    }
    if (!fs.existsSync(this.dataDir)) {
      this.logger.warn(`数据目录不存在：${this.dataDir}`);
    }
    this.logger.log(`配置就绪：server=${this.serverDir} data=${this.dataDir} 端口=${this.port}`);
  }

  get users(): Record<string, UserRecord> {
    const users = this.web.users;
    return users && typeof users === 'object' ? users : {};
  }

  get sessionSecret(): string {
    return String(this.web.session_secret || '');
  }

  get sessionHours(): number {
    return clampInt(this.web.session_hours, 12, 1, 168);
  }

  get maxActiveJobs(): number {
    return clampInt(this.web.max_active_jobs, 2, 1, 20);
  }

  get maxActivePerUser(): number {
    return clampInt(this.web.max_active_per_user, 1, 1, 20);
  }

  /** 快速通道条数上限钳死 3：配大就静默突破「小任务插队」的契约。 */
  get smallLaneItems(): number {
    return clampInt(this.web.small_lane_items, 3, 1, 3);
  }

  get maxActiveSmallJobs(): number {
    return clampInt(this.web.max_active_small_jobs, 4, 1, 20);
  }

  get smallLaneInflight(): number {
    return clampInt(this.web.small_lane_inflight, 12, 1, 50);
  }

  get maxJobItems(): number {
    return clampInt(this.web.max_job_items, 20, 1, 100);
  }

  get maxBatchItems(): number {
    return clampInt(this.web.max_batch_items, 500, 1, 500);
  }

  get maxBatchRunItems(): number {
    return clampInt(this.web.max_batch_run_items, 100, 1, 100);
  }

  get maxConcurrency(): number {
    return clampInt(this.app.wan?.max_concurrency, 3, 1, 100);
  }

  get maxUploadMb(): number {
    return clampInt(this.web.max_upload_mb, 20, 1, 200);
  }

  get confirmOverItems(): number {
    return clampInt(this.web.confirm_over_items, 5, 1, 100);
  }

  get wan() {
    return this.app.wan ?? {};
  }

  get provider(): string {
    return String(this.wan.provider || 'dashscope');
  }

  get model(): string {
    return String(this.wan.model || 'wan3.0-video');
  }

  /** 全站唯一一把 Key：`server/config.json → wan.api_key`（历史上做过每账户一把，已废弃）。 */
  get apiKey(): string {
    return String(this.wan.api_key || '');
  }

  /**
   * DashScope 基址，含 `/api/v1`。
   * `MOCK_WAN=1` 时指向本地 mock（`tests/mock_dashscope.py`），便于离线跑通主链路。
   */
  get wanBaseUrl(): string {
    if (process.env.MOCK_WAN === '1' || process.env.MOCK_WAN === 'true') {
      return String(process.env.MOCK_WAN_BASE_URL || 'http://127.0.0.1:18090/api/v1').replace(/\/+$/, '');
    }
    return String(this.wan.base_url || 'https://dashscope.aliyuncs.com/api/v1').replace(/\/+$/, '');
  }

  get submitRpm(): number {
    return clampInt(this.wan.query_rpm ?? this.wan.submit_rpm, 250, 1, 600);
  }

  get queryQps(): number {
    return clampInt(this.wan.query_qps ?? this.wan.query_rps, 15, 1, 60);
  }

  get maxInflight(): number {
    return clampInt(this.wan.max_inflight, 100, 1, 500);
  }

  get pollIntervalMs(): number {
    return clampInt(this.wan.poll_interval, 15, 1, 120) * 1000;
  }

  get pollTimeoutMs(): number {
    return clampInt(this.wan.poll_timeout, 900, 30, 7200) * 1000;
  }

  get defaultDuration(): number {
    return clampInt(this.wan.default_duration, 7, 2, 15);
  }

  get defaultMode(): string {
    return String(this.wan.default_mode || 'ref2video');
  }

  get defaultRatio(): string {
    return String(this.wan.default_ratio || '720P');
  }

  get cosEnabled(): boolean {
    const cos = (this.app.cos ?? {}) as Record<string, unknown>;
    const enabled = cos.enabled ?? cos.ENABLED;
    return enabled === true || enabled === 'true' || enabled === 1 || enabled === '1';
  }

  /** 云端模板索引地址（config.json → wan.manifest_url 或顶层 manifest_url）。 */
  get manifestUrl(): string {
    return String(this.wan.manifest_url || this.app.manifest_url || '');
  }

  get notice(): string {
    return String(this.app.notice || '');
  }

  get materialUrl(): string {
    const configured = String(this.web.material?.url || '').trim();
    return configured || 'http://192.168.10.179:8700';
  }

  /** 与 `settings.unit_price()` 一致：配置优先，其次公开价目表。 */
  unitPrice(resolution: string): { price: number | null; source: 'config' | 'default' } {
    const key = String(resolution || '').toUpperCase();
    const configured = this.app.wan?.price_per_second?.[key];
    if (configured !== null && configured !== undefined && Number.isFinite(Number(configured))) {
      return { price: Number(configured), source: 'config' };
    }
    const table: Record<string, number> = { '480P': 0.3, '720P': 0.6, '1080P': 1.2 };
    return { price: table[key] ?? null, source: 'default' };
  }
}
