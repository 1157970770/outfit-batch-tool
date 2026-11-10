/** 服务端返回结构的 TS 视图（与 `apps/server/src/...` 里的 publicView / public() 一一对应）。 */
export type { PromptItem, ServiceInfo, User } from '@outfit/shared';

export interface TemplateItem {
  id: string;
  name: string;
  shot: string;
  duration: number | null;
  size_mb: number | null;
  tags: string[];
  tag_categories: Record<string, string[]>;
  ready: boolean;
  upload_state: string;
}

export interface TemplatesResponse {
  ok: boolean;
  items: TemplateItem[];
  categories: string[];
  tag_values: Record<string, string[]>;
  tag_filter_enabled: boolean;
  error?: string;
  total?: number;
  ready?: number;
  untagged?: number;
  retired?: number;
}

export interface JobRecordView {
  id: string;
  status: string;
  seconds?: number | null;
  error?: string;
  task_id?: string;
  request_id?: string;
  billable_seconds?: number | null;
  estimated_cost?: number | null;
  verify?: { size_kb?: number | null; duration?: number | null } | null;
  cos_key?: string;
  download_failed?: boolean;
  download_url?: string;
}

export interface JobSummaryView {
  total?: number;
  success?: number;
  failed?: number;
  canceled?: number;
  skipped?: number;
  [key: string]: unknown;
}

export interface JobView {
  id: string;
  owner: string;
  status: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  batch_id: string;
  image_name: string;
  image_urls: string[];
  image_names: string[];
  prompt_id: string;
  template_ids: string[];
  resolution: string;
  duration: number;
  total: number;
  done: number;
  message: string;
  log_tail?: string;
  cancel_requested?: boolean;
  lane?: string;
  style_no?: string;
  tags?: Record<string, string>;
  summary?: JobSummaryView;
  records: JobRecordView[];
  download_all?: string;
  report_url?: string;
  queue_position?: number;
  recheck?: RecheckView | null;
}

/** 复查（只查云端、不重新提交）的进度。 */
export interface RecheckView {
  running: boolean;
  auto: boolean;
  total: number;
  checked: number;
  success: number;
  failed: number;
  still_running: number;
  errors: number;
  started_at: number;
  finished_at: number | null;
  message: string;
}

export interface JobsResponse {
  ok: boolean;
  jobs: JobView[];
  queued: number;
  max_active: number;
  max_active_per_user: number;
  small_lane_items: number;
  max_active_small_jobs: number;
}

export interface TagFilterValue {
  filters: Record<string, string[]>;
  excludes: Record<string, string[]>;
}
