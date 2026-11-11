import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiUrl, authHeaders, errorText, IS_REMOTE, mediaUrl } from '@/api';
import { useSession } from '@/session';
import { toast } from '@/ui/toast';
import { ImageSlotGrid } from '@/pages/create/ImageSlotGrid';
import { useImageSlots } from '@/lib/image-slots';
import { useVisiblePolling } from '@/lib/useVisiblePolling';
import type {
  VgListResponse,
  VgTask,
  VgTaskResponse,
  VgUploadItem,
  VideoGenOptions,
} from '@/lib/videogen-types';

const POLL_MS = 4000;
const VIDEO_SUFFIX = /\.(mp4|mov|m4v)$/i;
const AUDIO_SUFFIX = /\.(wav|mp3)$/i;
const ACTIVE = new Set(['排队中', '生成中']);

interface ReadyRef {
  uploadId: string;
  name: string;
  sizeMb: number | null;
  duration: number | null;
  width: number | null;
  height: number | null;
}

function fmtSize(mb: number | null | undefined): string {
  const value = Number(mb);
  if (!value) return '';
  return value >= 1 ? `${value.toFixed(1)} MB` : `${Math.round(value * 1024)} KB`;
}

function fmtTime(text: string): string {
  if (!text) return '';
  const date = new Date(String(text).replace(' ', 'T'));
  if (Number.isNaN(date.getTime())) return String(text);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function statusTone(status: string): string {
  if (status === '已完成') return 'chip-ok';
  if (status === '生成中') return 'chip-busy';
  if (status === '排队中') return 'chip-muted';
  if (status === '部分完成' || status === '已取消') return 'chip-warn';
  return 'chip-bad';
}

/** 原始字节流上传参考视频 / 音频（`POST /api/videogen/upload`）。 */
async function uploadRaw(file: File, kind: 'video' | 'audio'): Promise<VgUploadItem> {
  const res = await fetch(apiUrl('/api/videogen/upload'), {
    method: 'POST',
    credentials: IS_REMOTE ? 'include' : 'same-origin',
    headers: {
      'Content-Type': 'application/octet-stream',
      'X-Kind': kind,
      'X-File-Name': encodeURIComponent(file.name),
      ...authHeaders(),
    },
    body: file,
  });
  const text = await res.text();
  let data: { ok?: boolean; upload?: VgUploadItem; error?: string } = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = {};
  }
  if (!res.ok || !data.upload) throw new Error(data.error || `上传失败（HTTP ${res.status}）`);
  return data.upload;
}

