(() => {
  const $ = (id) => document.getElementById(id);
  const state = { user: null, service: {}, prompts: [], templates: [], filteredTemplates: [], tagCategories: [], tagValues: {}, tagFilters: {}, upload: null, jobs: [], selectedJob: null, detailStatus: null, timer: null, detailTimer: null, picked: new Set(), showAll: false, submitting: false, rerunDupWarnOnce: false, ownerFilter: '', adminUsers: [], usageOwner: '', usageRows: [], usageTotal: 0, styleUsed: {}, styleUsedTop: '', styleUsedTimer: 0 };
  const statusText = { queued: '排队中', running: '正在生成', succeeded: '已完成', partial: '部分失败', failed: '未完成', canceled: '已取消', interrupted: '已中断', invalid: '无效', expired: '过期', unknown: '结果未知', timeout: '等待超时', success: '成功' };
  const esc = (s) => String(s ?? '');
  // 6 栏位款号标签：读取容器内 [data-tag-slot]，返回 {top:...} 形式（空值丢弃）。
  const TAG_LABELS = { top: '上衣', lining: '内衬', pants: '裤子', shoes: '鞋子', scene: '场景', note: '特殊备注' };
  function collectTagSlots(containerId) {
    const root = $(containerId);
    if (!root) return {};
    const tags = {};
    root.querySelectorAll('input[data-tag-slot]').forEach(input => {
      const value = input.value.trim();
      if (value) tags[input.dataset.tagSlot] = value;
    });
    return tags;
  }
  function fillTagSlots(containerId, tags) {
    const root = $(containerId);
    if (!root) return;
    const source = tags && typeof tags === 'object' ? tags : {};
    root.querySelectorAll('input[data-tag-slot]').forEach(input => {
      input.value = String(source[input.dataset.tagSlot] ?? '');
    });
  }
  // 列表/详情里的一行标签摘要：上衣 AX111 · 内衬 BX222 …（旧数据只有 style_no 也兼容）。
  function tagSummaryText(tags, styleNo) {
    const source = tags && typeof tags === 'object' ? tags : {};
    const parts = Object.entries(source).filter(([slot, value]) => value && TAG_LABELS[slot])
      .map(([slot, value]) => `${TAG_LABELS[slot]} ${value}`);
    if (!parts.length && styleNo) parts.push(`上衣 ${styleNo}`);
    return parts.join(' · ');
  }
  async function api(path, options = {}) {
    const res = await fetch(path, { credentials: 'same-origin', ...options, headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) } });
    const data = res.status === 204 ? {} : await res.json().catch(() => ({}));
    if (res.status === 401 && path !== '/api/login') showLogin();
    // err.status / err.data 供调用方按状态码分支（如批次删除要区分 403/404/409）。
    // 只是往 Error 上挂两个字段，message 口径不变，老调用方不受影响。
    if (!res.ok) { const err = new Error(data.error || `请求失败（${res.status}）`); err.status = res.status; err.data = data; throw err; }
    return data;
  }
  function toast(text) { const el = $('toast'); el.textContent = text; el.classList.remove('hidden'); clearTimeout(el._timer); el._timer = setTimeout(() => el.classList.add('hidden'), 3200); }
  // 首屏占位在 session 判定后立刻撤掉，登录页/主界面才有机会显示
  function hideBoot() { const el = $('boot-view'); if (el) el.classList.add('hidden'); }
  function showLogin() { hideBoot(); $('app-view').classList.add('hidden'); $('login-view').classList.remove('hidden'); state.user = null; state.showAll = false; clearInterval(state.timer); }
  function showApp() { hideBoot(); $('login-view').classList.add('hidden'); $('app-view').classList.remove('hidden'); state.showAll = false; state.ownerFilter = ''; $('owner-filter').value = ''; $('owner-filter').classList.add('hidden'); $('jobs-heading').textContent = '历史任务'; $('scope-toggle').textContent = '查看全部用户'; $('welcome-user').textContent = state.user.username; $('admin-tab').classList.toggle('hidden', state.user.role !== 'admin'); $('scope-toggle').classList.toggle('hidden', state.user.role !== 'admin'); restoreView(); }
  // 管理员"查看全部"时的账户维度：scope=all 再加 owner 过滤，普通账号永远只看自己。
  function scopeQuery() {
    if (!(state.user && state.user.role === 'admin' && state.showAll)) return 'mine';
    return state.ownerFilter ? `all&owner=${encodeURIComponent(state.ownerFilter)}` : 'all';
  }
  async function fillOwnerFilter() {
    const select = $('owner-filter');
    const adminAll = state.user && state.user.role === 'admin' && state.showAll;
    select.classList.toggle('hidden', !adminAll);
    // 分组方式对所有账号都有用（按日期 / 按批次），只有「按账户」需要管理员看全部用户。
    const groupBy = $('history-group-by');
    if (groupBy) groupBy.classList.remove('hidden');
    if (!adminAll) { state.ownerFilter = ''; select.value = ''; return; }
    if (!state.adminUsers.length) {
      try { const data = await api('/api/admin/users'); state.adminUsers = data.users || []; } catch (_) { /* 取不到就只留"全部账户" */ }
    }
    const current = state.ownerFilter;
    select.replaceChildren(new Option('全部账户', ''));
    state.adminUsers.forEach(u => select.append(new Option(u.username, u.username)));
    select.value = current;
    if (select.value !== current) state.ownerFilter = '';
  }
  const VIEW_KEY = 'outfit.view';
  // 素材检索是另一个局域网服务（192.168.10.179:8700），内嵌在 tab 里：
  // 首次进入才挂 src（登录首屏不拉外部页面），之后切走再切回保留 iframe 状态。
  function mountMaterialFrame() {
    const frame = $('material-frame');
    if (!frame || frame.getAttribute('src')) return;
    frame.src = frame.dataset.src;
  }
  const materialReload = $('material-reload');
  if (materialReload) materialReload.addEventListener('click', () => {
    const frame = $('material-frame');
    if (frame) frame.src = frame.dataset.src; // 同一地址重新赋值即强制重载
  });
  function switchView(id) {
    document.querySelectorAll('.tab[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === id));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active-view', v.id === id));
    try { localStorage.setItem(VIEW_KEY, id); } catch (_) { /* 隐私模式下存不了，忽略 */ }
    if (id === 'history-view') refreshJobs();
    if (id === 'admin-view') refreshAdmin();
    if (id === 'batch-view' && window.BatchUI) window.BatchUI.enter();
    if (id === 'tag-view' && window.TagUI) window.TagUI.enter();
    if (id === 'material-view') mountMaterialFrame();
    if (id === 'ps-view' && window.PsUI) window.PsUI.enter();
    if (id === 'imagegen-view' && window.ImageGenUI) window.ImageGenUI.enter();
    if (id !== 'canvas-view' && window.CanvasUI) window.CanvasUI.leave();
    if (id === 'canvas-view' && window.CanvasUI) window.CanvasUI.enter();
  }
  // 回到上次看的页：只在 create/history/admin/tag/material/ps/imagegen/canvas 之间恢复。
  // 批次页有自己的状态机（进入即加载批次），刷新时自动跳过去容易让人以为"上次的任务还在"。
  function restoreView() {
    let saved = '';
    try { saved = localStorage.getItem(VIEW_KEY) || ''; } catch (_) { return; }
    if (!['create-view', 'history-view', 'admin-view', 'tag-view', 'material-view', 'ps-view', 'imagegen-view', 'canvas-view'].includes(saved)) return;
    const tab = document.querySelector(`.tab[data-view="${saved}"]`);
    if (!tab || tab.classList.contains('hidden')) return;
    if (!document.getElementById(saved)) return;
    switchView(saved);
  }
  document.querySelectorAll('.tab[data-view]').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault(); $('login-error').classList.add('hidden');
    try { const data = await api('/api/login', { method: 'POST', body: JSON.stringify({ username: $('login-name').value.trim(), password: $('login-password').value }) }); state.user = data.user; await bootstrap(); }
    catch (err) { $('login-error').textContent = err.message; $('login-error').classList.remove('hidden'); }
  });
  $('logout-btn').addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST' }); } catch (_) {} showLogin(); });

  // 开发版标记：登录页靠 /healthz、登录后靠 /api/bootstrap，两处都调用这里
  function markDev(isDev) {
    const on = Boolean(isDev);
    document.querySelectorAll('.dev-badge').forEach(el => el.classList.toggle('hidden', !on));
    document.title = on ? '换装视频工作台（开发版）' : '换装视频工作台';
  }

  async function bootstrap() {
    const data = await api('/api/bootstrap'); state.user = data.user; state.service = data.service; state.prompts = data.prompts || []; showApp();
    // 图片槽位上限以后端为准（官方 reference_image 上限 10），前端不硬编码。
    if (window.ImageSlots) {
      window.ImageSlots.configure({ max: Number(data.service.max_images) || 10 });
      if (typeof renderImageSlots === 'function') renderImageSlots();
    }
    const ready = Number(data.service.source_ready_templates || 0);
    $('service-status').textContent = data.service.templates_error ? '视频场景暂不可用'
      : `${data.service.templates_count} 个${data.service.tag_filter_enabled ? '已标注视频场景' : '视频场景'}已就绪${data.service.templates_count ? `（${ready} 个可生成）` : ''}`;
    $('server-summary').textContent = `${data.service.provider || 'Wan3'} · 本机服务`;
    // 开发版标记由后端下发（web.json → dev_mode），生产环境永远不亮
    markDev(data.service.dev_mode);
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
    if (window.BatchUI && window.BatchUI.onTemplates) window.BatchUI.onTemplates();
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

  // 款号-模板防重标记：按上衣款号查已用模板（批次生产才记账，小批量不计入）。
  // 挑选网格由 renderPickGrid 重建，已用过的场景自动挂【已用】徽标。
  function paintTemplateUsed() {
    renderPickGrid();
  }
  async function refreshStyleUsed() {
    const top = collectTagSlots('style-tags').top || '';
    state.styleUsedTop = top;
    if (!top) { state.styleUsed = {}; paintTemplateUsed(); return; }
    try {
      const data = await api(`/api/style-usage?top=${encodeURIComponent(top)}`);
      if (state.styleUsedTop !== top) return;   // 输入又变了，丢弃过期结果
      state.styleUsed = data.used || {};
    } catch { state.styleUsed = {}; }
    paintTemplateUsed();
  }
  function scheduleStyleUsed() {
    clearTimeout(state.styleUsedTimer);
    state.styleUsedTimer = setTimeout(refreshStyleUsed, 400);
  }
  // 该款号已用过的候选模板数量（提交前确认用）。
  function usedCount(ids) {
    return (ids || []).filter(id => state.styleUsed[id]).length;
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
    // 挑选网格跟着筛选走：已不在筛选结果里的选中项剔除
    const validIds = new Set(state.filteredTemplates.map(template => String(template.id)));
    for (const id of [...state.picked]) if (!validIds.has(id)) state.picked.delete(id);
    renderPickGrid();
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
    syncCountButtons();
    paintTemplateUsed();
  }

  /* 我来挑选：从筛选结果里点选，最多 3 个（与生成数量上限一致，1–3 条走快速通道）。
     未上传云端的场景置灰不可选；款号用过的挂【已用】徽标提醒但不拦。 */
  const PICK_MAX = 3;
  function renderPickGrid() {
    const root = $('template-pick');
    if (!root) return;
    root.replaceChildren();
    if (!state.filteredTemplates.length) {
      const empty = document.createElement('span');
      empty.className = 'muted';
      empty.textContent = '当前筛选没有匹配的视频场景。';
      root.append(empty);
      return;
    }
    for (const template of state.filteredTemplates) {
      const id = String(template.id);
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'template-pick-item';
      item.dataset.id = id;
      item.textContent = `${template.name}（${id}）`;
      if (!template.ready) {
        item.classList.add('is-unready');
        item.disabled = true;
        item.title = '尚未上传云端，不能生成';
      } else {
        if (state.styleUsed[id]) { item.classList.add('is-used'); item.title = `${template.name}（${id}）\n款号已用过该模板（仅提醒，可重复使用）`; }
        item.setAttribute('aria-pressed', state.picked.has(id) ? 'true' : 'false');
        item.addEventListener('click', () => {
          if (state.picked.has(id)) state.picked.delete(id);
          else {
            if (state.picked.size >= PICK_MAX) return toast(`一次最多挑 ${PICK_MAX} 个视频场景，先取消一个再选。`);
            state.picked.add(id);
          }
          root.querySelectorAll('.template-pick-item').forEach((node) => {
            const on = state.picked.has(node.dataset.id);
            node.classList.toggle('is-on', on);
            node.setAttribute('aria-pressed', on ? 'true' : 'false');
          });
          updateTemplatePreview();
          updateSummary();
        });
      }
      item.classList.toggle('is-on', state.picked.has(id));
      root.append(item);
    }
  }

  $('clear-tag-filter').addEventListener('click', () => {
    document.querySelectorAll('.tag-filter-input').forEach(input => { input.checked = false; });
    refreshFilteredTemplates();
  });

  document.querySelectorAll('input[name="selection"]').forEach(r => r.addEventListener('change', () => {
    const selected = document.querySelector('input[name="selection"]:checked').value === 'selected';
    $('template-wrap').classList.toggle('hidden', !selected); $('count-wrap').classList.toggle('hidden', selected);
    updateTemplatePreview(); updateSummary();
  }));
  ['count-select','resolution','duration'].forEach(id => $(id).addEventListener('change', updateSummary));
  // 数量用小分段控件代替下拉：值仍写进隐藏的 #count-select，
  // 这样上限裁剪、可用场景不足时的禁用逻辑都还能复用原来的 options.disabled。
  function syncCountButtons() {
    const select = $('count-select');
    document.querySelectorAll('#count-buttons button').forEach(btn => {
      const option = [...select.options].find(o => o.value === btn.dataset.count);
      const active = btn.dataset.count === select.value;
      btn.disabled = !option || option.disabled;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }
  $('count-buttons').addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-count]');
    if (!btn || btn.disabled) return;
    $('count-select').value = btn.dataset.count;
    $('count-select').dispatchEvent(new Event('change'));
  });
  $('prompt-select').addEventListener('change', () => { $('prompt-body').value = $('prompt-select').selectedOptions[0]?.dataset.body || ''; updatePromptCount(); updateSummary(); });
  $('prompt-body').addEventListener('input', () => { updatePromptCount(); updateSummary(); });
  document.querySelectorAll('#style-tags input[data-tag-slot]').forEach(input => input.addEventListener('input', () => { updateSummary(); scheduleStyleUsed(); }));
  function updatePromptCount() { $('prompt-length').textContent = `${$('prompt-body').value.length} / 4000`; }
  function templateLabel(id) {
    const template = state.templates.find(item => String(item.id) === id);
    return template ? template.name : id;
  }
  /* 「我来挑选」最多 3 个场景，每个都要在左栏看到画面。
     复用已有 cell：增删一个场景时其它视频不重建、不重新加载，也不会跳回开头。 */
  function updateTemplatePreview() {
    const selected = document.querySelector('input[name="selection"]:checked').value === 'selected';
    const wrap = $('image-preview-wrap');
    const root = $('template-preview-list');
    if (!wrap || !root) return;
    const ids = selected ? [...state.picked] : [];
    if (!ids.length) {
      for (const cell of root.querySelectorAll('.template-preview-cell')) {
        const video = cell.querySelector('video');
        if (video) { video.pause(); video.removeAttribute('src'); if (video.load) video.load(); }
        cell.remove();
      }
      wrap.classList.add('hidden');
      return;
    }
    wrap.classList.remove('hidden');
    const keep = new Set(ids);
    for (const cell of [...root.querySelectorAll('.template-preview-cell')]) {
      if (keep.has(cell.dataset.id)) continue;
      const video = cell.querySelector('video');
      if (video) { video.pause(); video.removeAttribute('src'); if (video.load) video.load(); }
      cell.remove();
    }
    ids.forEach((id, index) => {
      let cell = [...root.querySelectorAll('.template-preview-cell')].find(node => node.dataset.id === id);
      if (!cell) {
        cell = document.createElement('figure');
        cell.className = 'template-preview-cell';
        cell.dataset.id = id;
        cell.setAttribute('role', 'listitem');
        const video = document.createElement('video');
        video.className = 'template-preview';
        video.controls = true;
        video.loop = true;
        video.muted = true;
        video.playsInline = true;
        video.preload = 'metadata';
        video.src = `/api/templates/${encodeURIComponent(id)}/preview`;
        // 选中场景即自动播放 + 循环：muted 是浏览器允许自动播放的前提（模板视频本身无音轨）。
        // 被浏览器策略拦下时不报错，控制条仍在，用户手点一次即可。
        const started = video.play();
        if (started && typeof started.catch === 'function') started.catch(() => {});
        const caption = document.createElement('figcaption');
        caption.className = 'template-preview-caption';
        cell.append(video, caption);
      }
      const caption = cell.querySelector('.template-preview-caption');
      const name = templateLabel(id);
      caption.textContent = `场景 ${index + 1} · ${name}`;
      caption.title = `${name}（${id}）`;
      if (root.children[index] !== cell) root.insertBefore(cell, root.children[index] || null);
    });
  }
  function selectedCount() {
    return document.querySelector('input[name="selection"]:checked').value === 'selected'
      ? state.picked.size : Number($('count-select').value);
  }
  function updateSummary() {
    const count = selectedCount(); const prompt = state.prompts.find(p => p.id === $('prompt-select').value);
    const selectedPreset = $('prompt-select').selectedOptions[0];
    const edited = selectedPreset && $('prompt-body').value.trim() !== (selectedPreset.dataset.body || '').trim();
    const available = state.filteredTemplates.length;
    const readyAvailable = state.filteredTemplates.filter(template => template.ready).length;
    $('summary-title').textContent = available ? `准备生成 ${count} 条视频` : '当前标签筛选没有匹配的视频';
    const style = collectTagSlots('style-tags').top || '';
    const setting = `${$('resolution').value} · ${$('duration').value === '-1' ? '跟随场景时长' : `${$('duration').value} 秒`} · ${prompt ? prompt.title : '标准换装'}${style ? ` · 上衣 ${style}` : ''}${edited ? ' · 已微调提示词' : ''}`;
    $('summary-detail').textContent = `${setting} · 筛选后 ${available} 个场景${available && !readyAvailable ? ' · 尚未上传云端，暂不能生成' : ''}`;
    $('submit-btn').disabled = state.submitting || !available || !readyAvailable || !count || count > available;
    // 数量是分段按钮：值改了还要把高亮同步过去，否则点了没反应（值其实变了）
    syncCountButtons();
  }

  // 多图槽位：定妆图 = 槽位 1，细节图 = 槽位 2–10，删除后自动左移（图N 不错位）。
  const fileInput = $('image-file');
  const imageSlots = window.ImageSlots;
  state.images = imageSlots.createSlotState();
  const maxImages = Number((state.service || {}).max_images);
  if (Number.isFinite(maxImages) && maxImages > 0) imageSlots.configure({ max: maxImages });
  const renderImageSlots = () => {
    // 事件委托由 bindSlotEvents 持有；这里只重建 DOM + 更新说明文字。
    // inputId 让空槽渲染成 <label for="image-file">，点击即可原生打开文件选择器。
    imageSlots.renderSlots($('image-slots'), state.images, { inputId: 'image-file' });
    $('image-info').classList.remove('is-warn');
    $('image-info').textContent = state.images.length
      ? `已选 ${state.images.length} / ${imageSlots.maxSlots} 张 · 槽位 1 是定妆图，其余依次为图2…图${state.images.length}`
      : '槽位 1 必填：定妆图；细节图可选，最多 9 张。';
  };
  imageSlots.bindSlotEvents($('image-slots'), fileInput, state.images, {
    onChange: renderImageSlots,
    // 被拒的图必须显眼：灰字太小，用户会以为「点了没反应」
    onError: (text) => {
      $('image-info').textContent = text;
      $('image-info').classList.add('is-warn');
      toast(text);
    },
  });
  // 局域网 http 不是安全上下文，navigator.clipboard 不可用，统一退回 execCommand。
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0';
    document.body.append(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand('copy'); } finally { area.remove(); }
    if (!copied) throw new Error('浏览器不允许复制，请手动选中文本');
  }
  $('copy-prompt').addEventListener('click', async () => {
    const text = $('prompt-body').value.trim();
    if (!text) { toast('提示词是空的。'); return; }
    try {
      await copyText(text);
      toast('提示词已复制，可直接粘贴到 RunningHub。');
    } catch (err) {
      toast(`复制失败：${err.message}`);
    }
  });

  $('submit-btn').addEventListener('click', async () => {
    // 重跑带进来的槽位只有 uploadId（图已在服务器上），没有本地 File，同样算已就绪。
    if (!state.images.length || !(state.images[0].file || state.images[0].uploadId))
      return showCreateError('请先在槽位 1 上传定妆图。');
    const selection = document.querySelector('input[name="selection"]:checked').value; const count = selectedCount();
    const pickedIds = [...state.picked];
    const promptBody = $('prompt-body').value.trim();
    if (!state.filteredTemplates.length) return showCreateError('当前标签筛选没有匹配的视频场景，请清除或调整筛选条件。');
    if (selection === 'selected' && !pickedIds.length) return showCreateError('请先在「视频场景」里点选 1–3 个场景。');
    if (!count || count > state.filteredTemplates.length) return showCreateError(`当前筛选只有 ${state.filteredTemplates.length} 个视频场景，请减少生成数量。`);
    if (!promptBody) return showCreateError('提示词不能为空，请选择模板或填写内容。');
    if (promptBody.length > 4000) return showCreateError('提示词最多 4000 个字符。');
    // 同款号防重（软拦截）：上衣款号已用过的模板再提交，弹确认让使用者自己定夺。
    const topNow = collectTagSlots('style-tags').top || '';
    // 「重跑」本来就是明确要再做同一个场景，不再弹「款号已用过」的软提醒。
    if (topNow && state.styleUsedTop === topNow && !state.rerunDupWarnOnce) {
      const chosen = selection === 'selected' ? pickedIds : state.filteredTemplates.map(t => String(t.id));
      const reused = usedCount(chosen);
      if (reused && !confirm(`款号 ${topNow} 有 ${reused} 个候选视频模板在之前的批次里用过。\n\n确定要重复使用吗？（历史记录只为防重提醒，不影响归档）`)) return;
    }
    if (!confirm(`将提交 ${count} 条生成任务，标签筛选后有 ${state.filteredTemplates.length} 个候选视频。已提交的任务不能撤回。是否继续？`)) return;
    const tags = collectTagSlots('style-tags');
    if (!tags.top) return showCreateError('请填写「上衣」款号（云端归档路径按它命名）。');
    const btn = $('submit-btn'); state.submitting = true; btn.disabled = true; btn.textContent = '正在上传…'; showCreateError('');
    try {
      // 只上传还没拿到 upload_id 的格子：上一轮失败重试时不会重复传已成功的图。
      const upload = await imageSlots.uploadImages(state.images, {
        concurrency: 3,
        onProgress: (info) => {
          btn.textContent = `正在上传 ${info.done}/${info.total} 张…`;
          renderImageSlots();
        },
      });
      renderImageSlots();
      if (!upload.ok) {
        const first = upload.failures[0];
        throw new Error(`第 ${first.index} 张图片（${first.name}）上传失败：${first.message}；其余已上传的图会保留，可直接重试。`);
      }
      btn.textContent = '正在提交…';
      const ids = upload.uploadIds;
      const body = { upload_ids: ids, upload_id: ids[0], selection, count, template_id: pickedIds.join(','),
        tag_filters: state.tagFilters, prompt_id: $('prompt-select').value, prompt: promptBody,
        resolution: $('resolution').value, duration: Number($('duration').value), confirmed: true,
        tags };
      const result = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) });
      toast('任务已提交，正在排队。');
      imageSlots.resetSlots(state.images);
      fileInput.value = '';
      renderImageSlots();
      switchView('history-view'); await refreshJobs(result.job.id);
    } catch (err) { showCreateError(err.message); }
    finally { state.submitting = false; state.rerunDupWarnOnce = false; btn.textContent = '确认提交'; updateSummary(); }
  });
  function showCreateError(text) { const el = $('create-error'); el.textContent = text; el.classList.toggle('hidden', !text); }

  $('refresh-btn').addEventListener('click', () => refreshJobs());
  function refreshBatchIfVisible() {
    const view = $('batch-view');
    if (view && view.classList.contains('active-view') && window.BatchUI) window.BatchUI.refresh();
  }
  $('scope-toggle').addEventListener('click', async () => { state.showAll = !state.showAll; $('scope-toggle').textContent = state.showAll ? '只看我的任务' : '查看全部用户'; $('jobs-heading').textContent = state.showAll ? '全部用户任务' : '历史任务'; await fillOwnerFilter(); await refreshJobs(); refreshBatchIfVisible(); });
  $('owner-filter').addEventListener('change', async (e) => { state.ownerFilter = e.target.value; $('jobs-heading').textContent = state.ownerFilter ? `${state.ownerFilter} 的任务` : '全部用户任务'; await refreshJobs(); refreshBatchIfVisible(); });
  async function refreshJobs(openId = '') {
    if (!state.user) return;
    if (window.BatchUI && window.BatchUI.refreshHistory) return window.BatchUI.refreshHistory(openId);
    return refreshFlatJobs(openId);
  }
  // 旧版平铺任务列表：分组接口不可用时兜底，避免整页空白。
  async function refreshFlatJobs(openId = '') {
    if (!state.user) return;
    try {
      const scope = scopeQuery();
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
      const meta = document.createElement('div'); meta.className = 'job-meta'; meta.textContent = `${new Date(job.created_at * 1000).toLocaleString()} · ${job.resolution} · ${job.duration === -1 ? '跟随场景' : `${job.duration}秒`}${job.lane === 'small' ? ' · 快速通道' : ''}${state.user.role === 'admin' ? ` · ${job.owner}` : ''}`;
      left.append(title, meta); const prog = document.createElement('div'); prog.className = 'progress'; const fill = document.createElement('span'); fill.style.width = `${job.total ? Math.min(100, job.done / job.total * 100) : 0}%`; prog.append(fill); left.append(prog);
      const actions = document.createElement('div'); actions.className = 'job-actions'; const badge = document.createElement('span'); badge.className = `status ${job.status}`; badge.textContent = statusText[job.status] || job.status; actions.append(badge);
      if (job.status === 'queued' || job.status === 'running') { const cancel = document.createElement('button'); cancel.className = 'quiet'; cancel.textContent = '取消'; cancel.addEventListener('click', async e => { e.stopPropagation(); if (confirm('取消任务？已提交给云端的任务可能仍在继续。')) { try { await api(`/api/jobs/${job.id}/cancel`, { method: 'POST', body: '{}' }); await refreshJobs(); } catch (err) { toast(err.message); } } }); actions.append(cancel); }
      card.append(left, actions); root.append(card);
    });
  }
  async function openJob(id) {
    state.selectedJob = id;
    stopDetailPoll();
    try { const data = await api(`/api/jobs/${id}`); renderDetail(data.job); scheduleDetailPoll(id, data.job && data.job.status, isRechecking(data.job)); } catch (err) { toast(err.message); }
  }
  /* 任务详情的运行中轮询：排队/生成中每 3.5s 重拉一次详情，
     单条视频完成状态就能自动跳成「成功」，不用手动刷新。跑完/关面板/切任务即停。
     复查超时（P0）也在轮询：后台线程查云端 + 补下载时，面板要跟着刷新。 */
  const DETAIL_ACTIVE = ['queued', 'running'];
  const isRechecking = (job) => !!(job && job.recheck && job.recheck.running);
  // 面板内容签名：状态 + 每条记录的状态。轮询时签名没变就不重渲染，
  // 避免把面板里正在播放的视频播放器每 3.5 秒重建一次。
  const detailSig = (job) => `${job && job.status}|${isRechecking(job) ? `R${(job.recheck || {}).checked || 0}` : ''}|${((job && job.records) || []).map((r) => `${r.id}:${r.status}`).join(',')}`;
  function stopDetailPoll() { if (state.detailTimer) { clearInterval(state.detailTimer); state.detailTimer = null; } }
  function scheduleDetailPoll(jobId, status, rechecking = false) {
    stopDetailPoll();
    if (!DETAIL_ACTIVE.includes(String(status || '')) && !rechecking) return;
    state.detailTimer = setInterval(async () => {
      if (document.hidden) return;
      // 面板已关 / 已切到别的任务：这轮定时器作废
      if (state.selectedJob !== jobId) { stopDetailPoll(); return; }
      const root = $('job-detail');
      if (!root || root.classList.contains('hidden')) { stopDetailPoll(); return; }
      try {
        const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        if (state.selectedJob !== jobId) { stopDetailPoll(); return; }
        if (detailSig(data.job) !== state.detailSig) renderDetail(data.job);
        if (!DETAIL_ACTIVE.includes(String(data.job && data.job.status)) && !isRechecking(data.job)) stopDetailPoll();
      } catch (_) { /* 轮询失败保留当前画面，下一轮再试 */ }
    }, 3500);
  }
  function renderDetail(job) {
    state.detailStatus = job.status;
    state.detailSig = detailSig(job);
    const root = $('job-detail'); root.classList.remove('hidden'); root.replaceChildren();
    const head = document.createElement('div'); head.className = 'detail-head'; const left = document.createElement('div'); const h = document.createElement('h3'); h.textContent = `任务 ${job.batch_id}`; const p = document.createElement('p'); p.className = 'muted'; p.textContent = job.message || statusText[job.status]; left.append(h, p); const badge = document.createElement('span'); badge.className = `status ${job.status}`; badge.textContent = statusText[job.status] || job.status;
    const side = document.createElement('div'); side.className = 'job-actions';
    const close = document.createElement('button'); close.className = 'quiet btn-sm'; close.type = 'button'; close.textContent = '关闭';
    close.addEventListener('click', () => { root.classList.add('hidden'); state.selectedJob = null; state.detailStatus = null; });
    side.append(badge, close); head.append(left, side); root.append(head);
    const meta = document.createElement('p'); meta.className = 'job-meta'; meta.textContent = `${job.done || 0} / ${job.total || 0} 条完成 · ${job.resolution} · ${job.duration === -1 ? '跟随场景时长' : `${job.duration} 秒`}${job.lane === 'small' ? ' · 快速通道' : ''}`; root.append(meta);
    // 这条任务提交时用的提示词（快照）：批次锁定后改提示词只影响之后的轮次，
    // 所以这里显示的是「当时那份」，不是批次当前的那份。默认折叠，长正文不占屏。
    const runInfo = job.batch_run && job.batch_run.no ? ` · 第 ${job.batch_run.no}/${job.batch_run.total} 批` : '';
    const changed = job.batch_run && job.batch_run.prompt_changed ? ' · 这批改过提示词' : '';
    const promptBox = document.createElement('details'); promptBox.className = 'detail-prompt';
    const promptSum = document.createElement('summary');
    promptSum.textContent = `提示词${job.prompt_id ? ` ${job.prompt_id}` : ''}${runInfo}${changed}`;
    const promptBody = document.createElement('pre'); promptBody.className = 'detail-prompt-body';
    promptBody.textContent = job.prompt || '（这条任务没有记录提示词）';
    const promptCopy = document.createElement('button'); promptCopy.className = 'quiet btn-sm'; promptCopy.type = 'button'; promptCopy.textContent = '复制提示词';
    promptCopy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(promptBody.textContent);
        promptCopy.textContent = '已复制';
        setTimeout(() => { promptCopy.textContent = '复制提示词'; }, 1500);
      } catch (err) { toast('复制失败，请手动选中文本。'); }
    });
    promptBox.append(promptSum, promptBody, promptCopy);
    root.append(promptBox);
    // 1 定妆 + 9 细节：只展示可访问的缩略 URL（后端 public() 不下发服务器路径）。
    const urls = Array.isArray(job.image_urls) ? job.image_urls : [];
    if (urls.length) {
      const strip = document.createElement('div');
      strip.className = 'detail-slots';
      urls.forEach((url, index) => {
        const link = document.createElement('a');
        link.className = 'detail-slot';
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener';
        link.title = (job.image_names && job.image_names[index]) || `图${index + 1}`;
        const img = document.createElement('img');
        img.src = url;
        img.alt = `图${index + 1}`;
        img.loading = 'lazy';
        const badge = document.createElement('span');
        badge.className = `slot-badge${index === 0 ? ' is-primary' : ''}`;
        badge.textContent = index === 0 ? '定妆' : `图${index + 1}`;
        link.append(img, badge);
        strip.append(link);
      });
      root.append(strip);
    }
    if (job.status === 'succeeded' || job.status === 'partial') { const dl = document.createElement('a'); dl.className = 'link-btn'; dl.href = job.download_all; dl.textContent = '下载已完成视频和报告（ZIP）'; root.append(dl); const first = (job.records || []).find(r => r.status === 'success' && r.download_url); if (first) { const player = document.createElement('video'); player.controls = true; player.preload = 'metadata'; player.className = 'preview-video'; player.src = first.download_url; root.append(player); } }
    // 等待超时 / 下载失败（P0）：先按原 taskId 复查云端——云端大多已出片并计费，
    // 下载失败更是「已扣费、只差文件」。直接点「重试」会重新提交、二次扣费。
    // 所以复查按钮放在重试按钮**前面**。
    const recheckable = (job.records || []).filter(r => r.status === 'timeout' || r.download_failed);
    const timeoutCount = recheckable.length;
    const dlCount = recheckable.filter(r => r.download_failed).length;
    const recheck = job.recheck && typeof job.recheck === 'object' ? job.recheck : null;
    if (timeoutCount || (recheck && recheck.running)) {
      const btn = document.createElement('button'); btn.className = 'quiet'; btn.type = 'button';
      if (recheck && recheck.running) {
        btn.textContent = `复查中 ${recheck.checked || 0}/${recheck.total || timeoutCount}`;
        btn.disabled = true;
      } else {
        btn.textContent = `复查补下载（${timeoutCount} 条）`;
        btn.title = '按原 taskId 查询云端：已出片的直接补下载，不重新提交、不二次计费';
        btn.addEventListener('click', async () => {
          if (!confirm(`按原 taskId 复查这 ${timeoutCount} 条记录（其中 ${dlCount} 条是云端已出片、只是下载失败）？\n\n只查询、不重新提交：云端已出片的会直接补下载；云端确实失败的才转成「失败」，那时再重试不会二次计费。`)) return;
          try { const data = await api(`/api/jobs/${job.id}/recheck`, { method: 'POST', body: '{}' }); toast('已开始复查，结果会自动刷新。'); state.selectedJob = job.id; renderDetail(data.job); scheduleDetailPoll(job.id, data.job && data.job.status, true); } catch (err) { toast(err.message); }
        });
      }
      root.append(btn);
    }
    const failedCount = (job.records || []).filter(r => ['failed','invalid','expired','canceled','unknown','timeout','skipped'].includes(r.status)).length;
    if (failedCount && ['partial','failed','canceled'].includes(job.status)) { const retry = document.createElement('button'); retry.className = 'quiet'; retry.textContent = `只重试失败的 ${failedCount} 条`; retry.title = dlCount ? `其中 ${dlCount} 条是「云端已出片、只是下载失败」：重试会二次计费，请先「复查补下载」` : timeoutCount ? '其中含等待超时的条目：建议先「复查补下载」，直接重试会二次计费' : ''; retry.addEventListener('click', async () => { const warn = timeoutCount ? `\n\n注意：其中 ${timeoutCount} 条是等待超时/下载失败——云端可能已出片并计费，直接重试会二次提交。\n建议先点「复查补下载」。` : ''; const skip = dlCount ? `\n其中 ${dlCount} 条「云端已出片、只是下载失败」会被跳过。` : ''; if (!confirm(`只重试失败的 ${failedCount} 条？${warn}${skip}`)) return; try { const data = await api(`/api/jobs/${job.id}/retry`, { method: 'POST', body: '{}' }); const skipped = Number(data.skipped_download_failed) || 0; toast(skipped ? `已重新排队；${skipped} 条「云端已出片、只是下载失败」已跳过（请点「复查补下载」）。` : '失败项已重新排队。'); await refreshJobs(data.job.id); } catch (err) { toast(err.message); } }); root.append(retry); }
    const list = document.createElement('div'); list.className = 'record-list';
    (job.records || []).forEach(rec => { const row = document.createElement('div'); row.className = 'record'; const id = document.createElement('b'); id.textContent = `场景 ${rec.id}`; const info = document.createElement('div'); const stateLine = document.createElement('span'); stateLine.textContent = statusText[rec.status] || rec.status; info.append(stateLine); const verifyWarn = rec.verify && typeof rec.verify === 'object' ? String(rec.verify.warn || '') : ''; const small = document.createElement('small'); small.textContent = [rec.error || rec.archive_warning, verifyWarn, rec.seconds ? `用时 ${rec.seconds} 秒` : ''].filter(Boolean).join(' · '); info.append(small); row.append(id, info); if (rec.download_url) { const a = document.createElement('a'); a.className = 'link-btn'; a.href = rec.download_url; a.textContent = '下载'; row.append(a); } else row.append(document.createElement('span')); list.append(row); }); root.append(list);
    const logs = document.createElement('div'); logs.className = 'log-box'; logs.textContent = (job.log_tail || []).slice(-60).join('\n') || '任务日志将在这里显示。'; root.append(logs);
  }

  // ------------------------------------------------------------ 小批量试跑历史
  // 两处入口共用同一套渲染：小批量页的「历史」子页、历史页的「小批量」分段。
  const SMALL_PAGE = 20;
  function createSmallHistory(listId, moreId) {
    const list = $(listId);
    const more = $(moreId);
    const st = { rows: [], total: 0, scope: 'mine', busy: false };

    function card(job) {
      const node = document.createElement('article');
      node.className = 'job-card';
      const left = document.createElement('div');
      const title = document.createElement('div');
      title.className = 'job-title';
      title.textContent = `试跑 · ${job.total || 0} 条`;
      const meta = document.createElement('div');
      meta.className = 'job-meta';
      const when = job.created_at ? new Date(job.created_at * 1000).toLocaleString() : '';
      const dur = job.duration === -1 ? '跟随场景' : `${job.duration} 秒`;
      meta.textContent = [when, job.resolution, dur, tagSummaryText(job.tags, job.style_no),
        state.user && state.user.role === 'admin' ? job.owner : ''].filter(Boolean).join(' · ');
      left.append(title, meta);
      const actions = document.createElement('div');
      actions.className = 'job-actions';
      const badge = document.createElement('span');
      badge.className = `status ${job.status}`;
      badge.textContent = statusText[job.status] || job.status;
      actions.append(badge);
      node.append(left, actions);
      node.addEventListener('click', () => {
        if (!$('history-view').classList.contains('active-view')) switchView('history-view');
        openJob(job.id);
      });
      return node;
    }

    function render() {
      list.replaceChildren();
      if (!st.rows.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = '还没有试跑记录。上传一张定妆图，试做 1 条就会出现在这里。';
        list.append(empty);
      } else {
        const fragment = document.createDocumentFragment();
        st.rows.forEach(job => fragment.append(card(job)));
        list.append(fragment);
      }
      more.classList.toggle('hidden', !st.rows.length || st.rows.length >= st.total);
      more.disabled = st.busy;
      more.textContent = st.busy ? '加载中…' : '加载更多';
    }

    async function load(reset, scope) {
      if (st.busy) return;
      st.busy = true;
      if (scope) st.scope = scope;
      if (reset) st.rows = [];
      render();
      try {
        const data = await api(`/api/jobs?scope=${st.scope}&lane=small&offset=${reset ? 0 : st.rows.length}&limit=${SMALL_PAGE}`);
        const items = Array.isArray(data.jobs) ? data.jobs : [];
        st.rows = reset ? items : st.rows.concat(items);
        st.total = Number(data.total || st.rows.length);
      } catch (err) { toast(err.message); }
      finally { st.busy = false; render(); }
    }

    more.addEventListener('click', () => load(false));
    return {
      reload: (scope) => load(true, scope),
      loadMore: () => load(false),
    };
  }
  // 历史任务页的「小批量」已并入统一分组视图，这里只剩小批量试跑页自己的历史子页。
  const smallHistories = {
    create: createSmallHistory('create-history-list', 'create-history-more'),
  };

  let createPane = 'make';
  function setCreatePane(pane) {
    createPane = pane === 'history' ? 'history' : 'make';
    document.querySelectorAll('#create-tabs button').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.pane === createPane);
    });
    $('create-pane-make').classList.toggle('hidden', createPane !== 'make');
    $('create-pane-history').classList.toggle('hidden', createPane !== 'history');
    $('create-view').classList.toggle('console-history', createPane === 'history');
    if (createPane === 'history') smallHistories.create.reload('mine');
  }
  document.querySelectorAll('#create-tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      // 试跑历史只在「历史任务 → 小批量」里维护一份，避免两处列表口径不同。
      if (btn.dataset.pane === 'history' && window.BatchUI && window.BatchUI.showSmallHistory) {
        window.BatchUI.showSmallHistory();
        return;
      }
      setCreatePane(btn.dataset.pane);
    });
  });

  /* ------------------------------------------------ 历史「重跑」配置回填
     由 batch.js 的历史行按钮调用：把那一条当时的图片 / 提示词 / 模板视频 /
     款号标签 / 画质时长原样带回本页，然后停下来等使用者修改，绝不自动提交。
     图片只填 upload_id（文件已在服务器上传目录里），提交仍走原有 /api/jobs，
     不会重新上传，也不会凭空多生成一条。 */
  function applyRerun(raw) {
    const c = raw || {};
    const tid = String(c.template_id || '');
    // 后端 selected 模式会校验「场景必须在当前标签筛选结果里」，所以先清筛选。
    document.querySelectorAll('.tag-filter-input').forEach(input => { input.checked = false; });
    state.picked.clear();
    const radio = document.querySelector('input[name="selection"][value="selected"]');
    if (radio) { radio.checked = true; radio.dispatchEvent(new Event('change')); }
    $('count-select').value = '1';
    refreshFilteredTemplates();
    const known = state.filteredTemplates.some(t => String(t.id) === tid);
    if (tid && known) state.picked.add(tid);
    renderPickGrid();

    // 只预填「前缀连续可用」的图：中间缺一张就把后面的全丢掉，
    // 否则图3 会被悄悄顶成图2，模型收到的 imageUrls 顺序就变了。
    const allImages = Array.isArray(c.images) ? c.images : [];
    const usable = [];
    for (const slot of allImages) {
      if (!slot || !slot.available || !slot.upload_id) break;
      usable.push(slot);
    }
    const dropped = allImages.length - usable.length;
    imageSlots.resetSlots(state.images);
    for (const slot of usable) {
      state.images.push({
        file: null, name: slot.name || '定妆图', sizeMb: 0,
        uploadId: slot.upload_id, status: 'done', progress: 100, error: '',
        previewUrl: '', url: slot.url || `/api/uploads/${slot.upload_id}`,
      });
    }
    renderImageSlots();

    const promptSelect = $('prompt-select');
    const promptId = String(c.prompt_id || '');
    if ([...promptSelect.options].some(option => option.value === promptId)) promptSelect.value = promptId;
    $('prompt-body').value = String(c.prompt || promptSelect.selectedOptions[0]?.dataset.body || '');
    updatePromptCount();

    const tags = Object.assign({}, c.tags || {});
    if (!tags.top && c.style_no) tags.top = c.style_no;   // 老任务只有 style_no
    fillTagSlots('style-tags', tags);
    const setSelect = (id, value) => {
      if (value === undefined || value === null || value === '') return;
      const node = $(id), wanted = String(value);
      if (![...node.options].some(option => option.value === wanted)) {
        // 大批量固定 7 秒，而小批量默认只给「5 秒 / 跟随场景」：临时补一个
        // 「原任务」选项，别把 7 秒静默改成 5 秒（那等于换了一条视频）。
        if (id !== 'duration' || !/^\d+$/.test(wanted)) return;
        const option = document.createElement('option');
        option.value = wanted;
        option.textContent = `${wanted} 秒（原任务）`;
        node.append(option);
      }
      node.value = wanted;
    };
    setSelect('resolution', c.resolution);
    setSelect('duration', c.duration);

    updateTemplatePreview();
    updateSummary();
    scheduleStyleUsed();
    state.rerunDupWarnOnce = true;   // 重跑就是明确要重复该场景，免掉一次软提醒

    switchView('create-view');
    setCreatePane('make');
    showCreateError('');

    const notes = [];
    if (!tid || !known) notes.push('原场景已不在候选里，请重新点选一个');
    if (!usable.length) notes.push('原图片已失效，请在槽位 1 重新上传定妆图');
    else if (dropped) notes.push(`另有 ${dropped} 张细节图已失效，需要时请补上`);
    // 管理员跨账户重跑：配置能带过来，但新任务算在当前账号名下，必须说清楚。
    const crossNote = c.cross_account && c.owner
      ? `注意：原任务属于 ${c.owner}，本次将以你自己的账号重新提交（图片沿用原图）。` : '';
    $('image-info').textContent = [
      `重跑预填：${usable.length} 张原图 · 场景 ${tid || '未指定'} · 上次的提示词与参数已带入。`,
      '可以替换图片、改提示词或改场景，确认后点「确认提交」。',
      ...(crossNote ? [crossNote] : []),
      ...notes,
    ].join(' ');
    toast(notes.length ? `已带入上次配置，但${notes.join('；')}`
      : '已带入上次的图片、提示词和场景，修改后点「确认提交」即可重跑。');
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (_) { /* 老浏览器忽略 */ }
  }

  function renderUsage(data) {
    const root = $('usage-table'); root.replaceChildren();
    const accounts = data.accounts || [];
    if (!accounts.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = '暂无用量。'; root.append(empty); return; }
    const table = document.createElement('table');
    const thead = document.createElement('thead'); const hr = document.createElement('tr');
    ['账户', '任务', '条数', '成功', '失败', '计费秒', '预估金额', '最近活跃'].forEach(label => {
      const th = document.createElement('th'); th.textContent = label; hr.append(th);
    });
    thead.append(hr); table.append(thead);
    const tbody = document.createElement('tbody');
    accounts.forEach(row => {
      const tr = document.createElement('tr');
      tr.title = '点击查看该账户跑了哪些视频、各花了多少';
      tr.addEventListener('click', () => openAccount(row.owner));
      const cells = [row.owner, row.jobs, row.items, row.success, row.failed,
        Number(row.billable_seconds || 0) ? Number(row.billable_seconds).toFixed(1) : '—',
        Number(row.estimated_cost || 0) ? `¥${Number(row.estimated_cost).toFixed(2)}` : '—',
        row.last_at ? new Date(row.last_at * 1000).toLocaleDateString() : '—'];
      cells.forEach(value => {
        const td = document.createElement('td'); td.textContent = String(value); tr.append(td);
      });
      tbody.append(tr);
    });
    table.append(tbody);
    const totals = data.totals || {};
    const sum = document.createElement('p'); sum.className = 'muted';
    sum.textContent = `合计 ${totals.items || 0} 条 · 成功 ${totals.success || 0} · 失败 ${totals.failed || 0} · ${Number(totals.billable_seconds || 0).toFixed(1)} 计费秒 · 预估 ¥${Number(totals.estimated_cost || 0).toFixed(2)}`;
    root.append(table, sum);
  }

  const ACCOUNT_PAGE = 100;
  function accountQuery(extra = {}) {
    const params = new URLSearchParams();
    const from = $('account-from').value; if (from) params.set('from', from);
    const to = $('account-to').value; if (to) params.set('to', to);
    const status = $('account-status').value; if (status) params.set('status', status);
    Object.entries(extra).forEach(([key, value]) => params.set(key, value));
    return params;
  }
  async function openAccount(owner) {
    state.usageOwner = owner; state.usageRows = []; state.usageTotal = 0;
    $('account-detail').classList.remove('hidden');
    $('account-title').textContent = `${owner} · 工作明细`;
    $('account-sub').textContent = '逐条列出该账户跑了哪些视频、计费秒与预估金额。';
    await loadAccount(true);
    $('account-detail').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  async function loadAccount(reset) {
    if (!state.usageOwner) return;
    const params = accountQuery({ offset: reset ? 0 : state.usageRows.length, limit: ACCOUNT_PAGE });
    try {
      const data = await api(`/api/admin/accounts/${encodeURIComponent(state.usageOwner)}/work?${params}`);
      state.usageRows = reset ? (data.items || []) : state.usageRows.concat(data.items || []);
      state.usageTotal = Number(data.total || 0);
      renderAccount(data);
    } catch (err) { toast(err.message); }
  }
  function renderAccount(data) {
    $('account-summary').textContent = `共 ${data.total || 0} 条 · 成功 ${data.success || 0} · 失败 ${data.failed || 0} · 进行中 ${data.unfinished || 0} · ${Number(data.total_billable_seconds || 0).toFixed(1)} 计费秒 · 预估 ¥${Number(data.total_cost || 0).toFixed(2)}`;
    const csv = accountQuery();
    $('account-csv').href = `/api/admin/accounts/${encodeURIComponent(state.usageOwner)}/work.csv?${csv}`;

    const root = $('account-table'); root.replaceChildren();
    if (!state.usageRows.length) {
      const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = '这个时间范围内没有记录。';
      root.append(empty);
    } else {
      const table = document.createElement('table');
      const thead = document.createElement('thead'); const hr = document.createElement('tr');
      ['时间', '批次号', '视频', '状态', '计费秒', '金额'].forEach(label => {
        const th = document.createElement('th'); th.textContent = label; hr.append(th);
      });
      thead.append(hr); table.append(thead);
      const tbody = document.createElement('tbody');
      state.usageRows.forEach(item => {
        const tr = document.createElement('tr');
        const when = item.created_at ? new Date(item.created_at * 1000).toLocaleString() : '—';
        const video = item.template_name ? `${item.template_name}（${item.template_id}）` : item.template_id;
        const cells = [when, item.batch_no || '未分组', video,
          statusText[item.status] || item.status || '—',
          item.billable_seconds != null ? Number(item.billable_seconds).toFixed(1) : '—',
          item.estimated_cost != null ? `¥${Number(item.estimated_cost).toFixed(2)}` : '—'];
        cells.forEach(value => { const td = document.createElement('td'); td.textContent = String(value); tr.append(td); });
        tbody.append(tr);
      });
      table.append(tbody); root.append(table);
    }
    const more = $('account-more');
    more.classList.toggle('hidden', state.usageRows.length >= state.usageTotal);
  }

  async function refreshAdmin() {
    if (state.user?.role !== 'admin') return;
    try {
      const data = await api('/api/admin/status'); const root = $('admin-status'); root.replaceChildren();
      [[data.templates, '可用视频场景'], [data.queued, '排队任务'], [data.active, '正在运行'], [`${data.free_gb} GB`, '主机剩余空间']].forEach(([value,label]) => { const box = document.createElement('div'); box.className = 'stat'; const b = document.createElement('b'); b.textContent = value; const s = document.createElement('small'); s.textContent = label; box.append(b,s); root.append(box); });
      const userData = await api('/api/admin/users'); const usersRoot = $('users-list'); usersRoot.replaceChildren();
      (userData.users || []).forEach(account => { const row = document.createElement('div'); row.className = 'record'; const name = document.createElement('b'); name.textContent = account.username;
        const quota = Number(account.quota) || 0; const used = Number(account.used) || 0;
        const info = document.createElement('div'); info.textContent = `${account.role === 'admin' ? '管理员' : '普通使用者'} · ${account.enabled ? '启用' : '已停用'} · 已生成 ${used} 条 / ${quota ? `上限 ${quota}` : '不限'}`; const actions = document.createElement('div'); actions.className = 'record-actions';
        const quotaBtn = document.createElement('button'); quotaBtn.className = 'quiet'; quotaBtn.textContent = '限额'; quotaBtn.title = '设置该账户可生成的视频数量上限';
        quotaBtn.addEventListener('click', async () => { const input = prompt(`为 ${account.username} 设置生成数量上限（条）。\n0 或留空 = 不限。\n\n当前：${quota ? `上限 ${quota} 条` : '不限'} · 已用 ${used} 条`, quota || 0); if (input === null) return; const value = Math.max(0, Math.floor(Number(input) || 0)); try { const res = await api(`/api/admin/users/${encodeURIComponent(account.username)}/quota`, { method: 'POST', body: JSON.stringify({ quota: value }) }); toast(res.note || '上限已更新。'); await refreshAdmin(); } catch (err) { toast(err.message); } });
        actions.append(quotaBtn);
        const reset = document.createElement('button'); reset.className = 'quiet'; reset.textContent = '重置密码'; reset.addEventListener('click', async () => { const password = prompt(`为 ${account.username} 设置新密码（至少 8 位）：`); if (!password) return; try { await api(`/api/admin/users/${encodeURIComponent(account.username)}/reset`, { method: 'POST', body: JSON.stringify({ password }) }); toast('密码已重置，该用户需重新登录。'); } catch (err) { toast(err.message); } }); actions.append(reset);
        if (account.username !== state.user.username) { const toggle = document.createElement('button'); toggle.className = 'quiet'; toggle.textContent = account.enabled ? '停用' : '启用'; toggle.addEventListener('click', async () => { if (account.enabled && !confirm(`停用账号 ${account.username}？`)) return; try { await api(`/api/admin/users/${encodeURIComponent(account.username)}/${account.enabled ? 'disable' : 'enable'}`, { method: 'POST', body: '{}' }); toast(account.enabled ? '账号已停用。' : '账号已启用。'); await refreshAdmin(); } catch (err) { toast(err.message); } }); actions.append(toggle);
          const del = document.createElement('button'); del.className = 'quiet'; del.textContent = '删除'; del.addEventListener('click', async () => { if (!confirm(`确定删除账号 ${account.username}？\n\n删除后该账号无法登录且不可恢复（与其相关的历史任务保留）。`)) return; try { await api(`/api/admin/users/${encodeURIComponent(account.username)}`, { method: 'DELETE' }); toast(`账号 ${account.username} 已删除。`); await refreshAdmin(); } catch (err) { toast(err.message); } }); actions.append(del); }
        row.append(name, info, actions); usersRoot.append(row); });
      state.adminUsers = userData.users || [];
      renderUsage(await api('/api/admin/usage'));
      if (state.usageOwner && !$('account-detail').classList.contains('hidden')) await loadAccount(true);
    } catch (err) { toast(err.message); }
  }
  $('reload-templates').addEventListener('click', async () => { try { const result = await api('/api/admin/reload-templates', { method: 'POST', body: '{}' }); toast(`已加载 ${result.count} 个视频场景`); await bootstrap(); } catch (err) { toast(err.message); } });
  $('add-user-form').addEventListener('submit', async e => { e.preventDefault(); const form = e.currentTarget; const f = new FormData(form); try { await api('/api/admin/users', { method: 'POST', body: JSON.stringify({ username: f.get('username'), password: f.get('password'), role: f.get('role') }) }); form.reset(); toast('账号已添加。'); await refreshAdmin(); } catch (err) { toast(err.message); } });

  ['account-from', 'account-to', 'account-status'].forEach(id => $(id).addEventListener('change', () => loadAccount(true)));
  $('account-more').addEventListener('click', () => loadAccount(false));
  $('account-close').addEventListener('click', () => {
    $('account-detail').classList.add('hidden');
    state.usageOwner = ''; state.usageRows = []; state.usageTotal = 0;
  });
  /* ---------- 管理端 · 云端归档检索与分享 ---------- */
  state.archiveEntries = []; state.archiveLinks = [];
  async function searchArchive() {
    const style = $('archive-style').value.trim();
    if (!style) return toast('请输入款号');
    const btn = $('archive-search-btn');
    btn.disabled = true; btn.textContent = '搜索中…';
    try {
      const data = await api(`/api/admin/archive-search?style=${encodeURIComponent(style)}`);
      state.archiveEntries = data.entries || [];
      renderArchive(data);
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; btn.textContent = '搜索'; }
  }
  function renderArchive(data) {
    const result = $('archive-result');
    const root = $('archive-table');
    root.replaceChildren();
    $('archive-actions').classList.add('hidden');
    $('archive-copy-btn').classList.add('hidden');
    state.archiveLinks = [];
    if (!state.archiveEntries.length) { result.textContent = `云端没有找到与「${data.style}」相关的归档视频。`; return; }
    const src = data.source === 'cos' ? '（按云端路径匹配）' : '';
    result.textContent = `找到 ${state.archiveEntries.length} 个归档视频${src}，默认全选。`;
    const table = document.createElement('table');
    const thead = document.createElement('thead'); const hr = document.createElement('tr');
    ['', '时间', '批次号', '视频文件', '上传者'].forEach(label => { const th = document.createElement('th'); th.textContent = label; hr.append(th); });
    thead.append(hr); table.append(thead);
    const tbody = document.createElement('tbody');
    state.archiveEntries.forEach(entry => {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      const box = document.createElement('input'); box.type = 'checkbox'; box.checked = true; box.dataset.key = entry.key;
      td.append(box); tr.append(td);
      [entry.created_at ? new Date(entry.created_at * 1000).toLocaleString() : '—',
        entry.batch_no || '—', entry.key.split('/').pop(), entry.owner || '—'].forEach(value => {
        const c = document.createElement('td'); c.textContent = String(value); c.title = entry.key; tr.append(c);
      });
      tbody.append(tr);
    });
    table.append(tbody); root.append(table);
    $('archive-actions').classList.remove('hidden');
  }
  $('archive-search-btn').addEventListener('click', searchArchive);
  $('archive-style').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); searchArchive(); } });
  $('archive-sign-btn').addEventListener('click', async () => {
    const keys = [...$('archive-table').querySelectorAll('input[type="checkbox"]:checked')].map(box => box.dataset.key);
    if (!keys.length) return toast('请先勾选视频');
    const btn = $('archive-sign-btn');
    btn.disabled = true; btn.textContent = '正在生成…';
    try {
      const data = await api('/api/admin/archive-sign', { method: 'POST', body: JSON.stringify({ keys }) });
      state.archiveLinks = data.links || [];
      if ((data.failed || []).length) toast(`${data.failed.length} 个生成失败，其余已生成。`);
      else toast(`已生成 ${state.archiveLinks.length} 条链接，7 天内有效。`);
      $('archive-copy-btn').classList.toggle('hidden', !state.archiveLinks.length);
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; btn.textContent = '生成下载链接（7 天有效）'; }
  });
  $('archive-copy-btn').addEventListener('click', async () => {
    const text = state.archiveLinks.map(link => link.url).join('\n');
    try { await copyText(text); toast(`已复制 ${state.archiveLinks.length} 条链接。`); }
    catch (_) { toast('复制失败，请手动选择链接文本。'); }
  });

  // 供 batch.js 复用：同一套 api 封装、共享 state、同一个详情面板。
  window.OutfitApp = { api, toast, state, switchView, openJob, refreshFlatJobs, statusText,
    smallHistory: (id) => smallHistories[id], setCreatePane, applyRerun,
    collectTagSlots, fillTagSlots, tagSummaryText, TAG_LABELS,
    // 标签页改完标签/弃用后要刷新小批量页的模板下拉，否则仍能选到已弃用的场景
    reloadTemplates: loadTemplates,
    // 大批量页复用同一套 10 槽位组件与并发上传
    imageSlots: window.ImageSlots };
  // Esc：先关任务详情，再关账户明细，符合"从最上层开始退"的直觉。
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const detail = $('job-detail');
    if (detail && !detail.classList.contains('hidden')) {
      detail.classList.add('hidden'); state.selectedJob = null; state.detailStatus = null; return;
    }
    const account = $('account-detail');
    if (account && !account.classList.contains('hidden')) $('account-close').click();
  });
  async function init() { fetch('/healthz', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(d => { if (d && d.dev) markDev(true); }).catch(() => {}); try { const data = await api('/api/me'); state.user = data.user; await bootstrap(); } catch (_) { showLogin(); } }
  init();
})();
