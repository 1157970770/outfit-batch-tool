/**
 * 自由视频的参考素材校验：视频用 ffprobe 读时长/分辨率/帧率，音频只看扩展名与体积。
 * 与 `app/media.py::probe_video / validate_video / validate_audio` 同口径，
 * 文案逐字照搬（前端直接展示这些字符串）。
 */
import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { VideoGenError } from './videogen-models';

export const MAX_VIDEO_MB = 100;
export const MAX_AUDIO_MB = 15;
export const MAX_VIDEO_SECONDS = 15;
export const VIDEO_EXT = ['.mp4', '.mov', '.m4v'];
export const AUDIO_EXT = ['.wav', '.mp3'];

export interface VideoInfo {
  path: string;
  size_mb: number;
  format?: string;
  duration?: number;
  width?: number;
  height?: number;
  fps?: number;
  warn?: string;
}

export interface AudioInfo {
  path: string;
  size_mb: number;
}

/**
 * 找 ffprobe：显式环境变量 → 本机已知安装目录 → PATH。
 * 找不到时返回空串，调用方按「只告警放行」处理（与 Python 一致，不误伤没装 ffmpeg 的机器）。
 */
export function findFfprobe(): string {
  const candidates: string[] = [];
  const fromEnv = String(process.env.FFPROBE_PATH || '').trim();
  if (fromEnv) candidates.push(fromEnv);
  candidates.push('D:\\ffmpeg\\ffmpeg-9.0.1-essentials_build\\bin\\ffprobe.exe');
  candidates.push('/usr/bin/ffprobe', '/usr/local/bin/ffprobe', 'ffprobe');
  for (const candidate of candidates) {
    if (!candidate) continue;
    if (candidate.includes('/') || candidate.includes('\\')) {
      try {
        if (fs.existsSync(candidate)) return candidate;
      } catch {
        /* 忽略 */
      }
      continue;
    }
    return candidate; // 裸命令交给 PATH 解析
  }
  return '';
}

export function probeVideo(file: string): VideoInfo {
  const stats = fs.statSync(file);
  const info: VideoInfo = {
    path: file,
    size_mb: Math.round((stats.size / 1024 / 1024) * 1000) / 1000,
  };
  const exe = findFfprobe();
  if (!exe) {
    info.warn = '本机没有 ffprobe，跳过视频时长/帧率预检';
    return info;
  }
  try {
    const raw = execFileSync(
      exe,
      [
        '-v',
        'error',
        '-select_streams',
        'v:0',
        '-show_entries',
        'stream=width,height,r_frame_rate:format=duration,format_name',
        '-of',
        'json',
        file,
      ],
      { encoding: 'utf8', timeout: 30_000 },
    );
    const data = JSON.parse(raw || '{}') as {
      streams?: Array<Record<string, unknown>>;
      format?: Record<string, unknown>;
    };
    const stream = (data.streams || [{}])[0] || {};
    const container = data.format?.format_name;
    if (container) info.format = String(container);
    const duration = data.format?.duration;
    if (duration !== undefined && duration !== null) {
      info.duration = Math.round(Number(duration) * 100) / 100;
    }
    if (stream.width) info.width = Number(stream.width);
    if (stream.height) info.height = Number(stream.height);
    const rate = String(stream.r_frame_rate || '');
    if (rate.includes('/')) {
      const [num, den] = rate.split('/', 2);
      const n = Number(num);
      const d = Number(den);
      if (Number.isFinite(n) && Number.isFinite(d) && d !== 0) {
        info.fps = Math.round((n / d) * 100) / 100;
      }
    } else if (rate) {
      const parsed = Number(rate);
      if (Number.isFinite(parsed)) info.fps = Math.round(parsed * 100) / 100;
    }
  } catch (err) {
    const text = (err as Error).message || String(err);
    info.warn = `ffprobe 读不了：${text.slice(0, 120)}`;
  }
  return info;
}

export function validateVideo(file: string): VideoInfo {
  const info = probeVideo(file);
  const name = path.basename(file);
  const suffix = path.extname(file).toLowerCase();
  if (!VIDEO_EXT.includes(suffix)) {
    throw new VideoGenError(`视频 ${name} 扩展名不支持（只收 mp4/mov）`);
  }
  if (!info.warn && !info.format) {
    throw new VideoGenError(`视频 ${name} 内容不是有效的 mp4/mov 容器，请检查文件`);
  }
  if ((info.size_mb || 0) > MAX_VIDEO_MB) {
    throw new VideoGenError(`视频 ${name} 有 ${info.size_mb.toFixed(1)}MB，超过官方 100MB 上限`);
  }
  const duration = info.duration;
  if (duration !== undefined && !(duration >= 1 && duration <= MAX_VIDEO_SECONDS)) {
    throw new VideoGenError(`视频 ${name} 时长 ${duration.toFixed(1)}s，单段需在 1–15 秒`);
  }
  const { width, height } = info;
  if (width && height) {
    if (!(width >= 240 && width <= 4096 && height >= 240 && height <= 4096)) {
      throw new VideoGenError(`视频 ${name} 尺寸 ${width}×${height}，单边需在 240–4096px`);
    }
    if (Math.max(width / height, height / width) > 8.0) {
      throw new VideoGenError(`视频 ${name} 宽高比超过 8:1`);
    }
  }
  if (info.fps && info.fps < 16) {
    throw new VideoGenError(`视频 ${name} 帧率 ${info.fps}fps，官方要求 ≥16fps`);
  }
  return info;
}

export function validateAudio(file: string): AudioInfo {
  const stats = fs.statSync(file);
  const sizeMb = Math.round((stats.size / 1024 / 1024) * 1000) / 1000;
  const name = path.basename(file);
  if (!AUDIO_EXT.includes(path.extname(file).toLowerCase())) {
    throw new VideoGenError(`音频 ${name} 扩展名不支持（只收 wav/mp3）`);
  }
  if (sizeMb > MAX_AUDIO_MB) {
    throw new VideoGenError(`音频 ${name} 有 ${sizeMb.toFixed(1)}MB，超过官方 15MB 上限`);
  }
  return { path: file, size_mb: sizeMb };
}

const MIME_BY_EXT: Record<string, string> = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.bmp': 'image/bmp',
  '.webp': 'image/webp',
};

/** 本地图片 → data URI（未启用 COS 时随每次提交发送）。 */
export function toDataUri(file: string): string {
  const stats = fs.statSync(file);
  if (stats.size / 1024 / 1024 > 20) {
    throw new VideoGenError('图片 Base64 前已超过 20MB 上限');
  }
  const mime = MIME_BY_EXT[path.extname(file).toLowerCase()] || 'image/jpeg';
  return `data:${mime};base64,${fs.readFileSync(file).toString('base64')}`;
}

export const MAX_BASE64_TOTAL_MB = 40;

/** 未启用 COS 时按图片总量兜底（与 `app/media.py::base64_budget_error` 同文案）。 */
export function base64BudgetError(files: string[], cosEnabled: boolean): string | null {
  if (cosEnabled) return null;
  let total = 0;
  for (const file of files) {
    try {
      total += fs.statSync(file).size;
    } catch {
      /* 文件不存在交给后面的 resolve 报错 */
    }
  }
  const totalMb = total / 1024 / 1024;
  if (totalMb <= MAX_BASE64_TOTAL_MB) return null;
  return (
    `未启用 COS 时图片以 Base64 随每次提交发送，图片总体积需 ≤${MAX_BASE64_TOTAL_MB}MB，` +
    `当前 ${Math.round(totalMb * 10) / 10}MB`
  );
}
