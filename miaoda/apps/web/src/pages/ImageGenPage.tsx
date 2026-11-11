import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, mediaUrl, errorText } from '@/api';
import { useVisiblePolling } from '@/lib/useVisiblePolling';
import { toast, toastError } from '@/ui/toast';

/** 生图 tab 的选项与任务形状：字段与 `app/imggen/manager.py` 的 public()/options 一致。 */

interface SizeOption {
  value: string;
  label: string;
}

interface QualityOption {
  value: string;
  label: string;
}

interface ImageModel {
  id: string;
  label: string;
  provider: 'images' | 'gemini' | string;
  price: number | string;
  sizes: SizeOption[];
  qualities: QualityOption[];
}

interface ImageGenOptions {
  configured: boolean;
  reason: string;
  models: ImageModel[];
  default_model: string;
  default_quality: string;
  max_count: number;
  max_references: number;
  price?: number | string;
  prompt_limit: number;
}

interface TaskImage {
  index: number;
  name: string;
  width?: number;
  height?: number;
  size_mb?: number;
  created_at?: number;
  url: string;
  download_url: string;
}

interface ImageTask {
  id: string;
  owner?: string;
  created_at?: number;
  updated_at?: number;
  prompt: string;
  model: string;
  model_label?: string;
  size?: string;
  size_label?: string;
  quality?: string;
  count?: number;
  status: string;
  progress?: number;
  error?: string;
  elapsed_ms?: number;
  images: TaskImage[];
  references: Array<{ index: number; name: string; url: string }>;
  active: boolean;
}

interface TaskListResponse {
  ok: true;
  items: ImageTask[];
  configured: boolean;
  reason: string;
}

interface RefSlot {
  key: string;
  name: string;
  uploadId?: string;
  url: string;
  status: 'uploading' | 'done' | 'error';
  error?: string;
}

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  canceled: '已取消',
};

const MAX_REF_MB = 20;

