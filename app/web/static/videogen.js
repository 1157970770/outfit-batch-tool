/* ============================================================
   自由视频 tab（通用 wan3.0）
   ------------------------------------------------------------
   · 选项（模式 / 分辨率 / 比例 / 时长 / 上限）全部来自
     /api/bootstrap 的 service.videogen，前端不复制官方规则。
   · 参考图走 /api/uploads（拿 upload_id）；参考视频 / 音频
     走 /api/videogen/upload（原始字节流，单文件最大 100MB）。
   · 生成在服务端独立队列里跑，这里只轮询记录；离开页面不影响出片。
   ============================================================ */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 4000;
  const IMG_SUFFIX = /\.(jpe?g|png|webp|bmp)$/i;
  const VIDEO_SUFFIX = /\.(mp4|mov|m4v)$/i;
  const AUDIO_SUFFIX = /\.(wav|mp3)$/i;
  const ACTIVE_STATUSES = ['排队中', '生成中'];

  const state = {
    bound: false,
    options: null,
    images: [],   // { upload_id, name, preview, size_mb }
    videos: [],   // { upload_id, name, size_mb, duration }
    audios: [],   // { upload_id, name, size_mb }
    tasks: [],
    timer: null,
    uploading: 0,
    submitting: false,
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

  function isAdmin() {
    const user = window.OutfitApp && window.OutfitApp.state && window.OutfitApp.state.user;
    return !!(user && user.role === 'admin');
  }

  function showError(text) {
    const box = $('vg-error');
    if (!box) return;
    if (!text) { box.textContent = ''; box.classList.add('hidden'); return; }
    box.textContent = text;
    box.classList.remove('hidden');
  }

  function fmtTime(text) {
    if (!text) return '';
    const date = new Date(String(text).replace(' ', 'T'));
    if (Number.isNaN(date.getTime())) return String(text);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function badgeClass(status) {
    if (status === '排队中') return 'is-wait';
    if (status === '生成中') return 'is-run';
    if (status === '已完成') return 'is-ok';
    if (status === '部分完成' || status === '已取消') return 'is-warn';
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

  function fmtSize(mb) {
    const value = Number(mb);
    if (!value) return '';
    return value >= 1 ? `${value.toFixed(1)} MB` : `${Math.round(value * 1024)} KB`;
  }

  // ------------------------------------------------------------ 选项

  function limits() {
    return (state.options && state.options.limits) || {};
  }

  function hintText() {
    const options = state.options || {};
    const parts = [`总时长 = 参考视频 + 输出，最多 30 秒；一条最多 ${limits().max_count || 4} 个分镜（分别计费）。`];
    if (options.cos_enabled === false) {
      parts.push('未启用 COS：只能传参考图（走 Base64）；参考视频 / 音频需要 COS。');
    }
    return parts.join(' ');
  }

  function applyOptions(options) {
    state.options = options || null;
    const submit = $('vg-submit');
    if (!options) {
      $('vg-hint').textContent = '视频配置读取失败，请刷新页面重试。';
      if (submit) submit.disabled = true;
      return;
    }

    const mode = $('vg-mode');
    mode.replaceChildren();
    (options.modes || []).forEach((item) => mode.append(new Option(item.label || item.value, item.value)));

    const resolution = $('vg-resolution');
    resolution.replaceChildren();
    (options.resolutions || []).forEach((item) => resolution.append(new Option(item.label || item.value, item.value)));

    const ratio = $('vg-ratio');
    ratio.replaceChildren();
    (options.ratios || []).forEach((item) => ratio.append(new Option(item.label || item.value, item.value)));

    const duration = $('vg-duration');
    duration.replaceChildren();
    (options.durations || []).forEach((item) => duration.append(new Option(item.label || String(item.value), String(item.value))));

    const maxCount = Math.max(1, Math.min(8, Number(limits().max_count) || 4));
    const count = $('vg-count');
    count.replaceChildren();
    for (let i = 1; i <= maxCount; i += 1) count.append(new Option(`${i} 条`, String(i)));

    const defaults = options.defaults || {};
    if (defaults.mode) mode.value = defaults.mode;
    if (defaults.resolution) resolution.value = defaults.resolution;
    if (defaults.ratio) ratio.value = defaults.ratio;
    duration.value = String(defaults.duration != null ? defaults.duration : 5);
    count.value = String(defaults.count || 1);
    $('vg-audio').checked = !!defaults.audio;
    $('vg-prompt-extend').checked = defaults.prompt_extend !== false;
    $('vg-watermark').checked = !!defaults.watermark;

    const configured = !!options.configured;
    submit.disabled = !configured;
    $('vg-hint').textContent = configured
      ? hintText()
      : (options.reason || '自由视频未配置：检查 server/config.json 的 wan.api_key / wan.model。');
    if (!configured) showError(options.reason || '');
    syncMode();
  }

  async function ensureOptions() {
    if (state.options) return;
    const appState = window.OutfitApp && window.OutfitApp.state;
    const cached = appState && appState.service && appState.service.videogen;
    if (cached) { applyOptions(cached); return; }
    try {
      const boot = await api('/api/bootstrap');
      applyOptions((boot.service && boot.service.videogen) || null);
    } catch (err) {
      applyOptions(null);
      showError(err && err.message ? err.message : '视频配置读取失败。');
    }
  }

  // 「自动」在有参考视频时按编辑走，所以 adaptive 只在确定是编辑时才可选。
  function syncMode() {
    const mode = $('vg-mode');
    const ratio = $('vg-ratio');
    if (!mode || !ratio) return;
    const choice = mode.value;
    const hasVideo = state.videos.length > 0;
    const editing = choice === 'edit' || (choice === 'auto' && hasVideo);
    const adaptive = Array.from(ratio.options).find((item) => item.value === 'adaptive');
    if (adaptive) adaptive.disabled = !editing;
    if (!editing && ratio.value === 'adaptive') ratio.value = '16:9';
    const spec = ((state.options || {}).modes || []).find((item) => item.value === choice);
    $('vg-hint').textContent = state.options && state.options.configured
      ? `${spec && spec.hint ? spec.hint + '。' : ''}${hintText()}`
      : ($('vg-hint').textContent || '');
  }

  // ------------------------------------------------------------ 参考图

  function renderImages() {
    const host = $('vg-refs');
    host.replaceChildren();
    state.images.forEach((item, index) => {
      const box = el('div', 'vg-ref');
      const img = el('img', 'vg-ref-thumb');
      img.src = item.preview;
      img.alt = item.name || `参考图 ${index + 1}`;
      const badge = el('span', 'vg-ref-index', `图${index + 1}`);
      const remove = el('button', 'vg-ref-remove', '×');
      remove.type = 'button';
      remove.title = '移除这张参考图';
      remove.addEventListener('click', () => {
        state.images.splice(index, 1);
        renderImages();
        syncMode();
      });
      box.append(img, badge, remove, el('span', 'vg-ref-name', item.name || ''));
      host.append(box);
    });
    if (state.uploading > 0) host.append(el('span', 'muted', `上传中…（${state.uploading}）`));
  }

  async function addImages(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    const limit = Math.max(1, Math.min(10, Number(limits().max_images) || 10));
    const room = limit - state.images.length - state.uploading;
    if (room <= 0) { showError(`参考图最多 ${limit} 张，先移除几张再添加。`); return; }
    showError('');
    const accepted = list.slice(0, room);
    if (list.length > room) showError(`参考图最多 ${limit} 张，已忽略多出的 ${list.length - room} 张。`);
    const maxMb = Number(limits().image_max_mb) || 20;

    for (const file of accepted) {
      if (!IMG_SUFFIX.test(file.name || '')) {
        showError(`「${file.name}」不是支持的图片格式（JPG / PNG / WebP / BMP）。`);
        continue;
      }
      if (file.size > maxMb * 1024 * 1024) {
        showError(`「${file.name}」超过 ${maxMb}MB，请压缩后再传。`);
        continue;
      }
      state.uploading += 1;
      renderImages();
      try {
        const dataUrl = await readAsDataURL(file);
        const data = await api('/api/uploads', {
          method: 'POST',
          body: JSON.stringify({ filename: file.name, data: dataUrl }),
        });
        state.images.push({
          upload_id: data.upload_id,
          name: data.name || file.name,
          preview: dataUrl,
          size_mb: data.size_mb,
        });
      } catch (err) {
        showError(err && err.message ? err.message : `「${file.name}」上传失败。`);
      } finally {
        state.uploading -= 1;
        renderImages();
      }
    }
  }

  // ------------------------------------------------------------ 参考视频/音频

  function refList(key) {
    return key === 'video' ? state.videos : state.audios;
  }

  function renderRefFiles(kind) {
    const host = $(kind === 'video' ? 'vg-video-list' : 'vg-audio-list');
    if (!host) return;
    host.replaceChildren();
    refList(kind).forEach((item, index) => {
      const row = el('div', 'vg-file');
      row.append(el('span', 'vg-file-name', `${kind === 'video' ? '视频' : '音频'}${index + 1} · ${item.name || ''}`));
      const meta = [item.duration ? `${Number(item.duration).toFixed(1)}s` : '', fmtSize(item.size_mb)].filter(Boolean).join(' · ');
      if (meta) row.append(el('span', 'vg-file-meta', meta));
      const remove = el('button', 'vg-file-drop', '移除');
      remove.type = 'button';
      remove.addEventListener('click', () => {
        refList(kind).splice(index, 1);
        renderRefFiles(kind);
        syncMode();
      });
      row.append(remove);
      host.append(row);
    });
    if (state.uploading > 0) host.append(el('span', 'muted', `上传中…（${state.uploading}）`));
  }

  // 原始字节流上传：不能用 OutfitApp.api（它会强制 JSON Content-Type）。
  async function uploadRef(file, kind) {
    const headers = {
      'X-Kind': kind,
      'X-File-Name': encodeURIComponent(file.name || ''),
    };
    const res = await fetch('/api/videogen/upload', {
      method: 'POST',
      credentials: 'same-origin',
      headers,
      body: file,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || `上传失败（${res.status}）`);
      err.status = res.status;
      throw err;
    }
    return data.upload;
  }

  async function addRefFiles(files, kind) {
    const list = Array.from(files || []);
    if (!list.length) return;
    const isVideo = kind === 'video';
    const max = Number(isVideo ? limits().max_videos : limits().max_audios) || 5;
    const maxMb = Number(isVideo ? limits().video_max_mb : limits().audio_max_mb) || (isVideo ? 100 : 15);
    const suffixRe = isVideo ? VIDEO_SUFFIX : AUDIO_SUFFIX;
    const current = refList(kind);
    const room = max - current.length - state.uploading;
    if (room <= 0) { showError(`参考${isVideo ? '视频' : '音频'}最多 ${max} 段，先移除再添加。`); return; }
    showError('');
    const accepted = list.slice(0, room);
    if (list.length > room) showError(`参考${isVideo ? '视频' : '音频'}最多 ${max} 段，已忽略多出的 ${list.length - room} 段。`);

    for (const file of accepted) {
      if (!suffixRe.test(file.name || '')) {
        showError(isVideo ? '参考视频只支持 mp4 / mov / m4v。' : '参考音频只支持 wav / mp3。');
        continue;
      }
      if (file.size > maxMb * 1024 * 1024) {
        showError(`「${file.name}」超过 ${maxMb}MB 上限。`);
        continue;
      }
      state.uploading += 1;
      renderRefFiles(kind);
      try {
        const item = await uploadRef(file, kind);
        current.push(item);
      } catch (err) {
        showError(err && err.message ? err.message : `「${file.name}」上传失败。`);
      } finally {
        state.uploading -= 1;
        renderRefFiles(kind);
        syncMode();
      }
    }
  }

  // ------------------------------------------------------------ 提交

  function updateCount() {
    const limit = Number(limits().prompt_limit) || 20000;
    $('vg-prompt-count').textContent = `${$('vg-prompt').value.length} / ${limit}`;
  }

  async function submit() {
    if (state.submitting) return;
    const prompt = $('vg-prompt').value.trim();
    if (!prompt) { showError('先写提示词再生成。'); $('vg-prompt').focus(); return; }
    if (!state.options || !state.options.configured) {
      showError((state.options && state.options.reason) || '自由视频未配置。');
      return;
    }
    if (state.uploading > 0) { showError('素材还在上传，等上传完成再提交。'); return; }
    if (state.options.cos_enabled === false && (state.videos.length || state.audios.length)) {
      showError('参考视频 / 音频需要开启 COS（官方不支持 Base64）；可以先只传参考图。');
      return;
    }

    const seedText = $('vg-seed').value.trim();
    const payload = {
      prompt,
      negative_prompt: $('vg-negative').value.trim(),
      mode: $('vg-mode').value,
      resolution: $('vg-resolution').value,
      ratio: $('vg-ratio').value,
      duration: Number($('vg-duration').value),
      count: Number($('vg-count').value) || 1,
      seed: seedText === '' ? null : Number(seedText),
      audio: $('vg-audio').checked,
      prompt_extend: $('vg-prompt-extend').checked,
      watermark: $('vg-watermark').checked,
      upload_ids: state.images.map((item) => item.upload_id),
      video_ids: state.videos.map((item) => item.upload_id),
      audio_ids: state.audios.map((item) => item.upload_id),
    };

    state.submitting = true;
    const button = $('vg-submit');
    button.disabled = true;
    button.textContent = '提交中…';
    showError('');
    try {
      const data = await api('/api/videogen/tasks', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      state.images = [];
      renderImages();
      toast(data.task && data.task.active ? '已提交，正在生成…' : '已提交');
      await refresh();
    } catch (err) {
      showError(err && err.message ? err.message : '提交失败，请重试。');
    } finally {
      state.submitting = false;
      button.textContent = '开始生成';
      button.disabled = !(state.options && state.options.configured);
    }
  }

  function clearComposer() {
    $('vg-prompt').value = '';
    $('vg-negative').value = '';
    $('vg-seed').value = '';
    state.images = [];
    state.videos = [];
    state.audios = [];
    renderImages();
    renderRefFiles('video');
    renderRefFiles('audio');
    updateCount();
    syncMode();
    showError('');
  }

  // ------------------------------------------------------------ 记录

  function modeLabel(value) {
    const spec = ((state.options || {}).modes || []).find((item) => item.value === value);
    return spec ? spec.label : (value || '');
  }

  function renderTasks() {
    const host = $('vg-tasks');
    if (!host) return;
    host.replaceChildren();
    const tasks = state.tasks || [];
    $('vg-empty').classList.toggle('hidden', tasks.length > 0);
    const activeCount = tasks.filter((task) => task.active).length;
    $('vg-status').textContent = activeCount
      ? `${activeCount} 个任务进行中，每 ${POLL_MS / 1000} 秒刷新`
      : (tasks.length ? `共 ${tasks.length} 条记录` : '');
    tasks.forEach((task) => host.append(renderTask(task)));
  }

  function renderTask(task) {
    const admin = isAdmin();
    const card = el('article', 'vg-task');

    const head = el('div', 'vg-task-head');
    head.append(el('span', `vg-badge ${badgeClass(task.status)}`, task.status || ''));
    head.append(el('span', 'vg-task-time', fmtTime(task.created_at)));
    if (task.progress) head.append(el('span', 'muted', `${task.progress} 条`));
    if (admin && task.owner) head.append(el('span', 'muted', task.owner));
    card.append(head);

    card.append(el('p', 'vg-task-prompt', task.prompt || ''));

    const metaParts = [
      modeLabel(task.mode_choice) || task.mode,
      task.resolution,
      task.ratio,
      task.duration === -1 ? '时长自动' : (task.duration ? `${task.duration} 秒` : ''),
      `共 ${task.count || 1} 条`,
      task.seed != null && task.seed !== -1 ? `seed ${task.seed}` : '',
      task.audio ? '带配音' : '',
      task.prompt_extend ? '扩写' : '',
      task.watermark ? '水印' : '',
    ].filter(Boolean);
    card.append(el('div', 'vg-task-meta', metaParts.join(' · ')));

    const refs = task.refs || {};
    const chips = [];
    ['images', 'videos', 'audios'].forEach((kind) => {
      (refs[kind] || []).forEach((ref) => {
        const label = kind === 'images' ? `图${ref.index}` : kind === 'videos' ? `视频${ref.index}` : `音频${ref.index}`;
        const link = el('a', '', label);
        link.href = ref.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.title = ref.name || label;
        chips.push(link);
      });
    });
    if (chips.length) {
      const box = el('div', 'vg-refs-inline');
      chips.forEach((chip) => box.append(chip));
      card.append(box);
    }

    const shots = el('div', 'vg-shots');
    (task.shots || []).forEach((shot) => {
      const box = el('div', 'vg-shot');
      if (shot.url) {
        const video = el('video');
        video.controls = true;
        video.preload = 'metadata';
        video.playsInline = true;
        video.src = shot.url;
        box.append(video);
        const bar = el('div', 'vg-shot-bar');
        bar.append(el('span', '', `第 ${shot.index} 条${shot.seconds ? ` · ${shot.seconds}s` : ''}`));
        const link = el('a', '', '下载');
        link.href = shot.download_url;
        bar.append(link);
        box.append(bar);
      } else {
        const holder = el('div', 'vg-shot-placeholder');
        holder.append(el('strong', '', `第 ${shot.index} 条`));
        holder.append(el('span', '', shot.status || ''));
        if (shot.error) holder.append(el('span', 'vg-shot-error', shot.error));
        box.append(holder);
      }
      shots.append(box);
    });
    if (shots.childElementCount) card.append(shots);

    if (task.message && task.status !== '已完成') {
      card.append(el('p', 'muted', task.message));
    }

    const actions = el('div', 'vg-task-actions');
    if (task.active) {
      const cancel = el('button', 'quiet', task.cancel_requested ? '正在取消…' : '取消');
      cancel.type = 'button';
      cancel.disabled = !!task.cancel_requested;
      cancel.addEventListener('click', () => doCancel(task, cancel));
      actions.append(cancel);
    } else {
      const remove = el('button', 'quiet', '删除记录');
      remove.type = 'button';
      remove.addEventListener('click', () => doDelete(task, remove));
      actions.append(remove);
    }
    card.append(actions);
    return card;
  }

  async function doCancel(task, button) {
    if (!window.confirm('取消这个任务？已经提交到云端的那一条无法撤回，仍会计费。')) return;
    button.disabled = true;
    try {
      await api(`/api/videogen/tasks/${task.id}/cancel`, { method: 'POST' });
      toast('已请求取消');
      await refresh();
    } catch (err) {
      showError(err && err.message ? err.message : '取消失败。');
      button.disabled = false;
    }
  }

  async function doDelete(task, button) {
    if (!window.confirm('删除这条记录和本机成品视频？云端任务不受影响。')) return;
    button.disabled = true;
    try {
      await api(`/api/videogen/tasks/${task.id}`, { method: 'DELETE' });
      toast('已删除');
      await refresh();
    } catch (err) {
      showError(err && err.message ? err.message : '删除失败。');
      button.disabled = false;
    }
  }

  async function refresh() {
    try {
      const data = await api('/api/videogen/tasks?limit=60');
      state.tasks = data.items || [];
      if (data.configured === false && state.options) {
        state.options = Object.assign({}, state.options, { configured: false, reason: data.reason || '' });
        $('vg-submit').disabled = true;
        $('vg-hint').textContent = data.reason || '自由视频未配置。';
      }
      renderTasks();
      schedule();
    } catch (err) {
      $('vg-status').textContent = '记录刷新失败';
      showError(err && err.message ? err.message : '记录刷新失败。');
    }
  }

  // 只在本页可见且确实有任务在跑时轮询；切走就停，回来由 enter() 拉起。
  function schedule() {
    window.clearTimeout(state.timer);
    state.timer = null;
    const view = $('videogen-view');
    if (!view || !view.classList.contains('active-view')) return;
    if (!(state.tasks || []).some((task) => task.active)) return;
    state.timer = window.setTimeout(refresh, POLL_MS);
  }

  // ------------------------------------------------------------ 绑定

  function bind() {
    if (state.bound) return;
    state.bound = true;

    $('vg-prompt').addEventListener('input', updateCount);
    $('vg-mode').addEventListener('change', syncMode);
    $('vg-files').addEventListener('change', (event) => {
      addImages(event.target.files);
      event.target.value = '';
    });
    $('vg-videos').addEventListener('change', (event) => {
      addRefFiles(event.target.files, 'video');
      event.target.value = '';
    });
    $('vg-audios').addEventListener('change', (event) => {
      addRefFiles(event.target.files, 'audio');
      event.target.value = '';
    });

    const drop = document.querySelector('.vg-drop');
    if (drop) {
      ['dragenter', 'dragover'].forEach((name) => drop.addEventListener(name, (event) => {
        event.preventDefault();
        drop.classList.add('drag');
      }));
      ['dragleave', 'dragend'].forEach((name) => drop.addEventListener(name, () => drop.classList.remove('drag')));
      drop.addEventListener('drop', (event) => {
        event.preventDefault();
        drop.classList.remove('drag');
        if (event.dataTransfer && event.dataTransfer.files) addImages(event.dataTransfer.files);
      });
    }

    $('vg-submit').addEventListener('click', submit);
    $('vg-clear').addEventListener('click', clearComposer);
    $('vg-refresh').addEventListener('click', refresh);

    updateCount();
  }

  function enter() {
    bind();
    ensureOptions();
    if (!state.tasks.length) updateCount();
    refresh();
  }

  window.VideoGenUI = {
    enter,
    refresh,
    leave: schedule,
  };
})();
