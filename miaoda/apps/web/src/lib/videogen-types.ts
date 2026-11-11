/** 自由视频 tab 的类型（字段与 `app/videogen/models.py` / `manager.py` 的 public() 一一对应）。 */

export interface VgChoice {
  value: string;
  label: string;
  hint?: string;
}

export interface VgNumberChoice {
  value: number;
  label: string;
}

export interface VgLimits {
  max_count: number;
  max_images: number;
  max_videos: number;
  max_audios: number;
  prompt_limit: number;
  negative_limit: number;
  video_max_seconds: number;
  video_max_mb: number;
  audio_max_mb: number;
  image_max_mb: number;
}

export interface VideoGenOptions {
  configured: boolean;
  reason: string;
  model: string;
  provider: string;
  cos_enabled: boolean;
  modes: VgChoice[];
  resolutions: VgChoice[];
  ratios: VgChoice[];
  durations: VgNumberChoice[];
  defaults: {
    mode: string;
    resolution: string;
    ratio: string;
    duration: number;
    count: number;
    prompt_extend: boolean;
    watermark: boolean;
    audio: boolean;
  };
  limits: VgLimits;
  modes_engine: string[];
}

export interface VgShot {
  index: number;
  status: string;
  error: string;
  message: string;
  task_id: string;
  seconds: number;
  url: string;
  download_url: string;
  billable_seconds?: number;
  estimated_cost?: number | null;
}

export interface VgRef {
  index: number;
  name: string;
  size_mb: number | null;
  duration?: number | null;
  width?: number | null;
  height?: number | null;
  url: string;
}

export interface VgTask {
  id: string;
  owner: string;
  created_at: string;
  updated_at: string;
  started_at: string;
  finished_at: string;
  status: string;
  progress: string;
  prompt: string;
  negative_prompt: string;
  mode: string;
  mode_choice: string;
  resolution: string;
  ratio: string;
  duration: number | null;
  count: number;
  seed: number | null;
  audio: boolean;
  prompt_extend: boolean;
  watermark: boolean;
  video_seconds: number | null;
  message: string;
  active: boolean;
  cancel_requested: boolean;
  shots?: VgShot[];
  refs?: { images: VgRef[]; videos: VgRef[]; audios: VgRef[] };
}

export interface VgListResponse {
  ok: boolean;
  items: VgTask[];
  configured: boolean;
  reason: string;
}

export interface VgTaskResponse {
  ok: boolean;
  task: VgTask;
}

export interface VgUploadItem {
  upload_id: string;
  kind: 'video' | 'audio';
  name: string;
  size_mb: number;
  duration: number | null;
  width: number | null;
  height: number | null;
}