export function VideogenPage() {
  const session = useSession();
  const options = (session.service as { videogen?: VideoGenOptions } | null)?.videogen;
  const limits = options?.limits;
  const images = useImageSlots();

  const [prompt, setPrompt] = useState('');
  const [negative, setNegative] = useState('');
  const [mode, setMode] = useState('auto');
  const [resolution, setResolution] = useState('1080P');
  const [ratio, setRatio] = useState('16:9');
  const [duration, setDuration] = useState(5);
  const [count, setCount] = useState(1);
  const [seed, setSeed] = useState('');
  const [audio, setAudio] = useState(false);
  const [promptExtend, setPromptExtend] = useState(true);
  const [watermark, setWatermark] = useState(false);

  const [videos, setVideos] = useState<ReadyRef[]>([]);
  const [audios, setAudios] = useState<ReadyRef[]>([]);
  const [uploading, setUploading] = useState('');
  const [tasks, setTasks] = useState<VgTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const videoInput = useRef<HTMLInputElement>(null);
  const audioInput = useRef<HTMLInputElement>(null);

  // 选项到位后把默认值灌进表单（只做一次，之后尊重用户改动）。
  const seeded = useRef(false);
  useEffect(() => {
    if (seeded.current || !options) return;
    seeded.current = true;
    setMode(options.defaults.mode || 'auto');
    setResolution(options.defaults.resolution || '1080P');
    setRatio(options.defaults.ratio || '16:9');
    setDuration(options.defaults.duration ?? 5);
    setCount(options.defaults.count ?? 1);
    setPromptExtend(Boolean(options.defaults.prompt_extend));
    setWatermark(Boolean(options.defaults.watermark));
    setAudio(Boolean(options.defaults.audio));
  }, [options]);

  const refresh = useCallback(async () => {
    try {
      const data = await api<VgListResponse>('/api/videogen/tasks?limit=40');
      setTasks(data.items || []);
      setError('');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const hasActive = tasks.some((task) => task.active);
  useVisiblePolling(refresh, POLL_MS, { enabled: hasActive });

  const modeHint = useMemo(
    () => (options?.modes || []).find((item) => item.value === mode)?.hint || '',
    [options, mode],
  );

  const hint = useMemo(() => {
    if (!limits) return '';
    const parts = [
      `总时长 = 参考视频 + 输出，最多 30 秒；一条最多 ${limits.max_count} 个分镜（分别计费）。`,
    ];
    if (options?.cos_enabled === false) {
      parts.push('未启用 COS：只能传参考图（走 Base64）；参考视频 / 音频需要 COS。');
    }
    return parts.join(' ');
  }, [limits, options]);

  async function addRefs(files: FileList | null, kind: 'video' | 'audio') {
    if (!files || !files.length) return;
    const maxMb = kind === 'video' ? limits?.video_max_mb ?? 100 : limits?.audio_max_mb ?? 15;
    const list = Array.from(files);
    setUploading(`${kind === 'video' ? '视频' : '音频'}上传中…`);
    for (const file of list) {
      const okSuffix = kind === 'video' ? VIDEO_SUFFIX.test(file.name) : AUDIO_SUFFIX.test(file.name);
      if (!okSuffix) {
        toast(
          kind === 'video' ? '视频只收 mp4/mov/m4v。' : '音频只收 wav/mp3。',
          'error',
        );
        continue;
      }
      if (file.size > maxMb * 1024 * 1024) {
        toast(`单个${kind === 'video' ? '视频' : '音频'}不能超过 ${maxMb}MB。`, 'error');
        continue;
      }
      try {
        const item = await uploadRaw(file, kind);
        const ref: ReadyRef = {
          uploadId: item.upload_id,
          name: item.name,
          sizeMb: item.size_mb,
          duration: item.duration,
          width: item.width,
          height: item.height,
        };
        if (kind === 'video') setVideos((prev) => [...prev, ref]);
        else setAudios((prev) => [...prev, ref]);
      } catch (err) {
        toast(errorText(err), 'error');
      }
    }
    setUploading('');
  }

  function resetForm() {
    setPrompt('');
    setNegative('');
    setSeed('');
    images.reset();
    setVideos([]);
    setAudios([]);
    if (options) {
      setMode(options.defaults.mode || 'auto');
      setResolution(options.defaults.resolution || '1080P');
      setRatio(options.defaults.ratio || '16:9');
      setDuration(options.defaults.duration ?? 5);
      setCount(options.defaults.count ?? 1);
      setAudio(Boolean(options.defaults.audio));
      setPromptExtend(Boolean(options.defaults.prompt_extend));
      setWatermark(Boolean(options.defaults.watermark));
    }
  }

  async function submit() {
    if (busy) return;
    if (!prompt.trim()) {
      setError('请填写提示词，描述你要生成的视频。');
      return;
    }
    setError('');
    setBusy(true);
    try {
      const uploaded = await images.uploadAll();
      if (!uploaded.ok) {
        toast(uploaded.failures[0]?.message || '参考图上传失败。', 'error');
        return;
      }
      const seedText = seed.trim();
      const body: Record<string, unknown> = {
        prompt: prompt.trim(),
        negative_prompt: negative.trim(),
        mode,
        resolution,
        ratio,
        duration,
        count,
        audio,
        prompt_extend: promptExtend,
        watermark,
        upload_ids: uploaded.uploadIds,
        video_ids: videos.map((item) => item.uploadId),
        audio_ids: audios.map((item) => item.uploadId),
      };
      if (seedText) body.seed = seedText;
      const data = await api<VgTaskResponse>('/api/videogen/tasks', { method: 'POST', body });
      toast(`已加入队列（${data.task.count} 个分镜）。`, 'success');
      resetForm();
      await refresh();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function cancelTask(id: string) {
    try {
      await api<VgTaskResponse>(`/api/videogen/tasks/${id}/cancel`, { method: 'POST', body: {} });
      toast('正在取消：当前这一条会尽快停下（云端任务不会被撤回）。', 'info');
      await refresh();
    } catch (err) {
      toast(errorText(err), 'error');
    }
  }

  async function deleteTask(id: string) {
    if (!window.confirm('删除这条记录？已生成的视频文件会一起删掉，无法恢复。')) return;
    try {
      await api(`/api/videogen/tasks/${id}`, { method: 'DELETE' });
      toast('已删除。', 'success');
      await refresh();
    } catch (err) {
      toast(errorText(err), 'error');
    }
  }

  if (!options) {
    return (
      <div className="vg-page">
        <p className="empty">视频配置读取失败，请刷新页面重试。</p>
      </div>
    );
  }

  if (!options.configured) {
    return (
      <div className="vg-page">
        <div className="error-banner">{options.reason || '未配置 wan.api_key / wan.model，自由视频不可用。'}</div>
      </div>
    );
  }

  return (
    <div className="vg-page" id="videogen-view-inner">
      <section className="panel vg-form" aria-label="生成设置">
        <div className="panel-head">
          <div>
            <h2>自由视频</h2>
            <p className="muted">
              不选模板、不填款号：给提示词就能出片，也可以带参考图 / 视频 / 音频。模型 {options.model}
            </p>
          </div>
          <span className="chip chip-muted">{options.provider}</span>
        </div>

        <label className="field">
          <span className="vg-label">提示词</span>
          <textarea
            className="vg-prompt"
            rows={5}
            value={prompt}
            maxLength={limits?.prompt_limit ?? 20000}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="例如：一位穿米色风衣的女生走在雨后的东京街头，镜头缓慢推近，电影感，浅景深"
          />
          <span className="muted vg-counter">
            {prompt.length} / {limits?.prompt_limit ?? 20000}
          </span>
        </label>

        <label className="field">
          <span className="vg-label">
            反向提示词 <span className="muted">（可选，描述不想出现的内容）</span>
          </span>
          <textarea
            rows={2}
            value={negative}
            maxLength={limits?.negative_limit ?? 2000}
            onChange={(event) => setNegative(event.target.value)}
          />
        </label>

        <div className="vg-grid vg-grid-2">
          <label className="field">
            <span className="vg-label">生成方式</span>
            <select value={mode} onChange={(event) => setMode(event.target.value)}>
              {(options.modes || []).map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
            {modeHint ? <span className="muted vg-hint-line">{modeHint}</span> : null}
          </label>
          <label className="field">
            <span className="vg-label">分辨率</span>
            <select value={resolution} onChange={(event) => setResolution(event.target.value)}>
              {(options.resolutions || []).map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="vg-grid vg-grid-3">
          <label className="field">
            <span className="vg-label">画面比例</span>
            <select value={ratio} onChange={(event) => setRatio(event.target.value)}>
              {(options.ratios || []).map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="vg-label">时长</span>
            <select
              value={duration}
              onChange={(event) => setDuration(Number(event.target.value))}
            >
              {(options.durations || []).map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="vg-label">生成条数</span>
            <select value={count} onChange={(event) => setCount(Number(event.target.value))}>
              {Array.from({ length: Math.max(1, limits?.max_count ?? 4) }, (_, index) => index + 1).map(
                (value) => (
                  <option key={value} value={value}>
                    {value} 条
                  </option>
                ),
              )}
            </select>
          </label>
        </div>

        <label className="field vg-seed">
          <span className="vg-label">
            种子 <span className="muted">（留空随机）</span>
          </span>
          <input
            type="number"
            min={-1}
            max={2147483647}
            step={1}
            value={seed}
            placeholder="随机"
            onChange={(event) => setSeed(event.target.value)}
          />
        </label>

        <div className="vg-switches">
          <label className="vg-check">
            <input type="checkbox" checked={audio} onChange={(event) => setAudio(event.target.checked)} /> 生成配音
          </label>
          <label className="vg-check">
            <input
              type="checkbox"
              checked={promptExtend}
              onChange={(event) => setPromptExtend(event.target.checked)}
            />{' '}
            提示词扩写
          </label>
          <label className="vg-check">
            <input
              type="checkbox"
              checked={watermark}
              onChange={(event) => setWatermark(event.target.checked)}
            />{' '}
            加水印
          </label>
        </div>

        <h3 className="vg-section-title">参考素材（可选）</h3>
        <p className="muted vg-note">
          参考图顺序就是「图1、图2…」；参考视频总时长与输出时长相加不能超过 30 秒。
        </p>

        <ImageSlotGrid
          slots={images.slots}
          busy={busy || images.busy}
          onFiles={images.appendFiles}
          onRemove={images.remove}
        />

        <div className="vg-ref-row">
          <div className="vg-ref-head">
            <span className="vg-label">参考视频（最多 {limits?.max_videos ?? 5} 段）</span>
            <button
              type="button"
              className="quiet btn-sm"
              onClick={() => videoInput.current?.click()}
              disabled={videos.length >= (limits?.max_videos ?? 5)}
            >
              上传视频
            </button>
            <input
              ref={videoInput}
              type="file"
              accept="video/mp4,video/quicktime,.mp4,.mov,.m4v"
              multiple
              hidden
              onChange={(event) => {
                void addRefs(event.target.files, 'video');
                event.target.value = '';
              }}
            />
          </div>
          {videos.length === 0 ? (
            <p className="muted vg-empty-line">没有参考视频（mp4 / mov / m4v，单段 ≤15 秒，≤100MB）。</p>
          ) : (
            <ul className="vg-ref-list">
              {videos.map((item, index) => (
                <li key={item.uploadId} className="vg-ref-item">
                  <span className="vg-ref-index">视频{index + 1}</span>
                  <span className="vg-ref-name" title={item.name}>
                    {item.name}
                  </span>
                  <span className="muted">
                    {item.duration ? `${item.duration}s · ` : ''}
                    {fmtSize(item.sizeMb)}
                  </span>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => setVideos((prev) => prev.filter((row) => row.uploadId !== item.uploadId))}
                  >
                    移除
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="vg-ref-row">
          <div className="vg-ref-head">
            <span className="vg-label">参考音频（最多 {limits?.max_audios ?? 5} 段）</span>
            <button
              type="button"
              className="quiet btn-sm"
              onClick={() => audioInput.current?.click()}
              disabled={audios.length >= (limits?.max_audios ?? 5)}
            >
              上传音频
            </button>
            <input
              ref={audioInput}
              type="file"
              accept="audio/wav,audio/mpeg,.wav,.mp3"
              multiple
              hidden
              onChange={(event) => {
                void addRefs(event.target.files, 'audio');
                event.target.value = '';
              }}
            />
          </div>
          {audios.length === 0 ? (
            <p className="muted vg-empty-line">没有参考音频（wav / mp3，≤15MB）。</p>
          ) : (
            <ul className="vg-ref-list">
              {audios.map((item, index) => (
                <li key={item.uploadId} className="vg-ref-item">
                  <span className="vg-ref-index">音频{index + 1}</span>
                  <span className="vg-ref-name" title={item.name}>
                    {item.name}
                  </span>
                  <span className="muted">{fmtSize(item.sizeMb)}</span>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => setAudios((prev) => prev.filter((row) => row.uploadId !== item.uploadId))}
                  >
                    移除
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {uploading ? <p className="muted">{uploading}</p> : null}

        <div className="vg-actions">
          <button type="button" className="primary" onClick={() => void submit()} disabled={busy}>
            {busy ? '提交中…' : '开始生成'}
          </button>
          <button type="button" className="quiet" onClick={resetForm} disabled={busy}>
            清空
          </button>
          <span className="muted vg-hint">{hint}</span>
        </div>
        {error ? <p className="error">{error}</p> : null}
      </section>

      <section className="vg-history" aria-label="生成记录">
        <div className="vg-history-head">
          <h2>生成记录</h2>
          <span className="muted">{loading ? '加载中…' : `${tasks.length} 条`}</span>
        </div>
        {tasks.length === 0 && !loading ? (
          <p className="empty">
            还没有生成记录。写清提示词（需要时补参考图/视频/音频），点「开始生成」。
          </p>
        ) : null}
        <div className="vg-tasks">
          {tasks.map((task) => (
            <article key={task.id} className="vg-task card">
              <header className="vg-task-head">
                <span className={`chip ${statusTone(task.status)}`}>{task.status}</span>
                <span className="vg-task-time muted">{fmtTime(task.created_at)}</span>
                <span className="muted">
                  {task.mode === 'edit' ? '视频编辑' : '参考生视频'} · {task.resolution} ·{' '}
                  {task.ratio} · {task.duration === -1 ? '自动时长' : `${task.duration}s`} ·{' '}
                  {task.count} 条
                </span>
                <span className="vg-task-ops">
                  {task.active ? (
                    <button
                      type="button"
                      className="quiet btn-sm"
                      onClick={() => void cancelTask(task.id)}
                      disabled={task.cancel_requested}
                    >
                      {task.cancel_requested ? '取消中…' : '取消'}
                    </button>
                  ) : (
                    <button type="button" className="link-button" onClick={() => void deleteTask(task.id)}>
                      删除
                    </button>
                  )}
                </span>
              </header>
              <p className="vg-task-prompt">{task.prompt}</p>
              {task.message ? <p className="muted vg-task-message">{task.message}</p> : null}
              <div className="vg-shots">
                {(task.shots || []).map((shot) => (
                  <div key={shot.index} className="vg-shot">
                    <div className="vg-shot-head">
                      <span className="muted">分镜 {shot.index}</span>
                      <span className={`chip ${statusTone(shot.status)}`}>{shot.status}</span>
                      {shot.seconds ? <span className="muted">{shot.seconds}s</span> : null}
                    </div>
                    {shot.url ? (
                      <>
                        <video src={mediaUrl(shot.url)} controls preload="metadata" className="vg-shot-video" />
                        <a className="quiet btn-sm" href={mediaUrl(shot.download_url)}>
                          下载
                        </a>
                      </>
                    ) : shot.error ? (
                      <p className="error vg-shot-error">{shot.error}</p>
                    ) : (
                      <p className="muted">等待生成…</p>
                    )}
                    {typeof shot.estimated_cost === 'number' ? (
                      <p className="muted">
                        {shot.billable_seconds ?? 0}s · ¥{shot.estimated_cost.toFixed(2)}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
