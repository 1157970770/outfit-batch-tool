/*
 * 任务图片槽位组件（小批量页 / 大批量页共用，无框架）
 *
 * 槽位 1 = 定妆图（必填），槽位 2–10 = 细节图；删除后后面的图自动左移，
 * 保证界面上写的「图N」永远等于发给模型的 imageUrls[N-1]。
 * 提交时才上传：uploadImages() 用 XHR 并发 3 上传，格内进度由 renderSlots 显示。
 */
(() => {
  'use strict';

  const DEFAULT_MAX = 10;
  const MAX_FILE_MB = 20;
  const TYPE_RE = /\.(jpe?g|png|webp|bmp)$/i;
  let maxSlots = DEFAULT_MAX;

  function configure(options) {
    const max = Number(options && options.max);
    if (Number.isFinite(max) && max > 0) maxSlots = Math.min(DEFAULT_MAX, Math.trunc(max));
  }

  function createSlotState() { return []; }

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('无法读取图片，请重新选择。'));
      reader.readAsDataURL(file);
    });
  }

  function validateImageFile(file) {
    if (!file) return '没有读到图片。';
    // HEIC 单独给一条能照着做的提示：iPhone 直出的照片默认就是它，服务端也无法解码。
    if (/\.hei[cf]$/i.test(file.name) || /heic|heif/i.test(file.type || '')) {
      return `${file.name}：iPhone 的 HEIC 格式不能直接上传。请在「照片」里分享→存储为 JPG，`
        + '或到 设置→相机→格式 选「兼容性最佳」后重拍/导出，再重新选择。';
    }
    if (!TYPE_RE.test(file.name)) {
      return `${file.name}：格式不支持，只能上传 JPG / PNG / WebP / BMP。`;
    }
    if (file.size > MAX_FILE_MB * 1024 * 1024) {
      return `${file.name}：${(file.size / 1048576).toFixed(1)}MB，超过单张 ${MAX_FILE_MB}MB 上限，请先压缩。`;
    }
    return '';
  }

  function entryFromFile(file) {
    return {
      file, name: file.name, sizeMb: file.size / 1048576,
      uploadId: '', status: 'idle', progress: 0, error: '',
      previewUrl: URL.createObjectURL(file), url: '',
    };
  }

  /** 追加（或替换）文件；返回 {added, errors, overflow}。 */
  function appendFiles(slots, files, options = {}) {
    const picked = Array.from(files || []);
    const replaceIndex = Number(options.replaceIndex) || 0;
    const errors = [];
    const accepted = [];
    for (const file of picked) {
      const problem = validateImageFile(file);
      if (problem) { errors.push(problem); continue; }
      accepted.push(entryFromFile(file));
    }
    if (replaceIndex > 0 && accepted.length) {
      if (replaceIndex > slots.length) return { added: 0, errors: ['要替换的槽位不存在。'], overflow: 0 };
      const old = slots[replaceIndex - 1];
      if (old && old.previewUrl) URL.revokeObjectURL(old.previewUrl);
      slots.splice(replaceIndex - 1, 1, accepted.shift());
    }
    const room = Math.max(0, maxSlots - slots.length);
    const taking = accepted.slice(0, room);
    for (const item of taking) slots.push(item);
    const overflow = accepted.length - taking.length;
    for (const item of accepted.slice(room)) {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    }
    if (overflow) errors.push(`最多 ${maxSlots} 张（1 张定妆图 + ${maxSlots - 1} 张细节图），多出的 ${overflow} 张没有加入。`);
    return { added: taking.length + (replaceIndex > 0 ? 1 : 0), errors, overflow };
  }

  function resetSlots(slots) {
    if (!Array.isArray(slots)) return;
    for (const item of slots) {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    }
    slots.length = 0;
  }

  function uploadIds(slots) {
    return (slots || []).map((item) => item.uploadId || '').filter(Boolean);
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function renderCell(slots, index, opts) {
    const item = slots[index - 1];
    const primary = index === 1;
    // 空槽用 <label for=fileInput>：点它由浏览器原生触发文件选择，
    // 不依赖 JS 的 .click()（某些浏览器在隐藏/pointer-events:none 的 input 上会吞掉）。
    const useLabel = !item && !opts.readonly && !!opts.inputId;
    const tag = item ? 'div' : (useLabel ? 'label' : 'button');
    const cell = el(tag, `slot-cell ${item ? 'is-filled' : 'is-empty'}${primary ? ' is-primary' : ''}`);
    if (!item) {
      if (useLabel) {
        cell.htmlFor = opts.inputId;        // 原生打开文件选择器
        cell.setAttribute('role', 'button');
      } else {
        cell.type = 'button';
        cell.dataset.pick = String(index);  // 只读态不会走到这里；保留兼容 JS 打开
      }
      cell.append(el('span', 'slot-add', '+'),
        el('span', 'slot-caption', primary ? '定妆图（必填）' : `图${index} · 细节`));
      cell.title = primary ? '点击选择定妆图' : `点击添加第 ${index} 张图（图${index}）`;
    } else {
      const src = item.url || item.previewUrl;
      if (src) {
        const img = el('img');
        img.src = src;
        img.alt = item.name || `图${index}`;
        img.loading = 'lazy';
        cell.append(img);
      }
      cell.append(el('span', `slot-badge${primary ? ' is-primary' : ''}`, primary ? '定妆 · 图1' : `图${index}`));
      if (item.status === 'uploading') {
        cell.classList.add('is-uploading');
        const bar = el('div', 'slot-progress');
        const fill = el('span');
        fill.style.width = `${Math.max(0, Math.min(100, Number(item.progress) || 0))}%`;
        bar.append(fill);
        cell.append(bar);
      }
      if (item.status === 'error') cell.classList.add('is-error');
      if (item.status === 'error' && item.error) cell.append(el('span', 'slot-caption', item.error));
      else if (!opts.compact) cell.append(el('span', 'slot-caption', item.name || `图${index}`));
      if (!opts.readonly) {
        const del = el('button', 'slot-del', '×');
        del.type = 'button';
        del.title = primary ? '定妆图不能删除' : `删除图${index}（后面的图会前移）`;
        del.dataset.remove = primary ? '' : String(index);
        if (primary) del.disabled = true;
        cell.append(del);
      }
      cell.dataset.pick = String(index);       // 点已填格子 = 替换这一张
    }
    return cell;
  }

  /** 重建槽位 DOM。opts: {compact, readonly, onPick, onRemove} */
  function renderSlots(root, slots, opts = {}) {
    if (!root) return;
    root.replaceChildren();
    root.classList.toggle('slot-grid-compact', !!opts.compact);
    root.classList.toggle('is-frozen', !!opts.readonly);
    const primary = el('div', 'slot-grid-primary');
    primary.append(renderCell(slots, 1, opts));
    const detail = el('div', 'slot-grid-detail');
    for (let index = 2; index <= maxSlots; index += 1) detail.append(renderCell(slots, index, opts));
    root.append(primary, detail);
    if (opts.onPick || opts.onRemove) {
      root.onclick = (event) => {
        const del = event.target.closest('.slot-del');
        if (del && del.dataset.remove && opts.onRemove) {
          event.preventDefault();
          opts.onRemove(Number(del.dataset.remove));
          return;
        }
        const pick = event.target.closest('[data-pick]');
        if (pick && opts.onPick && !pick.classList.contains('slot-del')) {
          event.preventDefault();
          opts.onPick(Number(pick.dataset.pick));
        }
      };
    }
    // 不传 handler 时保留已有委托（onChange 只重建 DOM，不覆盖 bindSlotEvents 绑定的事件）。
  }

  /** 绑定 file input / 拖拽 / 点击 / 删除 / 替换。opts: {onChange, onError, compact, readonly} */
  function bindSlotEvents(root, input, slots, opts = {}) {
    if (!root || !input) return;
    const isReadonly = () => (typeof opts.isReadonly === 'function'
      ? !!opts.isReadonly() : !!opts.readonly);
    let replaceIndex = 0;
    const refresh = () => {
      const frozen = isReadonly();
      renderSlots(root, slots, {
        compact: opts.compact, readonly: frozen, inputId: input.id,
        onPick: frozen ? null : (index) => {
          replaceIndex = index;
          input.value = '';
          input.click();
        },
        onRemove: frozen ? null : (index) => {
          const removed = slots.splice(index - 1, 1)[0];
          if (removed && removed.previewUrl) URL.revokeObjectURL(removed.previewUrl);
          refresh();
        },
      });
      if (opts.onChange) opts.onChange();
    };
    const take = (files) => {
      const result = appendFiles(slots, files, { replaceIndex });
      replaceIndex = 0;
      input.value = '';
      refresh();     // 先重渲染（onChange 会写说明文字），再报错，否则错误信息会被覆盖
      if (opts.onError && result.errors.length) opts.onError(result.errors.join(' '));
    };
    input.addEventListener('change', () => take(input.files));
    root.addEventListener('dragover', (event) => {
      if (isReadonly()) return;
      event.preventDefault();
      root.classList.add('is-drop');
    });
    root.addEventListener('dragleave', () => root.classList.remove('is-drop'));
    root.addEventListener('drop', (event) => {
      event.preventDefault();
      root.classList.remove('is-drop');
      if (isReadonly()) return;
      replaceIndex = 0;
      take(event.dataTransfer.files);
    });
    refresh();
    return { refresh };
  }

  /** 单张 XHR 上传（fetch 没有上传进度）。返回 upload_id。 */
  async function uploadOne(item) {
    const dataUrl = await readFile(item.file);
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/uploads', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.withCredentials = true;
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) item.progress = Math.round((event.loaded / event.total) * 100);
      };
      xhr.onerror = () => reject(new Error('网络中断，请重试'));
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) { /* 用下面的兜底文案 */ }
        if (xhr.status >= 200 && xhr.status < 300 && data.upload_id) {
          resolve(data.upload_id);
        } else {
          reject(new Error(data.error || `上传失败（${xhr.status}）`));
        }
      };
      xhr.send(JSON.stringify({ filename: item.name, data: dataUrl }));
    });
  }

  /** 提交时并发上传（默认 3）。已完成的不重传；单张失败不影响其他在飞的。 */
  async function uploadImages(slots, options = {}) {
    const concurrency = Math.max(1, Number(options.concurrency) || 3);
    const pending = [];
    slots.forEach((item, index) => {
      if (item.file && !item.uploadId) pending.push({ item, index: index + 1 });
    });
    const total = pending.length;
    const failures = [];
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
          job.item.error = String(err.message || err);
          failures.push({ index: job.index, name: job.item.name, message: job.item.error });
        }
        done += 1;
        if (options.onProgress) options.onProgress({ index: job.index, done, total, failures: failures.length });
      }
    };
    const workers = Array.from({ length: Math.min(concurrency, Math.max(1, total)) }, worker);
    // XHR 的 progress 事件改的是内存里的百分比，格内进度条要有人定期重画才看得见。
    const ticker = total && options.onProgress
      ? setInterval(() => options.onProgress({ index: 0, done, total, failures: failures.length, tick: true }), 250)
      : null;
    try {
      await Promise.all(workers);
    } finally {
      if (ticker) clearInterval(ticker);
    }
    return { ok: failures.length === 0, failures, uploadIds: uploadIds(slots) };
  }

  window.ImageSlots = {
    SLOT_MAX: DEFAULT_MAX,
    configure, createSlotState, readFile, validateImageFile, appendFiles,
    renderSlots, bindSlotEvents, resetSlots, uploadImages, uploadIds,
    get maxSlots() { return maxSlots; },
  };
})();
