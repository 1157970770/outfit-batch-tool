/** 批次 / 历史的视图类型（字段与 Python 版 `batches.public()` 一一对应）。 */
import type { JobView } from './types';

export interface BatchImageSlot {
  upload_id: string;
  name: string;
  path?: string;
  url?: string;
}

export interface BatchItemView {
  template_id: string;
  name: string;
  tags: string[];
  state: 'pending' | 'producing' | 'done' | 'failed';
  job_id: string;
  error: string;
  download_failed?: boolean;
  /** 详情接口额外下发的成片地址（有 job 且已成功时）。 */
  download_url?: string;
  url?: string;
}

export interface BatchRunView {
  job_id: string;
  count: number;
  started_at: number;
  finished_at: number | null;
  success: number;
  failed: number;
  blocked?: number;
}

export interface BatchView {
  id: string;
  batch_no: string;
  title: string;
  owner: string;
  style_no?: string;
  tags?: Record<string, string>;
  state: 'draft' | 'locked' | 'closed';
  /** 主图（定妆图）地址，与 Python 版同义，老前端/对比层靠它。 */
  image_url?: string;
  image_urls?: string[];
  image_names?: string[];
  image_upload_id?: string;
  image_path?: string;
  image_name?: string;
  prompt_id: string;
  prompt: string;
  resolution: string;
  duration: number;
  tag_filters?: Record<string, string[]>;
  tag_excludes?: Record<string, string[]>;
  items: BatchItemView[];
  runs: BatchRunView[];
  active_job_id: string;
  total_items: number;
  done: number;
  failed: number;
  pending: number;
  producing: number;
  created_at: number;
  updated_at: number;
  locked_at: number | null;
  closed_at: number | null;
  /** 生产额度提示（只给管理端用的金额字段在这里不存在）。 */
  estimate?: Record<string, unknown> | null;
}

export interface BatchListResponse {
  ok: boolean;
  batches: BatchView[];
}

export interface BatchDetailResponse {
  ok: boolean;
  batch: BatchView;
  items: BatchItemView[];
}

export interface BatchProduceResponse {
  ok: boolean;
  job?: JobView;
  batch: BatchView;
  taken?: number;
  blocked?: string[];
  skipped_download_failed?: number;
}

export interface HistoryGroupCard {
  key: string;
  day: string;
  batch_no: string;
  group_kind: 'batch' | 'owner' | 'day' | string;
  is_ungrouped: boolean;
  owner: string;
  small_jobs: number;
  big_jobs: number;
  job_count: number;
  total: number;
  success: number;
  failed: number;
  unfinished: number;
  state_sig: string;
  first_at: number | null;
  last_at: number;
}

export interface HistoryGroupsResponse {
  ok: boolean;
  groups: HistoryGroupCard[];
  empty_batches: number;
}

export interface HistoryRunView {
  no: number;
  job_id: string;
  count: number;
  started_at: number;
  finished_at: number | null;
  prompt: string;
  prompt_changed: boolean;
  success?: number;
  failed?: number;
}

export interface HistoryItemsResponse {
  ok: boolean;
  key: string;
  batch_no: string;
  items: HistoryRow[];
  runs: HistoryRunView[];
  total: number;
  offset: number;
  limit: number;
}

export interface HistoryRow {
  job_id: string;
  template_id: string;
  run_no: number;
  run_nos: number[];
  run_total: number;
  run_prompt_changed: boolean;
  image_url: string;
  template_name: string;
  owner: string;
  created_at: number;
  batch_no: string;
  job_status: string;
  status: string;
  seconds?: number | null;
  download_url: string;
  archive_warning: string;
  verify_warn: string;
}
