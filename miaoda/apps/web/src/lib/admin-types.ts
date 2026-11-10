/** 管理端的数据形状：字段与 Python `server.py` 的 /api/admin/* 输出逐字对应。 */

export interface AdminUserRow {
  username: string;
  role: string;
  enabled: boolean;
  quota: number;
  used: number;
}

export interface AdminStatus {
  ok: true;
  version: string;
  templates: number;
  queued: number;
  active: number;
  big_active: number;
  small_active: number;
  free_gb: number;
  max_concurrency: number;
  max_active_per_user: number;
  small_lane: { items: number; max_jobs: number; inflight: number };
  limits: Record<string, number>;
  users: Array<{ username: string; role: string; enabled: boolean }>;
}

export interface TemplateHealth {
  ok: true;
  workbook_rows: number;
  tagged_rows: number;
  source_ready: number;
  unmatched: number;
  untagged: number;
  ready_ratio: number;
  manifest_warning: string;
}

export interface UsageAccount {
  owner: string;
  role: string;
  enabled: boolean;
  jobs: number;
  items: number;
  success: number;
  failed: number;
  unfinished: number;
  timeout: number;
  billable_seconds: number;
  estimated_cost: number;
  last_at: number;
  success_rate: number;
}

export interface UsageTotals {
  jobs: number;
  items: number;
  success: number;
  failed: number;
  unfinished: number;
  timeout: number;
  billable_seconds: number;
  estimated_cost: number;
  accounts: number;
}

export interface UsageResponse {
  ok: true;
  owner: string;
  from: string;
  to: string;
  accounts: UsageAccount[];
  totals: UsageTotals;
}

export interface WorkRow {
  created_at: number;
  finished_at?: number | null;
  job_id: string;
  batch_id: string;
  batch_ref: string;
  template_id: string;
  template_name: string;
  status: string;
  seconds?: number | null;
  billable_seconds?: number | null;
  estimated_cost?: number | null;
  cost_source?: string;
  resolution?: string;
  duration?: number;
}

export interface WorkResponse {
  ok: true;
  owner: string;
  total: number;
  success: number;
  failed: number;
  unfinished: number;
  offset: number;
  limit: number;
  items: WorkRow[];
  total_billable_seconds: number;
  total_cost: number;
}

export interface ArchiveEntry {
  key: string;
  size?: number;
  job_id: string;
  record_id: string;
  template_id: string;
  owner: string;
  batch_ref: string;
  batch_no?: string;
  created_at?: number | null;
}

export interface ArchiveSearchResponse {
  ok: true;
  style: string;
  source: 'ledger' | 'cos';
  entries: ArchiveEntry[];
  total: number;
}

export interface ArchiveSignResponse {
  ok: true;
  links: Array<{ key: string; url: string; expires_at: number }>;
  failed: Array<{ key: string; error: string }>;
  expires_at: number;
}

export interface AdminUsersResponse {
  ok: true;
  users: AdminUserRow[];
}
