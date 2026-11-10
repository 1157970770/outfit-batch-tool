/**
 * 图片槽位（React 版）：槽位 1 = 定妆图（必填），2–10 = 细节图。
 *
 * 交互与原生 `image-slots.js` 逐条对齐，**这是用户明确要求保留的 insert 方式**：
 * - 点空槽 → 选图；点已填槽右上角 × → 删除，后面的图自动左移；
 * - 拖拽/多选文件 → 从第一个空槽开始依次填入，超出上限的部分丢弃并提示；
 * - 「图N」永远等于 `upload_ids[N-1]`，因为提交前不做任何重排；
 * - 提交时才上传：3 路并发 XHR（fetch 拿不到上传进度），250ms ticker 让格内进度条动起来；
 * - 单张失败只标红该格，不阻塞其它在飞的图。
 */
import { useCallback, useRef, useState } from 'react';

export const SLOT_MAX = 10;
const MAX_FILE_MB = 20;
const TYPE_RE = /\.(jpe?g|png|webp|bmp)$/i;
const UPLOAD_CONCURRENCY = 3;

export type SlotStatus = 'idle' | 'uploading' | 'done' | 'error';

export interface SlotItem {
  key: string;
  file: File;
  name: string;
  sizeMb: number;
  uploadId: string;
  status: SlotStatus;
  progress: number;
  error: string;
  previewUrl: string;
}

/** 与原生版同文案：HEIC 单独给能照着做的提示，iPhone 直出照片默认就是它。 */
export function validateImageFile(file: File): string {
  if (!file) return '没有读到图片。';
  if (/\.hei[cf]$/i.test(file.name) || /heic|heif/i.test(file.type || '')) {
    return `${file.name}：iPhone 的 HEIC 格式不能直接上传。请在「照片」里分享→存储为 JPG，或到 设置→相机→格式 选「兼容性最佳」后重拍/导出，再重新选择。`;
  }
  if (!TYPE_RE.test(file.name)) return `${file.name}：格式不支持，只能上传 JPG / PNG / WebP / BMP。`;
  if (file.size > MAX_FILE_MB * 1024 * 1024) {
    return `${file.name}：${(file.size / 1048576).toFixed(1)}MB，超过单张 ${MAX_FILE_MB}MB 上限，请先压缩。`;
  }
  return '';
}

