/* ============================================================
   生图 tab（老张 API · GPT Image VIP）
   ------------------------------------------------------------
   · 选项全部来自后端 /api/bootstrap 的 service.imagegen，
     前端不重复实现「哪个型号支持哪些尺寸/质量」这类规则。
   · 参考图先走 /api/uploads 拿 upload_id，再提交给
     /api/imagegen/tasks；顺序即模型看到的「图1、图2…」。
   · 生成在服务端队列里跑，这里只轮询记录；离开页面不影响出图。
   ============================================================ */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 2500;
  const ACTIVE_STATUSES = ['排队中', '生成中'];
  const SUFFIX_RE = /\.(jpe?g|png|webp|bmp)$/i;
  const MAX_BYTES = 20 * 1024 * 1024;

  const state = {
    bound: false,
    options: null,
    refs: [],        // { upload_id, name, preview }
    tasks: [],
    timer: null,
    uploading: 0,
    submitting: false,
    dragIndex: -1,
  };

  // ------------------------------------------------------------ 小工具

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function api(path, options) {
    if (window.OutfitApp && window.OutfitApp.api) return window.OutfitApp.api(path, options);
    throw new Error('页面脚本未加载完成，请刷新后重试。');
  }

  function toast(text) {
    if (window.OutfitApp && window.OutfitApp.toast) window.OutfitApp.toast(text);
  }

  function currentUser() {
    return (window.OutfitApp && window.OutfitApp.state && window.OutfitApp.state.user) || null;
  }

  function showError(text) {
    const box = $('ig-error');
    if (!text) { box.textContent = ''; box.classList.add('hidden'); return; }
    box.textContent = text;
    box.classList.remove('hidden');
  }

  function fmtTime(seconds) {
    if (!seconds) return '';
    const date = new Date(Number(seconds) * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function fmtElapsed(ms) {
    const total = Math.round(Number(ms) / 1000);
    if (!total) return '';
    if (total < 60) return `${total}s`;
    return `${Math.floor(total / 60)}分${total % 60}秒`;
  }

  function badgeClass(status) {
    if (status === '排队中') return 'is-wait';
    if (status === '生成中') return 'is-run';
    if (status === '已完成') return 'is-ok';
    if (status === '部分完成') return 'is-warn';
    return 'is-bad';
  }

  function readAsDataURL(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(new Error(`读取「${file.name}」失败，请重新选择。`));
      reader.readAsDataURL(file);
    });
  }

  // ------------------------------------------------------------ 选项

  function applyOptions(options) {
    state.options = options || null;
    if (!options) {
      $('ig-hint').textContent = '生图配置读取失败，请刷新页面重试。';
      $('ig-submit').disabled = true;
      return;
    }
    const model = $('ig-model');
    model.replaceChildren();
    (options.models || []).forEach((item) => model.append(new Option(item.label || item.id, item.id)));
    if (options.default_model) model.value = options.default_model;

    syncModelOptions(true);

    const maxCount = Math.max(1, Math.min(8, Number(options.max_count) || 4));
    const count = $('ig-count');
    count.replaceChildren();
    for (let i = 1; i <= maxCount; i += 1) count.append(new Option(`${i} 张`, String(i)));
    count.value = '1';

    const configured = !!options.configured;
    $('ig-submit').disabled = !configured;
    const price = options.price || '0.03';
    $('ig-hint').textContent = configured
      ? `单张 $${price}；一次最多 ${maxCount} 张。提交后在后台排队，可以离开这个页面。`
      : (options.reason || '生图未配置：检查 server/imagegen.json。');
    if (!configured) showError(options.reason || '');
  }

  // 型号变了就重建尺寸/质量选项：不同型号支持的范围不一样，由后端给出。
  function syncModelOptions(first) {
    const options = state.options;
    if (!options) return;
    const modelId = $('ig-model').value;
    const spec = (options.models || []).find((item) => item.id === modelId);
    if (!spec) return;

    const size = $('ig-size');
    const quality = $('ig-quality');
    const prevSize = first ? '' : size.value;
    const prevQuality = first ? '' : quality.value;

    size.replaceChildren();
    (spec.sizes || []).forEach((item) => size.append(new Option(item.label || item.value, item.value)));
    const sizeValues = (spec.sizes || []).map((item) => item.value);
    size.value = sizeValues.includes(prevSize) ? prevSize : (sizeValues[0] || 'auto');

    quality.replaceChildren();
    (spec.qualities || []).forEach((item) => quality.append(new Option(item.label || item.value, item.value)));
    const qualityValues = (spec.qualities || []).map((item) => item.value);
    const fallbackQuality = qualityValues.includes(options.default_quality) ? options.default_quality : qualityValues[0];
    quality.value = qualityValues.includes(prevQuality) ? prevQuality : (fallbackQuality || 'auto');
    quality.disabled = qualityValues.length <= 1;
  }

  async function ensureOptions() {
    if (state.options) return;
    const appState = window.OutfitApp && window.OutfitApp.state;
    const cached = appState && appState.service && appState.service.imagegen;
    if (cached) { applyOptions(cached); return; }
    try {
      const boot = await api('/api/bootstrap');
      applyOptions((boot.service && boot.service.imagegen) || null);
    } catch (err) {
      applyOptions(null);
      showError(err && err.message ? err.message : '生图配置读取失败。');
    }
  }

  // ------------------------------------------------------------ 参考图

  function renderRefs() {
    const host = $('ig-refs');
    host.replaceChildren();
    state.refs.forEach((ref, index) => {
      const box = el('div', 'ig-ref');
      box.draggable = true;
      if (state.dragIndex === index) box.classList.add('is-dragging');

      const img = el('img');
      img.src = ref.preview;
      img.alt = `参考图 ${index + 1}`;
      img.draggable = false;
      box.append(img);
      box.append(el('span', 'ig-ref-badge', String(index + 1)));

      const del = el('button', 'ig-ref-del', '×');
      del.type = 'button';
      del.title = '移除此图';
      del.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        state.refs.splice(index, 1);
        renderRefs();
      });
      box.append(del);
      box.append(el('span', 'ig-ref-name', ref.name || ''));

      box.addEventListener('dragstart', () => { state.dragIndex = index; });
      box.addEventListener('dragend', () => { state.dragIndex = -1; renderRefs(); });
      box.addEventListener('dragover', (event) => { event.preventDefault(); box.classList.add('is-drop-target'); });
      box.addEventListener('dragleave', () => box.classList.remove('is-drop-target'));
      box.addEventListener('drop', (event) => {
        // 必须挡住冒泡：否则会落到下面的上传区，被当成"新增参考图"。
        event.preventDefault();
        event.stopPropagation();
        moveRef(index);
      });

      host.append(box);
    });
    if (state.uploading > 0) host.append(el('span', 'ig-uploading', `上传中…（${state.uploading}）`));
  }

  function moveRef(target) {
    const from = state.dragIndex;
    state.dragIndex = -1;
    if (from < 0 || from === target) { renderRefs(); return; }
    const [item] = state.refs.splice(from, 1);
    state.refs.splice(target, 0, item);
    renderRefs();
  }

  async function addFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    const limit = Math.max(1, Math.min(10, Number(state.options && state.options.max_references) || 10));
    const room = limit - state.refs.length - state.uploading;
    if (room <= 0) { showError(`参考图最多 ${limit} 张，先移除几张再添加。`); return; }
    showError('');
    const accepted = list.slice(0, room);
    if (list.length > room) showError(`参考图最多 ${limit} 张，已忽略多出的 ${list.length - room} 张。`);

    for (const file of accepted) {
      if (!SUFFIX_RE.test(file.name || '')) {
        showError(`「${file.name}」不是支持的图片格式（JPG / PNG / WebP / BMP）。`);
        continue;
      }
      if (file.size > MAX_BYTES) {
        showError(`「${file.name}」超过 20MB，请压缩后再传。`);
        continue;
      }
      state.uploading += 1;
      renderRefs();
      try {
        const dataUrl = await readAsDataURL(file);
        const data = await api('/api/uploads', {
          method: 'POST',
          body: JSON.stringify({ filename: file.name, data: dataUrl }),
        });
        state.refs.push({ upload_id: data.upload_id, name: data.name || file.name, preview: dataUrl });
      } catch (err) {
        showError(err && err.message ? err.message : `「${file.name}」上传失败。`);
      } finally {
        state.uploading -= 1;
        renderRefs();
      }
    }
  }

  // ------------------------------------------------------------ 提交

  async function submit() {
    if (state.submitting) return;
    const prompt = $('ig-prompt').value.trim();
    if (!prompt) { showError('先写提示词再生成。'); $('ig-prompt').focus(); return; }
    if (!state.options || !state.options.configured) {
      showError((state.options && state.options.reason) || '生图未配置：检查 server/imagegen.json。');
      return;
    }
    if (state.uploading > 0) { showError('参考图还在上传，等上传完成再生成。'); return; }

    state.submitting = true;
    const button = $('ig-submit');
    button.disabled = true;
    button.textContent = '提交中…';
    showError('');
    try {
      const data = await api('/api/imagegen/tasks', {
        method: 'POST',
        body: JSON.stringify({
          prompt,
          model: $('ig-model').value,
          size: $('ig-size').value,
          quality: $('ig-quality').value,
          count: Number($('ig-count').value) || 1,
          upload_ids: state.refs.map((ref) => ref.upload_id),
        }),
      });
      state.refs = [];
      renderRefs();
      toast(data.task && data.task.active ? '已提交，正在生成…' : '已提交');
      await refresh();
    } catch (err) {
      showError(err && err.message ? err.message : '提交失败，请重试。');
    } finally {
      state.submitting = false;
      button.textContent = '生成';
      button.disabled = !(state.options && state.options.configured);
    }
  }

  function clearComposer() {
    $('ig-prompt').value = '';
    updateCount();
    state.refs = [];
    renderRefs();
    showError('');
  }

  function updateCount() {
    $('ig-prompt-count').textContent = `${$('ig-prompt').value.length} / 4000`;
  }

  // ------------------------------------------------------------ 记录

  function renderTasks() {
    const host = $('ig-tasks');
    host.replaceChildren();
    const tasks = state.tasks || [];
    $('ig-empty').classList.toggle('hidden', tasks.length > 0);
    const activeCount = tasks.filter((task) => task.active).length;
    $('ig-status').textContent = activeCount
      ? `${activeCount} 个任务进行中，每 ${POLL_MS / 1000} 秒刷新`
      : (tasks.length ? `共 ${tasks.length} 条记录` : '');
    tasks.forEach((task) => host.append(renderTask(task)));
  }

  function renderTask(task) {
    const card = el('article', 'ig-task');
    const head = el('div', 'ig-task-head');
    head.append(el('span', `ig-badge ${badgeClass(task.status)}`, task.status || ''));
    head.append(el('span', 'ig-task-time', fmtTime(task.created_at)));
    const user = currentUser();
    if (user && user.role === 'admin' && task.owner) {
      head.append(el('span', 'ig-task-owner muted', `· ${task.owner}`));
    }
    const del = el('button', 'quiet btn-sm ig-task-del', '删除');
    del.type = 'button';
    del.disabled = !!task.active;
    del.title = task.active ? '生成中不能删除，等它跑完' : '删除这条记录与本地图片';
    del.addEventListener('click', () => removeTask(task));
    head.append(del);
    card.append(head);

    const prompt = el('p', 'ig-task-prompt', task.prompt || '');
    prompt.title = '点击展开 / 收起';
    prompt.addEventListener('click', () => prompt.classList.toggle('is-open'));
    card.append(prompt);

    const bits = [];
    if (task.model_label || task.model) bits.push(task.model_label || task.model);
    if (task.size_label || task.size) bits.push(task.size_label || task.size);
    if (task.quality && task.quality !== 'auto') bits.push(`质量 ${task.quality}`);
    bits.push(`${Number(task.count) || 1} 张`);
    if (Number(task.progress) > 0) bits.push(`已完成 ${task.progress} 张`);
    if (Number(task.elapsed_ms) > 0) bits.push(`耗时 ${fmtElapsed(task.elapsed_ms)}`);
    card.append(el('div', 'ig-task-meta', bits.join(' · ')));

    if (task.error) card.append(el('p', 'ig-task-error', task.error));

    if ((task.references || []).length) {
      const row = el('div', 'ig-refs-row');
      task.references.forEach((ref) => {
        const img = el('img');
        img.src = ref.url;
        img.alt = `参考图 ${ref.index}`;
        img.title = ref.name || `参考图 ${ref.index}`;
        row.append(img);
      });
      card.append(row);
    }

    if (task.active) {
      const total = Math.max(1, Number(task.count) || 1);
      const done = Math.min(total, Number(task.progress) || 0);
      const bar = el('div', 'ig-progress');
      const fill = el('span');
      fill.style.width = `${Math.round((done / total) * 100)}%`;
      bar.append(fill);
      card.append(bar);
    }

    if ((task.images || []).length) {
      const grid = el('div', 'ig-shots');
      task.images.forEach((image) => {
        const shot = el('div', 'ig-shot');
        const img = el('img');
        img.src = image.url;
        img.alt = image.name || '生成结果';
        img.loading = 'lazy';
        shot.append(img);
        shot.title = '点击查看原图';
        shot.addEventListener('click', () => window.open(image.url, '_blank', 'noopener'));
        const bar = el('div', 'ig-shot-bar');
        bar.append(el('span', null, `#${image.index}`));
        const link = el('a', null, '下载');
        link.href = image.download_url;
        link.download = image.name || '';
        link.addEventListener('click', (event) => event.stopPropagation());
        bar.append(link);
        shot.append(bar);
        grid.append(shot);
      });
      card.append(grid);
    }
    return card;
  }

  async function removeTask(task) {
    if (!window.confirm('删除这条生成记录？本机上的图片文件也会一起删掉。')) return;
    try {
      await api(`/api/imagegen/tasks/${task.id}`, { method: 'DELETE' });
      state.tasks = state.tasks.filter((item) => item.id !== task.id);
      renderTasks();
      toast('已删除');
    } catch (err) {
      showError(err && err.message ? err.message : '删除失败，请重试。');
    }
  }

  async function refresh() {
    try {
      const data = await api('/api/imagegen/tasks?limit=40');
      state.tasks = (data && data.items) || [];
      renderTasks();
      showError('');
    } catch (err) {
      $('ig-status').textContent = (err && err.message) || '加载失败';
    }
    schedule();
  }

  // 只在「本页可见 + 确实有任务在跑」时轮询；切走就停，回来由 enter() 重新拉起。
  function schedule() {
    window.clearTimeout(state.timer);
    state.timer = null;
    const view = $('imagegen-view');
    if (!view || !view.classList.contains('active-view')) return;
    if (!(state.tasks || []).some((task) => task.active)) return;
    state.timer = window.setTimeout(refresh, POLL_MS);
  }

  // ------------------------------------------------------------ 绑定

  function bind() {
    if (state.bound) return;
    state.bound = true;

    const prompt = $('ig-prompt');
    prompt.addEventListener('input', updateCount);
    updateCount();

    const file = $('ig-file');
    file.addEventListener('change', (event) => {
      addFiles(event.target.files);
      event.target.value = '';
    });

    const zone = document.querySelector('#imagegen-view .ig-drop');
    if (zone) {
      ['dragenter', 'dragover'].forEach((type) => {
        zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.add('drag'); });
      });
      ['dragleave', 'dragend'].forEach((type) => {
        zone.addEventListener(type, () => zone.classList.remove('drag'));
      });
      zone.addEventListener('drop', (event) => {
        event.preventDefault();
        zone.classList.remove('drag');
        if (event.dataTransfer && event.dataTransfer.files) addFiles(event.dataTransfer.files);
      });
    }

    $('ig-model').addEventListener('change', () => syncModelOptions(false));
    $('ig-submit').addEventListener('click', submit);
    $('ig-clear').addEventListener('click', clearComposer);
    $('ig-refresh').addEventListener('click', async () => {
      await refresh();
      toast('已刷新');
    });

    // Ctrl/Cmd + Enter 直接提交，长提示词时不用挪鼠标。
    prompt.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); submit(); }
    });
  }

  function enter() {
    bind();
    ensureOptions();
    refresh();
  }

  function reset() {
    window.clearTimeout(state.timer);
    state.timer = null;
    state.tasks = [];
    state.refs = [];
    state.options = null;
    state.uploading = 0;
    renderTasks();
    renderRefs();
  }

  window.ImageGenUI = { enter, refresh, reset };
})();
