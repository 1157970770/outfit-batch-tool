/** 标签页的数据形状：字段与服务端 `tag_api.py` 的输出逐字对应。 */

export interface TagRow {
  id: string;
  name: string;
  filename: string;
  relative_path: string;
  tags: string[];
  tag_categories: Record<string, string>;
  deprecated: boolean;
  deprecated_at?: string | null;
  deprecated_by: string;
  ready: boolean;
  upload_state: string;
  duration?: number | null;
  size_mb?: number | null;
  missing: boolean;
  changed: boolean;
  touched_at?: string | null;
  touched_by: string;
  uploaded_at: number;
  rating: number;
}

export interface TagLibrary {
  version: number;
  tags: string[];
  categories: Record<string, string>;
  category_order?: string[];
  usage: Record<string, number>;
}

export interface TagStats {
  total?: number;
  tagged?: number;
  deprecated?: number;
  missing?: number;
  changed?: number;
  ready?: number;
  candidates?: number;
  [key: string]: number | undefined;
}

export interface TagSnapshot {
  ok: true;
  enabled: boolean;
  source: string;
  schema?: number;
  revision: number;
  updated_at?: string;
  updated_by?: string;
  categories: string[];
  library: TagLibrary;
  videos: TagRow[];
  stats: TagStats;
  audit: Array<Record<string, unknown>>;
  manifest: { count: number; matched: number; pulled_at: number; warning: string };
  path?: string;
  error?: string;
  unchanged?: boolean;
}

export interface TagWriteResponse {
  ok: true;
  revision: number;
  changed: number;
  updated: TagRow[];
  stats: TagStats;
  library?: TagLibrary;
  affected?: number;
  becomes_untagged?: number;
  skipped?: string[];
  catalog_error?: string;
  action?: string;
  row?: TagRow;
  warning?: string;
  key?: string;
  restored?: number;
  backup?: string;
}

export interface TagVersion {
  name: string;
  kind?: string;
  created_at?: number | string;
  bytes?: number;
  note?: string;
  user?: string;
  [key: string]: unknown;
}

export interface TagVersionsResponse {
  ok: true;
  revision: number;
  versions: TagVersion[];
  auto_keep: number;
  pinned_keep: number;
  path: string;
}

export type TagStatusFilter = 'all' | 'active' | 'deprecated' | 'untagged' | 'notready';