function entryFromFile(file: File): SlotItem {
  return {
    key: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`,
    file,
    name: file.name,
    sizeMb: file.size / 1048576,
    uploadId: '',
    status: 'idle',
    progress: 0,
    error: '',
    previewUrl: URL.createObjectURL(file),
  };
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('无法读取图片，请重新选择。'));
    reader.readAsDataURL(file);
  });
}

export interface AppendResult {
  added: number;
  errors: string[];
}

export interface UseImageSlots {
  slots: SlotItem[];
  busy: boolean;
  appendFiles: (files: FileList | File[] | null, replaceIndex?: number) => AppendResult;
  remove: (index: number) => void;
  reset: () => void;
  ids: () => string[];
  uploadAll: (onProgress?: (done: number, total: number) => void) => Promise<{
    ok: boolean;
    failures: Array<{ index: number; name: string; message: string }>;
    uploadIds: string[];
  }>;
  primary: SlotItem | undefined;
}

export function useImageSlots(): UseImageSlots {
  const [slots, setSlots] = useState<SlotItem[]>([]);
  const [busy, setBusy] = useState(false);
  // 上传过程中直接改对象字段（进度），再靠 tick 触发一次重渲染；ref 与 state 同步替换。
  const slotsRef = useRef<SlotItem[]>([]);
  slotsRef.current = slots;

  const commit = useCallback((next: SlotItem[]) => {
    slotsRef.current = next;
    setSlots(next);
  }, []);

  const appendFiles = useCallback(
    (files: FileList | File[] | null, replaceIndex = 0): AppendResult => {
      const picked = Array.from(files || []);
      const errors: string[] = [];
      const accepted: SlotItem[] = [];
      for (const file of picked) {
        const problem = validateImageFile(file);
        if (problem) {
          errors.push(problem);
          continue;
        }
        accepted.push(entryFromFile(file));
      }
      const next = [...slotsRef.current];
      if (replaceIndex > 0 && accepted.length) {
        if (replaceIndex > next.length) return { added: 0, errors: ['要替换的槽位不存在。'] };
        const old = next[replaceIndex - 1];
        if (old?.previewUrl) URL.revokeObjectURL(old.previewUrl);
        next.splice(replaceIndex - 1, 1, accepted.shift() as SlotItem);
      }
      const room = Math.max(0, SLOT_MAX - next.length);
      const taking = accepted.slice(0, room);
      next.push(...taking);
      for (const item of accepted.slice(room)) URL.revokeObjectURL(item.previewUrl);
      if (accepted.length > taking.length) {
        errors.push(`最多 ${SLOT_MAX} 张（1 张定妆图 + ${SLOT_MAX - 1} 张细节图），多出的 ${accepted.length - taking.length} 张没有加入。`);
      }
      commit(next);
      return { added: taking.length + (replaceIndex > 0 ? 1 : 0), errors };
    },
    [commit],
  );

  const remove = useCallback(
    (index: number) => {
      const next = [...slotsRef.current];
      const [removed] = next.splice(index - 1, 1);
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      commit(next);
    },
    [commit],
  );

  const reset = useCallback(() => {
    for (const item of slotsRef.current) if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    commit([]);
  }, [commit]);

  const ids = useCallback(() => slotsRef.current.map((item) => item.uploadId).filter(Boolean), []);

  const uploadAll = useCallback<UseImageSlots['uploadAll']>(async (onProgress) => {
    const list = slotsRef.current;
    const pending = list.map((item, index) => ({ item, index: index + 1 })).filter((entry) => entry.item.file && !entry.item.uploadId);
    const total = pending.length;
    const failures: Array<{ index: number; name: string; message: string }> = [];
    let cursor = 0;
    let done = 0;

    const worker = async () => {
      while (cursor < total) {
        const job = pending[cursor];
        cursor += 1;
        job.item.status = 'uploading';
        job.item.progress = 0;
        job.item.error = '';
        try {
          job.item.uploadId = await uploadOne(job.item);
          job.item.status = 'done';
          job.item.progress = 100;
        } catch (err) {
          job.item.status = 'error';
          job.item.error = String((err as Error).message || err);
          failures.push({ index: job.index, name: job.item.name, message: job.item.error });
        }
        done += 1;
        onProgress?.(done, total);
        commit([...slotsRef.current]);
      }
    };

    if (!total) return { ok: true, failures, uploadIds: ids() };

    setBusy(true);
    // 进度百分比写的是内存对象，必须有人定期重画，否则格内进度条一动不动。
    const ticker = window.setInterval(() => commit([...slotsRef.current]), 250);
    try {
      await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, total) }, worker));
    } finally {
      window.clearInterval(ticker);
      setBusy(false);
      commit([...slotsRef.current]);
    }
    return { ok: failures.length === 0, failures, uploadIds: ids() };
  }, [commit, ids]);

  return { slots, busy, appendFiles, remove, reset, ids, uploadAll, primary: slots[0] };
}

/** 单张 XHR 上传：fetch 没有上传进度，这里必须是 XHR。 */
function uploadOne(item: SlotItem): Promise<string> {
  return readAsDataUrl(item.file).then(
    (dataUrl) =>
      new Promise<string>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/uploads', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.withCredentials = true;
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) item.progress = Math.round((event.loaded / event.total) * 100);
        };
        xhr.onerror = () => reject(new Error('网络中断，请重试'));
        xhr.onload = () => {
          let data: { upload_id?: string; error?: string } = {};
          try {
            data = JSON.parse(xhr.responseText || '{}');
          } catch {
            // 用下面的兜底文案
          }
          if (xhr.status >= 200 && xhr.status < 300 && data.upload_id) resolve(data.upload_id);
          else reject(new Error(data.error || `上传失败（${xhr.status}）`));
        };
        xhr.send(JSON.stringify({ filename: item.name, data: dataUrl }));
      }),
  );
}
