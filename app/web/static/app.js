(() => {
  const $ = (id) => document.getElementById(id);
  const state = { user: null, service: {}, prompts: [], templates: [], filteredTemplates: [], tagCategories: [], tagValues: {}, tagFilters: {}, upload: null, jobs: [], selectedJob: null, detailStatus: null, timer: null, showAll: false, submitting: false };
  const statusText = { queued: '排队中', running: '正在生成', succeeded: '已完成', partial: '部分失败', failed: '未完成', canceled: '已取消', interrupted: '已中断' };
  const esc = (s) => String(s ?? '');
  async function api(path, options = {}) {
    const res = await fetch(path, { credentials: 'same-origin', ...options, headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) } });
    const data = res.status === 204 ? {} : await res.json().catch(() => ({}));
    if (res.status === 401 && path !== '/api/login') showLogin();
    if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
    return data;
  }
  function toast(text) { const el = $('toast'); el.textContent = text; el.classList.remove('hidden'); clearTimeout(el._timer); el._timer = setTimeout(() => el.classList.add('hidden'), 3200); }
  function showLogin() { $('app-view').classList.add('hidden'); $('login-view').classList.remove('hidden'); state.user = null; state.showAll = false; clearInterval(state.timer); }
  function showApp() { $('login-view').classList.add('hidden'); $('app-view').classList.remove('hidden'); state.showAll = false; $('jobs-heading').textContent = '我的任务'; $('scope-toggle').textContent = '查看全部用户'; $('welcome-user').textContent = state.user.username; $('admin-tab').classList.toggle('hidden', state.user.role !== 'admin'); $('scope-toggle').classList.toggle('hidden', state.user.role !== 'admin'); }
  function switchView(id) { document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.view === id)); document.querySelectorAll('.view').forEach(v => v.classList.toggle('active-view', v.id === id)); if (id === 'history-view') refreshJobs(); if (id === 'admin-view') refreshAdmin(); }
  document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault(); $('login-error').classList.add('hidden');
    try { const data = await api('/api/login', { method: 'POST', body: JSON.stringify({ username: $('login-name').value.trim(), password: $('login-password').value }) }); state.user = data.user; await bootstrap(); }
    catch (err) { $('login-error').textContent = err.message; $('login-error').classList.remove('hidden'); }
  });
  $('logout-btn').addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST' }); } catch (_) {} showLogin(); });

  async function bootstrap() {
    const data = await api('/api/bootstrap'); state.user = data.user; state.service = data.service; state.prompts = data.prompts || []; showApp();
    const ready = Number(data.service.source_ready_templates || 0);
    $('service-status').textContent = data.service.templates_error ? '视频场景暂不可用'
      : `${data.service.templates_count} 个${data.service.tag_filter_enabled ? '已标注视频场景' : '视频场景'}已就绪${data.service.templates_count ? `（${ready} 个可生成）` : ''}`;
    $('server-summary').textContent = `${data.service.provider || 'Wan3'} · 本机服务`;
    if (data.service.notice) { $('notice').textContent = data.service.notice; $('notice').classList.remove('hidden'); }
    if (data.service.templates_error) { $('notice').textContent = '视频场景暂时无法加载，请联系管理员。'; $('notice').classList.remove('hidden'); }
    else if (data.service.templates_count && !ready) { $('notice').textContent = `${data.service.templates_count} 个场景已标注标签，但都还没上传到云端模板库：现在可以按标签筛选和预览，生成功能要等上传完成。`; $('notice').classList.remove('hidden'); }
    $('count-select').querySelectorAll('option').forEach(o => { if (Number(o.value) > data.service.max_items) o.disabled = true; });
    const promptSelect = $('prompt-select'); promptSelect.replaceChildren();
    for (const p of state.prompts) { const o = document.createElement('option'); o.value = p.id; o.textContent = p.title; o.dataset.body = p.body || ''; promptSelect.append(o); }
    $('prompt-body').value = promptSelect.selectedOptions[0]?.dataset.body || '';
    updatePromptCount();
    await loadTemplates(); updateSummary(); await refreshJobs();
    clearInterval(state.timer); state.timer = setInterval(() => { if (!document.hidden) refreshJobs(); }, 3500);
  }
  async function loadTemplates() {
    try {
      const data = await api('/api/templates');
      state.templates = data.items || [];
      state.tagCategories = data.categories || [];
      state.tagValues = data.tag_values || {};
      renderTagFilters();
      refreshFilteredTemplates();
    } catch (err) { $('notice').textContent = err.message; $('notice').classList.remove('hidden'); }
  }

  function collectTagFilters() {
    const filters = {};
    document.querySelectorAll('.tag-filter-input:checked').forEach(input => {
      const category = input.dataset.category;
      (filters[category] ||= []).push(input.value);
    });
    return filters;
  }

  function matchesTagFilters(template, filters) {
    return Object.entries(filters).every(([category, selected]) =>
      selected.some(tag => (template.tags || []).includes(tag) && (template.tag_categories || {})[tag] === category)
    );
  }

  function renderTagFilters() {
    const card = $('tag-filter-card');
    const root = $('tag-filter-groups');
    root.replaceChildren();
    const categories = state.tagCategories.length ? state.tagCategories : Object.keys(state.tagValues);
    const hasAny = categories.some(category => (state.tagValues[category] || []).length);
    card.classList.toggle('hidden', !hasAny);
    if (!hasAny) return;
    for (const category of categories) {
      const tags = state.tagValues[category] || [];
      if (!tags.length) continue;
      const group = document.createElement('fieldset');
      group.className = 'tag-filter-group';
      const legend = document.createElement('legend');
      legend.textContent = category;
      group.append(legend);
      const options = document.createElement('div');
      options.className = 'tag-filter-options';
      for (const tag of tags) {
        const label = document.createElement('label');
        label.className = 'tag-filter-option';
        const input = document.createElement('input');
        input.type = 'checkbox'; input.className = 'tag-filter-input';
        input.dataset.category = category; input.value = tag;
        input.addEventListener('change', refreshFilteredTemplates);
        const text = document.createElement('span'); text.textContent = tag;
        label.append(input, text); options.append(label);
      }
      group.append(options); root.append(group);
    }
  }

  function refreshFilteredTemplates() {
    state.tagFilters = collectTagFilters();
    state.filteredTemplates = state.templates.filter(template => matchesTagFilters(template, state.tagFilters));
    const select = $('template-select');
    const previous = select.value;
    select.replaceChildren();
    state.filteredTemplates.forEach(template => {
      const option = document.createElement('option');
      option.value = template.id;
      option.textContent = `${template.name}（${template.id}）`;
      select.append(option);
    });
    if (state.filteredTemplates.some(template => String(template.id) === previous)) select.value = previous;
    else if (select.options.length) select.selectedIndex = 0;
    const count = state.filteredTemplates.length;
    const readyCount = state.filteredTemplates.filter(template => template.ready).length;
    const filterActive = Object.keys(state.tagFilters).length > 0;
    $('tag-filter-summary').textContent = filterActive
      ? `匹配 ${count} / ${state.templates.length} 个场景${count && !readyCount ? '（均未上传云端）' : ''}`
      : `${state.templates.length} 个可用场景${state.templates.length && !readyCount ? '（均未上传云端）' : ''}`;
    const cap = Math.min(Number(state.service.max_items || 20), count);
    const countSelect = $('count-select');
    [...countSelect.options].forEach(option => { option.disabled = Number(option.value) > cap; });
    if (count > 0 && Number(countSelect.value) > cap) {
      const fallback = [...countSelect.options].find(option => !option.disabled);
      if (fallback) countSelect.value = fallback.value;
    }
    updateTemplatePreview();
    updateSummary();
  }

  $('clear-tag-filter').addEventListener('click', () => {
    document.querySelectorAll('.tag-filter-input').forEach(input => { input.checked = false; });
    refreshFilteredTemplates();
  });

  document.querySelectorAll('input[name="selection"]').forEach(r => r.addEventListener('change', () => {
    const selected = document.querySelector('input[name="selection"]:checked').value === 'selected';
    $('template-wrap').classList.toggle('hidden', !selected); $('count-wrap').classList.toggle('hidden', selected);
    $('template-preview').classList.toggle('hidden', !selected); updateTemplatePreview(); updateSummary();
  }));
  ['count-select','resolution','duration'].forEach(id => $(id).addEventListener('change', updateSummary));
  $('prompt-select').addEventListener('change', () => { $('prompt-body').value = $('prompt-select').selectedOptions[0]?.dataset.body || ''; updatePromptCount(); updateSummary(); });
  $('prompt-body').addEventListener('input', () => { updatePromptCount(); updateSummary(); });
  function updatePromptCount() { $('prompt-length').textContent = `${$('prompt-body').value.length} / 4000`; }
  $('template-select').addEventListener('change', () => { updateTemplatePreview(); updateSummary(); });
  function updateTemplatePreview() {
    const selected = document.querySelector('input[name="selection"]:checked').value === 'selected';
    const video = $('template-preview');
    if (!selected || !$('template-select').value) { video.removeAttribute('src'); video.classList.add('hidden'); return; }
    video.src = `/api/templates/${encodeURIComponent($('template-select').value)}/preview`;
    video.classList.remove('hidden');
  }
  function selectedCount() {
    return document.querySelector('input[name="selection"]:checked').value === 'selected'
      ? ($('template-select').value ? 1 : 0) : Number($('count-select').value);
  }
  function updateSummary() {
    const count = selectedCount(); const prompt = state.prompts.find(p => p.id === $('prompt-select').value);
    const selectedPreset = $('prompt-select').selectedOptions[0];
    const edited = selectedPreset && $('prompt-body').value.trim() !== (selectedPreset.dataset.body || '').trim();
    const available = state.filteredTemplates.length;
    const readyAvailable = state.filteredTemplates.filter(template => template.ready).length;
    $('summary-title').textContent = available ? `准备生成 ${count} 条视频` : '当前标签筛选没有匹配的视频';
    const setting = `${$('resolution').value} · ${$('duration').value === '-1' ? '跟随场景时长' : `${$('duration').value} 秒`} · ${prompt ? prompt.title : '标准换装'}${edited ? ' · 已微调提示词' : ''}`;
    $('summary-detail').textContent = `${setting} · 筛选后 ${available} 个场景${available && !readyAvailable ? ' · 尚未上传云端，暂不能生成' : ''}`;
    $('submit-btn').disabled = state.submitting || !available || !readyAvailable || !count || count > available;
  }

  const fileInput = $('image-file');
  fileInput.addEventListener('change', () => previewFile(fileInput.files[0]));
  const dropZone = $('drop-zone');
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag'));
  dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('drag'); if (e.dataTransfer.files[0]) { fileInput.files = e.dataTransfer.files; previewFile(e.dataTransfer.files[0]); } });
  function previewFile(file) {
    state.upload = null; $('image-info').textContent = '';
    if (!file) return;
    const ok = /\.(jpe?g|png|webp|bmp)$/i.test(file.name);
    if (!ok || file.size > 20 * 1024 * 1024) { $('image-info').textContent = '请选择 JPG、PNG 或 WebP 图片，大小不超过 20MB。iPhone HEIC 请先另存为 JPG。'; fileInput.value = ''; return; }
    const url = URL.createObjectURL(file); $('image-preview').src = url; $('image-preview').classList.remove('hidden'); $('drop-copy').classList.add('hidden');
    $('image-info').textContent = `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`;
  }
  $('submit-btn').addEventListener('click', async () => {
    const file = fileInput.files[0]; if (!file) return showCreateError('请先上传服装图片。');
    const selection = document.querySelector('input[name="selection"]:checked').value; const count = selectedCount();
    const promptBody = $('prompt-body').value.trim();
    if (!state.filteredTemplates.length) return showCreateError('当前标签筛选没有匹配的视频场景，请清除或调整筛选条件。');
    if (!count || count > state.filteredTemplates.length) return showCreateError(`当前筛选只有 ${state.filteredTemplates.length} 个视频场景，请减少生成数量。`);
    if (!promptBody) return showCreateError('提示词不能为空，请选择模板或填写内容。');
    if (promptBody.length > 4000) return showCreateError('提示词最多 4000 个字符。');
    if (!confirm(`将提交 ${count} 条生成任务，标签筛选后有 ${state.filteredTemplates.length} 个候选视频，可能产生云端费用。已提交的任务不能撤回。是否继续？`)) return;
    const btn = $('submit-btn'); state.submitting = true; btn.disabled = true; btn.textContent = '正在上传…'; showCreateError('');
    try {
      const dataUrl = await readFile(file);
      const uploaded = await api('/api/uploads', { method: 'POST', body: JSON.stringify({ filename: file.name, data: dataUrl }) });
      btn.textContent = '正在提交…';
      const body = { upload_id: uploaded.upload_id, selection, count, template_id: $('template-select').value,
        tag_filters: state.tagFilters, prompt_id: $('prompt-select').value, prompt: promptBody,
        resolution: $('resolution').value, duration: Number($('duration').value), confirmed: true };
      const result = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) });
      toast('任务已提交，正在排队。'); fileInput.value = ''; state.upload = null; $('image-preview').classList.add('hidden'); $('drop-copy').classList.remove('hidden'); $('image-info').textContent = '';
      switchView('history-view'); await refreshJobs(result.job.id);
    } catch (err) { showCreateError(err.message); }
    finally { state.submitting = false; btn.textContent = '确认提交'; updateSummary(); }
  });
  function readFile(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('无法读取图片')); reader.readAsDataURL(file); }); }
  function showCreateError(text) { const el = $('create-error'); el.textContent = text; el.classList.toggle('hidden', !text); }

  $('refresh-btn').addEventListener('click', () => refreshJobs());
  $('scope-toggle').addEventListener('click', async () => { state.showAll = !state.showAll; $('scope-toggle').textContent = state.showAll ? '只看我的任务' : '查看全部用户'; $('jobs-heading').textContent = state.showAll ? '全部用户任务' : '我的任务'; await refreshJobs(); });
  async function refreshJobs(openId = '') {
    if (!state.user) return;
    try {
      const scope = state.user.role === 'admin' && state.showAll ? 'all' : 'mine';
      const data = await api(`/api/jobs?scope=${scope}`); state.jobs = data.jobs || []; renderJobs();
      if (openId) openJob(openId); else if (state.selectedJob) { const current = state.jobs.find(j => j.id === state.selectedJob); if (current && (['queued','running'].includes(current.status) || current.status !== state.detailStatus)) renderDetail(current); }
    } catch (_) { /* 轮询失败时保留当前画面，下次重试 */ }
  }
  function renderJobs() {
    const root = $('jobs-list'); root.replaceChildren();
    if (!state.jobs.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = '暂无任务。开始新建一个试试。'; root.append(empty); return; }
    state.jobs.forEach(job => {
      const card = document.createElement('article'); card.className = 'job-card'; card.addEventListener('click', () => openJob(job.id));
      const left = document.createElement('div'); const title = document.createElement('div'); title.className = 'job-title'; title.textContent = `换装视频 · ${job.template_ids?.length || job.total || 0} 条`;
      const meta = document.createElement('div'); meta.className = 'job-meta'; meta.textContent = `${new Date(job.created_at * 1000).toLocaleString()} · ${job.resolution} · ${job.duration === -1 ? '跟随场景' : `${job.duration}秒`}${state.user.role === 'admin' ? ` · ${job.owner}` : ''}`;
      left.append(title, meta); const prog = document.createElement('div'); prog.className = 'progress'; const fill = document.createElement('span'); fill.style.width = `${job.total ? Math.min(100, job.done / job.total * 100) : 0}%`; prog.append(fill); left.append(prog);
      const actions = document.createElement('div'); actions.className = 'job-actions'; const badge = document.createElement('span'); badge.className = `status ${job.status}`; badge.textContent = statusText[job.status] || job.status; actions.append(badge);
      if (job.status === 'queued' || job.status === 'running') { const cancel = document.createElement('button'); cancel.className = 'quiet'; cancel.textContent = '取消'; cancel.addEventListener('click', async e => { e.stopPropagation(); if (confirm('取消任务？已提交给云端的任务仍可能继续并产生费用。')) { try { await api(`/api/jobs/${job.id}/cancel`, { method: 'POST', body: '{}' }); await refreshJobs(); } catch (err) { toast(err.message); } } }); actions.append(cancel); }
      card.append(left, actions); root.append(card);
    });
  }
  async function openJob(id) { state.selectedJob = id; try { const data = await api(`/api/jobs/${id}`); renderDetail(data.job); } catch (err) { toast(err.message); } }
  function renderDetail(job) {
    state.detailStatus = job.status;
    const root = $('job-detail'); root.classList.remove('hidden'); root.replaceChildren();
    const head = document.createElement('div'); head.className = 'detail-head'; const left = document.createElement('div'); const h = document.createElement('h3'); h.textContent = `任务 ${job.batch_id}`; const p = document.createElement('p'); p.className = 'muted'; p.textContent = job.message || statusText[job.status]; left.append(h, p); const badge = document.createElement('span'); badge.className = `status ${job.status}`; badge.textContent = statusText[job.status] || job.status; head.append(left, badge); root.append(head);
    const meta = document.createElement('p'); meta.className = 'job-meta'; meta.textContent = `${job.done || 0} / ${job.total || 0} 条完成 · ${job.resolution} · ${job.duration === -1 ? '跟随场景时长' : `${job.duration} 秒`}`; root.append(meta);
    if (job.status === 'succeeded' || job.status === 'partial') { const dl = document.createElement('a'); dl.className = 'link-btn'; dl.href = job.download_all; dl.textContent = '下载已完成视频和报告（ZIP）'; root.append(dl); const first = (job.records || []).find(r => r.status === 'success' && r.download_url); if (first) { const player = document.createElement('video'); player.controls = true; player.preload = 'metadata'; player.className = 'preview-video'; player.src = first.download_url; root.append(player); } }
    const failedCount = (job.records || []).filter(r => ['failed','invalid','expired','canceled'].includes(r.status)).length;
    if (failedCount && ['partial','failed','canceled'].includes(job.status)) { const retry = document.createElement('button'); retry.className = 'quiet'; retry.textContent = `只重试失败的 ${failedCount} 条`; retry.addEventListener('click', async () => { if (!confirm(`只重试失败的 ${failedCount} 条，可能再次产生费用。是否继续？`)) return; try { const data = await api(`/api/jobs/${job.id}/retry`, { method: 'POST', body: '{}' }); toast('失败项已重新排队。'); await refreshJobs(data.job.id); } catch (err) { toast(err.message); } }); root.append(retry); }
    const list = document.createElement('div'); list.className = 'record-list';
    (job.records || []).forEach(rec => { const row = document.createElement('div'); row.className = 'record'; const id = document.createElement('b'); id.textContent = `场景 ${rec.id}`; const info = document.createElement('div'); const stateLine = document.createElement('span'); stateLine.textContent = statusText[rec.status] || rec.status; info.append(stateLine); const small = document.createElement('small'); small.textContent = rec.error || rec.archive_warning || (rec.seconds ? `用时 ${rec.seconds} 秒` : ''); info.append(small); row.append(id, info); if (rec.download_url) { const a = document.createElement('a'); a.className = 'link-btn'; a.href = rec.download_url; a.textContent = '下载'; row.append(a); } else row.append(document.createElement('span')); list.append(row); }); root.append(list);
    const logs = document.createElement('div'); logs.className = 'log-box'; logs.textContent = (job.log_tail || []).slice(-60).join('\n') || '任务日志将在这里显示。'; root.append(logs);
  }

  async function refreshAdmin() {
    if (state.user?.role !== 'admin') return;
    try {
      const data = await api('/api/admin/status'); const root = $('admin-status'); root.replaceChildren();
      [[data.templates, '可用视频场景'], [data.queued, '排队任务'], [data.active, '正在运行'], [`${data.free_gb} GB`, '主机剩余空间']].forEach(([value,label]) => { const box = document.createElement('div'); box.className = 'stat'; const b = document.createElement('b'); b.textContent = value; const s = document.createElement('small'); s.textContent = label; box.append(b,s); root.append(box); });
      const userData = await api('/api/admin/users'); const usersRoot = $('users-list'); usersRoot.replaceChildren();
      (userData.users || []).forEach(account => { const row = document.createElement('div'); row.className = 'record'; const name = document.createElement('b'); name.textContent = account.username; const info = document.createElement('div'); info.textContent = `${account.role === 'admin' ? '管理员' : '普通使用者'} · ${account.enabled ? '启用' : '已停用'}`; const actions = document.createElement('div'); actions.className = 'record-actions';
        const reset = document.createElement('button'); reset.className = 'quiet'; reset.textContent = '重置密码'; reset.addEventListener('click', async () => { const password = prompt(`为 ${account.username} 设置新密码（至少 8 位）：`); if (!password) return; try { await api(`/api/admin/users/${encodeURIComponent(account.username)}/reset`, { method: 'POST', body: JSON.stringify({ password }) }); toast('密码已重置，该用户需重新登录。'); } catch (err) { toast(err.message); } }); actions.append(reset);
        if (account.username !== state.user.username) { const toggle = document.createElement('button'); toggle.className = 'quiet'; toggle.textContent = account.enabled ? '停用' : '启用'; toggle.addEventListener('click', async () => { if (account.enabled && !confirm(`停用账号 ${account.username}？`)) return; try { await api(`/api/admin/users/${encodeURIComponent(account.username)}/${account.enabled ? 'disable' : 'enable'}`, { method: 'POST', body: '{}' }); toast(account.enabled ? '账号已停用。' : '账号已启用。'); await refreshAdmin(); } catch (err) { toast(err.message); } }); actions.append(toggle); }
        row.append(name, info, actions); usersRoot.append(row); });
    } catch (err) { toast(err.message); }
  }
  $('reload-templates').addEventListener('click', async () => { try { const result = await api('/api/admin/reload-templates', { method: 'POST', body: '{}' }); toast(`已加载 ${result.count} 个视频场景`); await bootstrap(); } catch (err) { toast(err.message); } });
  $('add-user-form').addEventListener('submit', async e => { e.preventDefault(); const f = new FormData(e.currentTarget); try { await api('/api/admin/users', { method: 'POST', body: JSON.stringify({ username: f.get('username'), password: f.get('password'), role: f.get('role') }) }); e.currentTarget.reset(); toast('账号已添加。'); await refreshAdmin(); } catch (err) { toast(err.message); } });
  async function init() { try { const data = await api('/api/me'); state.user = data.user; await bootstrap(); } catch (_) { showLogin(); } }
  init();
})();