export function ImageGenPage() {
  const [options, setOptions] = useState<ImageGenOptions | null>(null);
  const [tasks, setTasks] = useState<ImageTask[]>([]);
  const [prompt, setPrompt] = useState('');
  const [model, setModel] = useState('');
  const [size, setSize] = useState('');
  const [quality, setQuality] = useState('');
  const [count, setCount] = useState(1);
  const [refs, setRefs] = useState<RefSlot[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refInput = useRef<HTMLInputElement>(null);

  const active = useMemo(() => tasks.some((task) => task.active), [tasks]);

  const refreshTasks = useCallback(async () => {
    try {
      const data = await api<TaskListResponse>('/api/imagegen/tasks?limit=40');
      setTasks(data.items || []);
    } catch (err) {
      // 轮询失败不打扰用户，下一轮再试
      if (!active) setError(errorText(err));
    }
  }, [active]);

  useEffect(() => {
    void refreshTasks();
  }, [refreshTasks]);

  useVisiblePolling(() => refreshTasks(), 2500, { enabled: active });

  // 选项来自 bootstrap（service.imagegen）；单独取一次，避免依赖外层 session 结构。
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await api<{ ok: true; service: { imagegen?: ImageGenOptions } }>('/api/bootstrap');
        if (!alive) return;
        const next = data.service?.imagegen || null;
        setOptions(next);
        if (next) {
          setModel((prev) => prev || next.default_model);
          setQuality((prev) => prev || next.default_quality);
        }
      } catch (err) {
        if (alive) setError(errorText(err));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const models = options?.models ?? [];
  const currentModel = models.find((item) => item.id === model) || null;

  // 换型号时把尺寸/质量拉回该型号的合法值（两套口径完全不同，绝不能沿用旧值）。
  useEffect(() => {
    if (!currentModel) return;
    const sizes = currentModel.sizes || [];
    if (!sizes.some((item) => item.value === size)) setSize(sizes[0]?.value || '');
    const qualities = currentModel.qualities || [];
    if (!qualities.some((item) => item.value === quality)) setQuality(qualities[0]?.value || '');
  }, [currentModel, size, quality]);

  const addRefs = async (files: FileList | null) => {
    if (!files || files.length === 0 || !options) return;
    const room = options.max_references - refs.length;
    if (room <= 0) {
      toastError(`参考图最多 ${options.max_references} 张。`);
      return;
    }
    const picked = Array.from(files).slice(0, room);
    if (files.length > room) toast(`只收下前 ${room} 张（上限 ${options.max_references} 张）。`, 'info');

    const created: RefSlot[] = picked.map((file) => ({
      key: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      name: file.name,
      url: URL.createObjectURL(file),
      status: 'uploading',
    }));
    setRefs((prev) => [...prev, ...created]);

    for (let i = 0; i < picked.length; i += 1) {
      const file = picked[i];
      const key = created[i].key;
      try {
        if (file.size > MAX_REF_MB * 1024 * 1024) throw new Error(`参考图不能超过 ${MAX_REF_MB}MB。`);
        const dataUrl = await readAsDataUrl(file);
        const upload = await api<{ ok: true; upload_id: string }>('/api/uploads', {
          method: 'POST',
          body: { filename: file.name, data: dataUrl },
        });
        setRefs((prev) => prev.map((slot) => (slot.key === key ? { ...slot, status: 'done', uploadId: upload.upload_id } : slot)));
      } catch (err) {
        setRefs((prev) => prev.map((slot) => (slot.key === key ? { ...slot, status: 'error', error: errorText(err) } : slot)));
      }
    }
  };

  const moveRef = (index: number, delta: number) => {
    setRefs((prev) => {
      const next = [...prev];
      const target = index + delta;
      if (target < 0 || target >= next.length) return prev;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const submit = async () => {
    setError('');
    if (!options?.configured) {
      setError(options?.reason || '生图功能未配置。');
      return;
    }
    if (!prompt.trim()) {
      setError('请先填写提示词。');
      return;
    }
    const pending = refs.filter((slot) => slot.status === 'uploading');
    if (pending.length > 0) {
      setError('参考图还在上传，等它们传完再提交。');
      return;
    }
    const broken = refs.filter((slot) => slot.status === 'error');
    if (broken.length > 0) {
      setError('有参考图上传失败，请先删掉它们。');
      return;
    }
    setBusy(true);
    try {
      await api<{ ok: true; task: ImageTask }>('/api/imagegen/tasks', {
        method: 'POST',
        body: {
          prompt,
          model,
          size,
          quality,
          count,
          upload_ids: refs.map((slot) => slot.uploadId).filter(Boolean),
        },
      });
      toast('已提交生图任务。', 'success');
      setPrompt('');
      refs.forEach((slot) => URL.revokeObjectURL(slot.url));
      setRefs([]);
      await refreshTasks();
    } catch (err) {
      const message = errorText(err);
      setError(message);
      toastError(message);
    } finally {
      setBusy(false);
    }
  };

  const removeTask = async (task: ImageTask) => {
    if (!window.confirm('删除这条生图记录？图片文件也会一起删掉。')) return;
    try {
      await api(`/api/imagegen/tasks/${task.id}`, { method: 'DELETE' });
      await refreshTasks();
    } catch (err) {
      toastError(errorText(err));
    }
  };

  const configured = options?.configured ?? false;
  const limit = options?.prompt_limit || 4000;

  return (
    <div className="ig-page">
      <header className="console-head">
        <div>
          <h1>生图</h1>
          <p>提示词 + 可选参考图直接出图；与视频链路完全独立，不占视频的在飞名额。</p>
        </div>
      </header>

      {options && !configured && (
        <p className="notice-banner">
          {options.reason || '生图功能未配置。'}（配置 `server/imagegen.json` 后重启服务）
        </p>
      )}

      <section className="panel ig-composer">
        <div className="panel-head"><h3>提示词</h3></div>
        <textarea
          className="ig-prompt"
          rows={4}
          value={prompt}
          placeholder="描述要生成的图片，例如：浅灰背景，一位穿白色针织开衫的模特，半身，柔和顶光"
          onChange={(event) => setPrompt(event.target.value)}
        />
        <div className="ig-count"><span>{prompt.length} / {limit}</span></div>

        <div className="ig-controls">
          <label className="field">
            <span>型号</span>
            <select value={model} onChange={(event) => setModel(event.target.value)}>
              {models.map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>尺寸</span>
            <select value={size} onChange={(event) => setSize(event.target.value)}>
              {(currentModel?.sizes || []).map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>质量</span>
            <select value={quality} onChange={(event) => setQuality(event.target.value)}>
              {(currentModel?.qualities || []).map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>张数</span>
            <select value={count} onChange={(event) => setCount(Number(event.target.value))}>
              {Array.from({ length: Math.max(1, options?.max_count || 1) }, (_, i) => i + 1).map((n) => (
                <option key={n} value={n}>{n} 张</option>
              ))}
            </select>
          </label>
        </div>

        <div className="ig-refs">
          <div className="ig-refs-head">
            <h3>参考图（顺序即「图1、图2…」，可上下调序）</h3>
            <span className="tag-hint">{refs.length} / {options?.max_references ?? 4}</span>
          </div>
          <div className="ig-ref-grid">
            {refs.map((slot, index) => (
              <div key={slot.key} className={`ig-ref${slot.status === 'error' ? ' is-bad' : ''}`}>
                <img src={slot.url} alt={slot.name} />
                <span className="ig-ref-order">{index + 1}</span>
                {slot.status === 'uploading' && <span className="ig-ref-tip">上传中…</span>}
                {slot.status === 'error' && <span className="ig-ref-tip">{slot.error || '上传失败'}</span>}
                <div className="ig-ref-ops">
                  <button className="link-button" onClick={() => moveRef(index, -1)} disabled={index === 0}>←</button>
                  <button className="link-button" onClick={() => moveRef(index, 1)} disabled={index === refs.length - 1}>→</button>
                  <button
                    className="link-button"
                    onClick={() => {
                      URL.revokeObjectURL(slot.url);
                      setRefs((prev) => prev.filter((item) => item.key !== slot.key));
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
            <label className="ig-ref-add">
              <input
                ref={refInput}
                type="file"
                accept=".jpg,.jpeg,.png,.webp,.bmp"
                multiple
                onChange={(event) => {
                  void addRefs(event.target.files);
                  if (refInput.current) refInput.current.value = '';
                }}
              />
              <span>＋ 加参考图</span>
            </label>
          </div>
        </div>

        {error && <p className="error-banner">{error}</p>}

        <div className="ig-actions">
          <button className="primary" onClick={() => void submit()} disabled={busy || !configured}>
            {busy ? '提交中…' : '开始生成'}
          </button>
          <button className="quiet btn-sm" onClick={() => void refreshTasks()}>刷新记录</button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>生成记录</h3>
          <span className="tag-hint">进行中的任务每 2.5 秒自动刷新</span>
        </div>
        {tasks.length === 0 ? (
          <p className="tag-empty">还没有生图记录。</p>
        ) : (
          <div className="ig-task-list">
            {tasks.map((task) => (
              <article key={task.id} className="ig-task">
                <header>
                  <span className={`chip-${task.status === 'succeeded' ? 'ok' : task.status === 'failed' ? 'bad' : 'busy'}`}>
                    {STATUS_TEXT[task.status] || task.status}
                  </span>
                  <strong>{task.model_label || task.model}</strong>
                  <span className="tag-hint">{task.size_label || task.size} · {task.count} 张{task.quality ? ` · ${task.quality}` : ''}</span>
                  <span className="ig-task-time">{formatTime(task.created_at)}</span>
                  {!task.active && (
                    <button className="link-button" onClick={() => void removeTask(task)}>删除</button>
                  )}
                </header>
                <p className="ig-task-prompt">{task.prompt}</p>
                {task.active && (
                  <div className="ig-progress"><span style={{ width: `${Math.max(4, Math.round((task.progress || 0) * 100))}%` }} /></div>
                )}
                {task.error && <p className="chip-bad">{task.error}</p>}
                {task.images.length > 0 && (
                  <div className="ig-images">
                    {task.images.map((image) => (
                      <figure key={image.index}>
                        <a href={mediaUrl(image.url)} target="_blank" rel="noreferrer">
                          <img src={mediaUrl(image.url)} alt={image.name} loading="lazy" />
                        </a>
                        <figcaption>
                          {image.width && image.height ? `${image.width}×${image.height}` : ''}
                          <a className="link-button" href={mediaUrl(image.download_url)}>下载</a>
                        </figcaption>
                      </figure>
                    ))}
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function formatTime(value?: number): string {
  if (!value) return '';
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取图片失败，请重试。'));
    reader.readAsDataURL(file);
  });
}
