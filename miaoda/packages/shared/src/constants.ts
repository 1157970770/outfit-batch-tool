/**
 * 与 Python 版逐字对齐的口径常量。
 * 改这里的任何数字之前，先确认 `app/web/batches.py` / `app/web/settings.py` 的同一份常量。
 */

/** 批次三态；「是否在跑」由 active_job_id 派生，没有 running 态。 */
export const BATCH_STATES = ['draft', 'locked', 'closed'] as const;
export type BatchState = (typeof BATCH_STATES)[number];

/** 批次条目四态。 */
export const ITEM_STATES = ['pending', 'producing', 'done', 'failed'] as const;
export type ItemState = (typeof ITEM_STATES)[number];

/** 任务终态（出现在 job.status 里）。 */
export const TERMINAL_JOB_STATES = ['succeeded', 'partial', 'failed', 'canceled', 'interrupted'] as const;
export type TerminalJobState = (typeof TERMINAL_JOB_STATES)[number];

/** 单次生产档位；接口同时接受 PRODUCE_MIN–上限的任意整数。 */
export const PRODUCE_CHOICES = [5, 10, 20, 50, 100] as const;
export const PRODUCE_MIN = 5;
export const DEFAULT_PRODUCE_COUNT = 50;
export const RETRY_MIN = 1;
export const RETRY_MAX = 100;

/** 历史分组虚拟 key。 */
export const UNGROUPED_KEY = '__ungrouped__';
export const OWNER_KEY = '__owner__';
export const DAY_KEY = '__day__';

/** 1 张定妆图 + 最多 9 张细节图（与 wan_api.MAX_IMAGES 对齐）。 */
export const MAX_IMAGE_SLOTS = 10;
export const MAX_IMAGES = 10;

/** 计费口径。 */
export const DEFAULT_DURATION = 7;
export const OUTPUT_SECONDS = 7.0;
export const DEFAULT_TEMPLATE_SECONDS = 1.45;
export const DEFAULT_PRICE_PER_SECOND: Record<string, number> = {
  '480P': 0.3,
  '720P': 0.6,
  '1080P': 1.2,
};

/** 限流口径：提交/轮询是两个独立桶。 */
export const SUBMIT_RPM = 250;
export const POLL_QPS = 15;
export const POLL_INTERVAL_MS = 15_000;
export const MAIN_INFLIGHT = 100;
export const SMALL_INFLIGHT = 12;

/** 登录限流：账号桶 8 次 / 10 分钟；IP 桶 30 次 / 10 分钟。 */
export const LOGIN_LIMIT = 8;
export const LOGIN_IP_LIMIT = 30;
export const LOGIN_WINDOW_SECONDS = 600;
export const LOGIN_MAX_KEYS = 2000;

/** 鉴权与体积。 */
export const PBKDF2_ROUNDS = 310_000;
export const SESSION_COOKIE = 'outfit_sid';
export const DEFAULT_SESSION_HOURS = 12;
export const MAX_BODY_BYTES = 32 * 1024 * 1024;

/** 上传目录布局：uploads/<xx>/<32hex>.<ext>。 */
export const UPLOAD_PATH_RE = /uploads[\\/][0-9a-f]{2}[\\/]([0-9a-f]{32})\.[A-Za-z0-9]{1,8}$/;
