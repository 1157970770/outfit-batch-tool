/**
 * PS 助手 · 去紫
 *
 * 修的是「AI 多轮生产把同一件白料越做越紫/发品红」：服务端在 Lab 空间只扣 A/B、不动 L，
 * 这里只负责选图、调强度、并排看前后差异。
 *
 * 选图用全站同一个约定：`.drop-zone input` 是一条 opacity:0、铺满整个虚线框的原生 input，
 * 点框=点 input，浏览器自己弹选择框，不靠 JS 的 input.click()（个别国产浏览器会拦）。
 * 上传也在本文件里自带实现（FileReader → dataURL → XHR POST /api/uploads），
 * 只借用 ImageSlots.validateImageFile 做一次格式/大小预检，不依赖它的内部状态。
 *
 * 不设像素上限（服务端也不设）：大图只是慢、吃内存，算不动时服务端会回一条「内存不足，请先缩图」。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const MAX_BYTES = 20 * 1024 * 1024;
  const TYPE_RE = /\.(jpe?g|png|webp|bmp)$/i;
  const MAX_UPLOAD_WARN_PIXELS = 20000000;   // 只提醒，不拦

  const state = { file: null, uploadId: '', strength: 100, result: null, busy: false };
  let ready = false;

  function api(path, options) {
    if (!window.OutfitApp || !window.OutfitApp.api) throw new Error('页面脚本还没加载完，刷新一下再试。');
    return window.OutfitApp.api(path, options);
  }

  function showError(message) {
    const el = $('ps-error');
    if (!el) return;
    el.textContent = message || '';
    el.classList.toggle('hidden', !message);
  }

  function setStatus(text) {
    const el = $('ps-drop-title');
    if (el) el.textContent = text;
  }

  function fmtMB(bytes) {
    return `${(Number(bytes || 0) / 1048576).toFixed(1)}MB`;
  }

  /** 格式/大小预检：优先用全站同一套文案，缺 ImageSlots 时用本文件的等价规则。 */
  function checkFile(file) {
    if (window.ImageSlots && typeof window.ImageSlots.validateImageFile === 'function') {
      const problem = window.ImageSlots.validateImageFile(file);
      if (problem) return problem;
    } else if (!TYPE_RE.test(file.name || '')) {
      return `${file.name}：格式不支持，只能上传 JPG / PNG / WebP / BMP。`;
    }
    if (file.size > MAX_BYTES) {
      return `${file.name}：${fmtMB(file.size)}，超过单张 20MB 上限，请先压缩。`;
    }
    return '';
  }

  /**
   * 浏览器解码一次原图拿宽高（不解析 EXIF）；解不开返回 null。
   * 只用来提示尺寸/大图提醒，不在这里拦死：服务端 PIL 支持的格式比浏览器多。
   */
  function readPixelSize(file) {
    return new Promise((resolve) => {
      let url = '';
      try { url = URL.createObjectURL(file); } catch (_) { resolve(null); return; }
      const img = new Image();
      const done = (value) => { try { URL.revokeObjectURL(url); } catch (_) {} resolve(value); };
      img.onload = () => done({ width: img.naturalWidth, height: img.naturalHeight });
      img.onerror = () => done(null);
      img.src = url;
    });
  }

  function readDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(new Error('读不到这个文件（可能已被移动或没有权限）。'));
      reader.readAsDataURL(file);
    });
  }

  /** 自带的上传：XHR 才有上传进度。返回 upload_id。 */
  function uploadFile(file, onProgress) {
    return readDataUrl(file).then((dataUrl) => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/uploads', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.withCredentials = true;
      xhr.timeout = 300000;
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable || !onProgress) return;
        onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
      };
      xhr.onerror = () => reject(new Error('网络中断：上传没发到主机，检查连接后重试。'));
      xhr.ontimeout = () => reject(new Error('上传超时：图片太大或网络太慢，可以先用小图试试。'));
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) { /* 用下面的兜底文案 */ }
        if (xhr.status >= 200 && xhr.status < 300 && data.upload_id) resolve(data.upload_id);
        else reject(new Error(data.error || `上传失败（HTTP ${xhr.status}）`));
      };
      xhr.send(JSON.stringify({ filename: file.name, data: dataUrl }));
    }));
  }

  /** 服务端版本低于本功能要求的 0.18.0 时直接说清楚，别让人对着 404 猜。 */
  const MIN_SERVER_VERSION = '0.18.0';
  let versionChecked = false;

  function versionBefore(left, right) {
    const a = String(left || '').split('.').map((n) => parseInt(n, 10) || 0);
    const b = String(right || '').split('.').map((n) => parseInt(n, 10) || 0);
    for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
      const x = a[i] || 0;
      const y = b[i] || 0;
      if (x !== y) return x < y;
    }
    return false;
  }

  async function checkServerVersion() {
    if (versionChecked) return;
    versionChecked = true;
    try {
      const response = await fetch('/healthz', { credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json();
      if (data && data.version && versionBefore(data.version, MIN_SERVER_VERSION)) {
        showError(`网页服务还是旧版本 ${data.version}，没有 PS 接口；请重启设计区网页服务（刷新后左下方版本号应为 ${MIN_SERVER_VERSION}）再试。`);
      }
    } catch (_) { /* 拿不到 /healthz 就算了，真出错时处理请求还会报 */ }
  }

  function setDownload(url) {
    const link = $('ps-download');
    if (!link) return;
    if (url) {
      link.href = url;
      link.classList.remove('disabled');
      link.removeAttribute('aria-disabled');
    } else {
      link.removeAttribute('href');
      link.classList.add('disabled');
      link.setAttribute('aria-disabled', 'true');
    }
  }

  function renderStats(data) {
    const el = $('ps-stats');
    if (!el) return;
    const before = Number(data.cast_before) || 0;
    const after = Number(data.cast_after) || 0;
    const cut = before > 0.01 ? Math.max(0, Math.round((1 - after / before) * 100)) : 0;
    const anchors = data.anchors || {};
    const fmt = (pair) => Array.isArray(pair) ? `${pair[0]} / ${pair[1]}` : '—';
    el.replaceChildren();
    const add = (text, cls, tag) => {
      const node = document.createElement(tag || 'span');
      node.textContent = text;
      if (cls) node.className = cls;
      el.append(node);
    };
    add('色偏 ');
    add(before.toFixed(2), 'mono');
    add(' → ');
    add(after.toFixed(2), 'better mono', 'strong');
    add(`（扣掉 ${cut}%）`);
    add(` · 耗时 ${Number(data.elapsed_ms || 0) / 1000}s`, '');
    add(` · 锚点 暗部 A/B ${fmt(anchors.dark)}、背景 A/B ${fmt(anchors.bg)}`);
    el.classList.remove('hidden');
  }

  function markStale(stale) {
    const box = $('ps-stale');
    if (box) box.classList.toggle('hidden', !stale);
    if (stale) setDownload('');
    else if (state.result) setDownload(state.result.download_url);
  }

  function reset(keepTitle) {
    state.file = null;
    state.uploadId = '';
    state.result = null;
    state.busy = false;
    const input = $('ps-file');
    if (input) input.value = '';
    const before = $('ps-before');
    const after = $('ps-after');
    if (before) before.removeAttribute('src');
    if (after) after.removeAttribute('src');
    // .ps-compare 的 CSS 是 display:grid，会盖掉 [hidden] 的默认 display:none，
    // 所以 ps.css 里补了 .ps-compare[hidden]{display:none!important}，这里照旧用属性表达意图。
    const compareBox = $('ps-compare');
    if (compareBox) compareBox.hidden = true;
    ['ps-stats', 'ps-stale'].forEach((id) => { const el = $(id); if (el) el.classList.add('hidden'); });
    const meta = $('ps-after-meta');
    if (meta) meta.textContent = '';
    const beforeMeta = $('ps-before-meta');
    if (beforeMeta) beforeMeta.textContent = '';
    setDownload('');
    showError('');
    const run = $('ps-run');
    if (run) run.disabled = true;
    if (!keepTitle) setStatus('点击或拖入一张图片');
  }

  /**
   * 选图 → 预检 → 上传（点「去紫」只剩一次处理请求）。
   * 每个阶段都写一句人话到虚线框标题里，这样卡住时用户能直接看到停在哪一步。
   */
  async function pickFile(file) {
    if (!file) return;
    if (state.busy) { setStatus('上一张还在处理，稍等一下…'); return; }
    showError('');
    const problem = checkFile(file);
    if (problem) {
      showError(problem);
      setStatus('点击或拖入一张图片');
      return;
    }
    const name = file.name || '这张图';
    reset(true);
    state.file = file;
    state.busy = true;
    const drop = $('ps-drop');
    if (drop) drop.classList.add('busy');
    setStatus(`正在上传 ${name}… 0%`);
    try {
      const size = await readPixelSize(file);
      if (size && size.width * size.height > MAX_UPLOAD_WARN_PIXELS) {
        // 只提醒：20MP 以上服务端要约 2GB 内存、跑十来秒
        console.warn('[PS助手] 大图：', `${size.width}×${size.height}`, `${(size.width * size.height / 1e6).toFixed(1)}MP`);
      }
      const uploadId = await uploadFile(file, (pct) => setStatus(`正在上传 ${name}… ${pct}%`));
      state.uploadId = uploadId;
      const before = $('ps-before');
      if (before) before.src = `/api/uploads/${uploadId}`;
      const beforeMeta = $('ps-before-meta');
      if (beforeMeta) {
        const dims = size ? `${size.width}×${size.height} · ` : '';
        beforeMeta.textContent = `${dims}${fmtMB(file.size)}`;
      }
      const compare = $('ps-compare');
      if (compare) compare.hidden = false;
      setStatus(`${name} · 已上传`);
      const run = $('ps-run');
      if (run) run.disabled = false;
    } catch (err) {
      console.error('[PS助手] 上传失败：', err);
      reset();
      showError(err && err.message ? err.message : String(err));
      setStatus('上传失败，点击重试');
    } finally {
      state.busy = false;
      if (drop) drop.classList.remove('busy');
    }
  }

  async function run() {
    if (state.busy || !state.uploadId) return;
    state.busy = true;
    const button = $('ps-run');
    if (button) { button.disabled = true; button.textContent = '处理中…'; }
    showError('');
    try {
      const data = await api('/api/ps/depurple', {
        method: 'POST',
        body: JSON.stringify({ upload_id: state.uploadId, strength: state.strength }),
      });
      state.result = data;
      const after = $('ps-after');
      if (after) after.src = data.url;           // 服务端已带 ?v= 时间戳，同强度重算也能刷新
      const afterMeta = $('ps-after-meta');
      if (afterMeta) afterMeta.textContent = `${data.width}×${data.height}${data.cached ? ' · 命中缓存' : ''}`;
      const compare = $('ps-compare');
      if (compare) compare.hidden = false;
      renderStats(data);
      markStale(false);
      if (window.OutfitApp && window.OutfitApp.toast) window.OutfitApp.toast(`去紫完成，用时 ${(Number(data.elapsed_ms || 0) / 1000).toFixed(1)}s`);
    } catch (err) {
      showError(err.message || String(err));
    } finally {
      state.busy = false;
      if (button) { button.disabled = !state.uploadId; button.textContent = '去紫'; }
    }
  }

  function bind() {
    if (ready) return;
    ready = true;

    // 点框 = 点那条铺满整个虚线框的透明 input（.drop-zone input 的既有约定），
    // 浏览器自己弹选择框；没有 input 时才退回 JS 触发。
    const input = $('ps-file');
    if (input) {
      input.addEventListener('change', () => {
        const file = input.files && input.files[0];
        if (file) pickFile(file);
      });
      const drop = $('ps-drop');
      if (drop) {
        drop.addEventListener('click', (event) => {
          if (event.target === input) return;   // 点在 input 上，交给浏览器原生行为
          event.preventDefault();
          input.click();
        });
        drop.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          input.click();
        });
        ['dragenter', 'dragover'].forEach((type) => drop.addEventListener(type, (event) => {
          event.preventDefault();
          drop.classList.add('dragging');
        }));
        ['dragleave', 'drop'].forEach((type) => drop.addEventListener(type, () => drop.classList.remove('dragging')));
        drop.addEventListener('drop', (event) => {
          event.preventDefault();
          const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
          if (file) pickFile(file);
        });
      }
    }

    const before = $('ps-before');
    if (before) before.addEventListener('error', () => {
      const meta = $('ps-before-meta');
      if (meta && before.getAttribute('src')) meta.textContent = '浏览器显示不了这张图，服务端仍会尝试处理';
    });

    const slider = $('ps-strength');
    const out = $('ps-strength-out');
    if (slider) {
      state.strength = Number(slider.value) || 100;
      if (out) out.textContent = String(state.strength);
      slider.addEventListener('input', () => {
        state.strength = Number(slider.value) || 0;
        if (out) out.textContent = String(state.strength);
        // 强度变了，屏幕上那张结果就不再对应当前参数：置灰并要求重算，别让人拿着旧图下载
        if (state.result) markStale(state.strength !== Number(state.result.strength));
      });
    }

    const button = $('ps-run');
    if (button) button.addEventListener('click', () => {
      run().catch((err) => showError(err && err.message ? err.message : String(err)));
    });
    const clear = $('ps-reset');
    if (clear) clear.addEventListener('click', () => reset());

    reset();
    checkServerVersion();
  }

  window.PsUI = {
    enter() { bind(); },
    reset,
  };
})();
