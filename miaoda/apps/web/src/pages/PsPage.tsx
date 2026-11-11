import { useState } from 'react';

import { api, apiUrl, errorText } from '@/api';
import { toastError } from '@/ui/toast';

interface DepurpleResult {
  ok: true;
  upload_id: string;
  strength: number;
  width: number;
  height: number;
  name: string;
  url: string;
  download_url: string;
  cast_before?: number | null;
  cast_after?: number | null;
  anchors?: number | null;
  elapsed_ms?: number | null;
  cached?: boolean;
}

const MAX_FILE_MB = 20;
const TYPE_RE = /\.(jpe?g|png|webp|bmp)$/i;

/**
 * PS 助手（去紫）：上传 → 选强度 → 出图 + 前后色偏统计。
 *
 * 与原版一致：结果由服务端落盘缓存（`ps/<upload_id>_s<强度>.png` + 侧车 JSON），
 * 同参数重复请求直接命中，所以前端不需要自己做缓存。
 */
export function PsPage() {
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState('');
  const [strength, setStrength] = useState(100);
  const [result, setResult] = useState<DepurpleResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [dragging, setDragging] = useState(false);

  const pick = (next: File | null) => {
    setError('');
    setResult(null);
    if (!next) {
      setFile(null);
      setPreviewUrl('');
      return;
    }
    if (next.name.toLowerCase().endsWith('.heic') || next.name.toLowerCase().endsWith('.heif')) {
      setError('iPhone 的 HEIC 不能直接处理：请先另存为 JPG 再上传。');
      return;
    }
    if (!TYPE_RE.test(next.name)) {
      setError('请上传 JPG、PNG、WebP 或 BMP 图片。');
      return;
    }
    if (next.size > MAX_FILE_MB * 1024 * 1024) {
      setError(`图片不能超过 ${MAX_FILE_MB}MB，当前 ${(next.size / 1024 / 1024).toFixed(1)}MB。`);
      return;
    }
    setFile(next);
    setPreviewUrl(URL.createObjectURL(next));
  };

  const run = async () => {
    if (!file) {
      setError('请先选一张图片。');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const dataUrl = await readAsDataUrl(file);
      const upload = await api<{ ok: true; upload_id: string }>('/api/uploads', {
        method: 'POST',
        body: { filename: file.name, data: dataUrl },
      });
      const data = await api<DepurpleResult>('/api/ps/depurple', {
        method: 'POST',
        body: { upload_id: upload.upload_id, strength },
      });
      setResult(data);
    } catch (err) {
      const message = errorText(err);
      setError(message);
      toastError(message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ps-page">
      <header className="console-head">
        <div>
          <h1>PS 助手 · 去紫</h1>
          <p>把偏紫的定妆图拉回中性：只改色偏，不动构图与尺寸。</p>
        </div>
      </header>

      <section className="ps-controls panel">
        <div
          className={`ps-drop${dragging ? ' is-drag' : ''}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            const dropped = event.dataTransfer.files?.[0];
            if (dropped) pick(dropped);
          }}
        >
          <label className="ps-pick">
            <input
              type="file"
              accept=".jpg,.jpeg,.png,.webp,.bmp"
              onChange={(event) => pick(event.target.files?.[0] || null)}
            />
            <span>{file ? file.name : '点击选择，或把图片拖到这里'}</span>
          </label>
          <p className="tag-hint">支持 JPG / PNG / WebP / BMP，单张 ≤ {MAX_FILE_MB}MB；长边超过 3000px 建议先缩小。</p>
        </div>

        <label className="ps-strength">
          <span>去紫强度</span>
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={strength}
            onChange={(event) => setStrength(Number(event.target.value))}
          />
          <output>{strength}</output>
        </label>

        <div className="ps-actions">
          <button className="primary" onClick={() => void run()} disabled={busy || !file}>
            {busy ? '处理中…' : '开始去紫'}
          </button>
          {result && (
            <a className="quiet btn-sm" href={apiUrl(result.download_url)}>下载 PNG</a>
          )}
        </div>

        {error && <p className="error-banner">{error}</p>}
      </section>

      {(previewUrl || result) && (
        <section className="ps-compare">
          <figure className="ps-pane">
            <figcaption>原图</figcaption>
            {previewUrl ? <img src={previewUrl} alt="原图" /> : <p className="tag-empty">没有预览</p>}
          </figure>
          <figure className="ps-pane">
            <figcaption>
              去紫后{result ? `（强度 ${result.strength}）` : ''}
              {result?.cached ? <span className="tag-hint"> · 命中缓存</span> : null}
            </figcaption>
            {result ? <img src={apiUrl(result.url)} alt="去紫结果" /> : <p className="tag-empty">点「开始去紫」生成</p>}
          </figure>
        </section>
      )}

      {result && (
        <section className="panel ps-stats">
          <div className="panel-head"><h3>统计</h3></div>
          <dl className="admin-kv">
            <div><dt>尺寸</dt><dd>{result.width} × {result.height}</dd></div>
            <div><dt>紫色色偏（前 → 后）</dt><dd>{fmt(result.cast_before)} → {fmt(result.cast_after)}</dd></div>
            <div><dt>中性锚点</dt><dd>{result.anchors ?? '—'}</dd></div>
            <div><dt>耗时</dt><dd>{result.elapsed_ms == null ? '—' : `${result.elapsed_ms} ms`}</dd></div>
          </dl>
          <p className="tag-hint">色偏数值越小越中性；0 表示图片里的中性带几乎不带紫。</p>
        </section>
      )}
    </div>
  );
}

function fmt(value: number | null | undefined): string {
  if (value == null) return '—';
  return value.toFixed(2);
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取图片失败，请重试。'));
    reader.readAsDataURL(file);
  });
}
