/**
 * PS 助手 · 去紫
 *
 * 修的是「AI 多轮生产把同一件白料越做越紫/发品红」：服务端在 Lab 空间只扣 A/B、不动 L，
 * 这里只负责选图、调强度、并排看前后差异。
 *
 * 上传复用 image-slots.js 的 uploadImages（单槽位）：20MB/格式校验、dataURL、上传进度都已在那边测过。
 * 像素上限与服务端 app/depurple.py 的 MAX_PS_PIXELS 保持一致（12MP），在选图时就拦，
 * 省得传完 20MB 再被 413。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const MAX_PIXELS = 12000000;

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

  function setDropTitle(text) { const el = $('ps-drop-title'); if (el) el.textContent = text; }

  /** 浏览器解码一次原图拿宽高；按像素总数判断即可（不解析 EXIF）。 */
  function readPixelSize(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        const size = { width: img.naturalWidth, height: img.naturalHeight };
        URL.revokeObjectURL(url);
        resolve(size);
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('这张图片打不开，换一张试试。')); };
      img.src = url;
    });
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
    ['ps-compare', 'ps-stats', 'ps-stale'].forEach((id) => { const el = $(id); if (el) el.classList.add('hidden'); });
    const meta = $('ps-after-meta');
    if (meta) meta.textContent = '';
    const beforeMeta = $('ps-before-meta');
    if (beforeMeta) beforeMeta.textContent = '';
    setDownload('');
    showError('');
    const run = $('ps-run');
    if (run) run.disabled = true;
    if (!keepTitle) setDropTitle('点击或拖入一张图片');
  }

  /** 选图 → 校验 → 立即上传（点「去紫」只剩一次处理请求）。 */
  async function pickFile(file) {
    if (!file || state.busy) return;
    showError('');
    const problem = window.ImageSlots ? window.ImageSlots.validateImageFile(file) : '';
    if (problem) { showError(problem); return; }
    let size;
    try {
      size = await readPixelSize(file);
    } catch (err) {
      showError(err.message || String(err));
      return;
    }
    const pixels = size.width * size.height;
    if (pixels > MAX_PIXELS) {
      showError(`这张图 ${size.width}×${size.height}（${(pixels / 1e6).toFixed(1)} 百万像素）超过 `
        + `${MAX_PIXELS / 1e6} 百万像素上限；请先把长边缩到 2000 像素左右再处理。`);
      return;
    }

    reset(true);
    state.file = file;
    state.busy = true;
    const drop = $('ps-drop');
    if (drop) drop.classList.add('busy');
    setDropTitle('正在上传…');
    try {
      // 单槽位复用 uploadImages：格式/大小校验、dataURL、进度都在这个函数里
      const slots = [{ file, name: file.name }];
      const result = await window.ImageSlots.uploadImages(slots, { concurrency: 1 });
      if (!result.ok) throw new Error((result.failures[0] || {}).message || '上传失败，请重试。');
      state.uploadId = slots[0].uploadId;
      const before = $('ps-before');
      if (before) before.src = `/api/uploads/${state.uploadId}`;
      const beforeMeta = $('ps-before-meta');
      if (beforeMeta) beforeMeta.textContent = `${size.width}×${size.height} · ${(file.size / 1048576).toFixed(1)}MB`;
      const compare = $('ps-compare');
      if (compare) compare.hidden = false;
      setDropTitle(file.name);
      const run = $('ps-run');
      if (run) run.disabled = false;
    } catch (err) {
      showError(err.message || String(err));
      reset();
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
    const input = $('ps-file');
    if (input) input.addEventListener('change', () => { if (input.files && input.files[0]) pickFile(input.files[0]); });

    const drop = $('ps-drop');
    if (drop) {
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

    const slider = $('ps-strength');
    const out = $('ps-strength-out');
    if (slider) {
      state.strength = Number(slider.value) || 100;
      slider.addEventListener('input', () => {
        state.strength = Number(slider.value) || 0;
        if (out) out.textContent = String(state.strength);
        // 强度变了，屏幕上那张结果就不再对应当前参数：置灰并要求重算，别让人拿着旧图下载
        if (state.result) markStale(state.strength !== Number(state.result.strength));
      });
    }

    const button = $('ps-run');
    if (button) button.addEventListener('click', run);
    const clear = $('ps-reset');
    if (clear) clear.addEventListener('click', () => reset());

    reset();
  }

  window.PsUI = {
    enter() { bind(); },
    reset,
  };
})();
