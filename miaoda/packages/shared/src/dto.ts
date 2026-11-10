/** 前后端共用的传输结构（字段名与现有 Python API 的 JSON 完全一致）。 */

export interface User {
  username: string;
  role: 'admin' | 'user' | string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface ApiError {
  ok: false;
  error: string;
}

export interface LoginResponse {
  ok: true;
  user: User;
}

/** GET /api/me */
export interface MeResponse {
  ok: true;
  user: User;
}

/** service 段：页面据此显示 provider / dev 徽标 / 上限。 */
export interface ServiceInfo {
  provider: string | null;
  model: string | null;
  dev_mode: boolean;
  cos_enabled: boolean;
  templates_count: number;
  templates_error: string;
  max_items: number;
  small_lane_items: number;
  max_batch_items: number;
  max_batch_run_items: number;
  produce_choices: number[];
  produce_min: number;
  max_images: number;
  max_concurrency: number;
  tag_filter_enabled: boolean;
  tagged_templates_count: number;
  source_ready_templates: number;
  untagged_skipped: number;
  unlisted_files: number | null;
  manifest_warning: string;
  notice: string;
  imagegen: Record<string, unknown> | null;
}

export interface PromptItem {
  id: string;
  title: string;
  body: string;
}

/** GET /api/bootstrap */
export interface BootstrapResponse {
  ok: true;
  user: User;
  service: ServiceInfo;
  prompts: PromptItem[];
}

/** 图片槽位：images[] 是唯一真源，下标即槽位。 */
export interface ImageSlot {
  upload_id: string;
  name: string;
  path: string;
}
