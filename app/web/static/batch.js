/*
 * 大批量生产 + 历史任务分组（原生 JS，无框架）
 * 依赖 app.js 暴露的 window.OutfitApp：{ api, toast, state, switchView, openJob, refreshFlatJobs, statusText }
 *
 * 口径说明：
 * - `duration` 就是输出秒数，本页固定传 7。后端 web 层与 wan_api 已统一为
 *   `duration_ok()`（-1 或 2–30），7 合法且是 POST /api/batches 的默认值。
 * - 本页不展示任何金额/费用信息；花费统一在「管理 → 账户用量」里按账户核算。
 *   生产按钮的档位仍由后端 cost_estimate.options[i].count 决定（配置改了上限也能跟上），
 *   只是不再读取 amount。
 */
(() => {
  'use strict';
  const App = window.OutfitApp;
  if (!App) return;
  const { api, toast } = App;
  const $ = (id) => document.getElementById(id);

  const BATCH_DURATION_PARAM = 7;    // duration = 输出秒数，本页固定 7
  const OUTPUT_SECONDS = BATCH_DURATION_PARAM;
  const PRODUCE_STEPS = [5, 10, 20, 50, 100];
  // 最小生成单位：档位下限（服务端 produce_min 优先）。低于它只有一种情况放行——清单就剩这么多。
  const PRODUCE_MIN_FALLBACK = 5;
  // 候选分页：每页条数 = 实测容量（列数 × 能完整放下的行数），**保底 50**。
  // 候选区现在与右列等高（见 batch.css），实测 1920×880 候选网格约 654px 高 · 6 列 × 14 行 = 84 条/页，
  // 1920×990 是 6 × 16 = 96 条/页 —— 一页正好填满整块区域，不再有「分页条下面空 280px」的观感。
  // 保底 50 是为了手机/平板（那里候选区交回页面滚动，无法量容量）和极端矮屏不至于每页只剩几条。
  // 一次把筛选结果全拉到前端（接口 limit 上限 1000），翻页纯本地切片，不再发请求。
  const CAND_PAGE_MIN = 50;
  const CAND_FETCH_LIMIT = 500;
  const ITEM_PAGE = 60;
  const HISTORY_PAGE = 200;
  const POLL_MS = 4000;

  const ITEM_STATE_TEXT = { pending: '待生产', producing: '生产中', done: '已完成', failed: '失败' };
  const BATCH_STATE_TEXT = { draft: '草稿', locked: '已锁定', closed: '已关闭' };

  const b = {
    mode: 'list',            // list | new | view
    batches: [], listLoaded: false,
    current: null, items: [],
    candidates: [], selected: new Set(), candTotal: 0, candReady: 0,
    // 评分筛选 / 排序：candRatingMin=0 不过滤；candSort='' 默认顺序、'rating' 评分高→低
    candRatingMin: 0, candSort: '',
    candTagFilter: new Set(), onlySelected: false, candPage: 1, candPageItems: [], candPageBackup: 0,
    // 每页条数由 candCapacity() 实测写入（列数 × 行数，保底 CAND_PAGE_MIN），页面尺寸变了就重算
    candPageSize: CAND_PAGE_MIN, candObserver: null,
    tagFilters: {}, tagPanelOpen: false, formOpen: true,
    previewId: '', resizeRaf: 0,
    itemFilter: 'all', itemLimit: ITEM_PAGE, itemsSignature: '',
    busy: false, pollTimer: null, activeJob: null,
    promptEditing: false,    // 锁定摘要里「修改提示词」的编辑态；切批次/退出时复位
    promptDraft: '',         // 编辑中的草稿文本（与 batch.prompt 分离，避免误覆盖）
    promptOpen: false,       // 提示词是否展开全文（默认折叠两行，页面更紧凑；4 秒轮询重渲染也靠它保持）
    groups: [], openGroups: new Set(), groupDetails: new Map(), groupSig: new Map(),
    historyMode: 'groups', flatWarned: false,
    styleUsed: {},           // 当前批次上衣款号已用过的模板（{id: {batch_no,...}}），防重标记
  };

  /* ---------- 小工具 ---------- */
  const num = (value, fallback = 0) => { const n = Number(value); return Number.isFinite(n) ? n : fallback; };
  const txt = (value) => (value === undefined || value === null ? '' : String(value));
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function fmtTime(ts) {
    const n = Number(ts);
    return Number.isFinite(n) && n > 0 ? new Date(n * 1000).toLocaleString() : '—';
  }
  // 档位与上限以 /api/bootstrap 的 service 为准（max_batch_items / max_batch_run_items / produce_choices），
  // 这里的常量只在服务端没给时兜底。
  function serviceLimits() {
    const svc = App.state.service || {};
    const choices = Array.isArray(svc.produce_choices)
      ? svc.produce_choices.map((value) => Number(value)).filter((value) => Number.isFinite(value) && value > 0)
      : [];
    return {
      choices: choices.length ? choices : PRODUCE_STEPS,
      maxBatchItems: Math.min(1000, num(svc.max_batch_items, 500) || 500),
      maxRunItems: num(svc.max_batch_run_items, PRODUCE_STEPS[PRODUCE_STEPS.length - 1]) || PRODUCE_STEPS[PRODUCE_STEPS.length - 1],
      produceMin: Math.max(1, num(svc.produce_min, PRODUCE_MIN_FALLBACK) || PRODUCE_MIN_FALLBACK),
    };
  }
  // 生产条数的合法区间：上限同时受「单次上限」与「清单剩余」钳制；
  // 剩余不足最小单位时下限降到 1（否则零头永远提交不出去）。
  function produceRange(batch) {
    const limits = serviceLimits();
    if (!batch) return { pending: 0, cap: 0, min: limits.produceMin };
    const total = num(batch.total_items, b.items.length);
    const pending = Math.max(0, num(batch.pending, total - num(batch.done) - num(batch.failed)));
    const cap = Math.min(limits.maxRunItems, pending);
    const min = cap < limits.produceMin ? Math.min(1, cap) : limits.produceMin;
    return { pending, cap, min };
  }
  // 生产按钮的档位：优先后端 cost_estimate.options 的 count（配置改了单次上限也能跟上），
  // 没有时就退回本地三档。本页不展示金额，只取档位。
  function produceCounts(batch) {
    const est = batch && batch.cost_estimate;
    if (est && Array.isArray(est.options) && est.options.length) {
      const counts = est.options
        .filter((option) => num(option.count) > 0)
        .map((option) => num(option.count));
      if (counts.length) return { source: 'server', counts };
    }
    return { source: 'local', counts: serviceLimits().choices };
  }
  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('无法读取图片，请重新选择。'));
      reader.readAsDataURL(file);
    });
  }
  function scopeParam() {
    const user = App.state.user;
    if (!(user && user.role === 'admin' && App.state.showAll)) return 'mine';
    // 管理员"查看全部"时再叠加账户维度；普通账号永远只能看到自己。
    const owner = App.state.ownerFilter ? `&owner=${encodeURIComponent(App.state.ownerFilter)}` : '';
    return `all${owner}`;
  }
  function ownerParam() {
    const user = App.state.user;
    return user && user.role === 'admin' && App.state.ownerFilter
      ? `&owner=${encodeURIComponent(App.state.ownerFilter)}` : '';
  }
  function openJobDetail(jobId) {
    if (!jobId) return;
    App.switchView('history-view');
    App.openJob(jobId);
  }

  /* ---------- 步骤条与面板显隐 ---------- */
  function renderStepper(active) {
    document.querySelectorAll('.batch-step').forEach((node, index) => {
      node.classList.toggle('is-done', active >= 0 && index < active);
      node.classList.toggle('is-active', index === active);
      if (index === active) node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
    });
  }
  function show(panelId, visible) { $(panelId).classList.toggle('hidden', !visible); }

  function renderBatch() {
    const batch = b.current;
    if (b.mode === 'list' || (!batch && b.mode !== 'new')) {
      show('batch-work', false);
      $('batch-list').classList.remove('hidden');
      renderStepper(-1);
      renderBatchList();
      updateExitButton();
      return;
    }
    $('batch-list').classList.add('hidden');
    show('batch-work', true);

    const creating = b.mode === 'new';
    const batchState = txt(batch && batch.state) || 'draft';
    const locked = !creating && (batchState === 'locked' || batchState === 'closed');

    show('batch-step-a', true);
    show('batch-step-b', !creating && !locked);
    show('batch-step-c', locked);
    show('batch-step-d', locked);
    setFormFrozen(!creating);
    setBatchFormOpen(creating ? true : b.formOpen);
    updateExitButton();
    if (creating) {
      renderStepper(0);
    } else {
      fillBatchForm(batch);
      if (locked) {
        renderStepper(3);
        renderSummary(batch);
        renderProduce(batch);
        renderItems();
      } else {
        renderStepper(1);
        updateBatchCounts();
      }
    }
    // 面板刚显隐完，等一帧再量高度（候选区高度跟着页头/步骤条/工具条的实际高度走），
    // 顺带核一遍每页条数：面板从隐藏变可见时网格才第一次有真实高度
    queueCandLayout();
  }

  /* ---------- 退出批次（界面导航，不动服务端数据） ----------
     草稿态：步骤 1 卡片上的「退出当前批次计划」——把界面退回批次列表，批次仍是 draft，
     冻结的设置、定妆图、提示词都在；本页内存里的勾选会清空（跨页选中本来就是本地状态），
     下次从列表点「继续筛选」重新筛一遍接着做。
     已锁定/已关闭：步骤 3 / 4 卡片上的「退出批次」——同样是纯界面导航，清单和进度都保留。 */
  function updateExitButton() {
    const draftBtn = $('batch-exit-btn');
    const state = txt(b.current && b.current.state) || 'draft';
    const draftVisible = b.mode === 'view' && !!b.current && !!b.current.id && state === 'draft';
    if (draftBtn) {
      draftBtn.classList.toggle('hidden', !draftVisible);
      draftBtn.disabled = !draftVisible || b.busy;
    }
    // 锁定/关闭态的退出按钮：与草稿同款交互，只是文案不带「计划」，也不清本地勾选
    const viewVisible = b.mode === 'view' && !!b.current && !!b.current.id && state !== 'draft';
    for (const id of ['batch-c-exit-btn', 'batch-d-exit-btn']) {
      const btn = $(id);
      if (!btn) continue;
      btn.classList.toggle('hidden', !viewVisible);
      btn.disabled = !viewVisible || b.busy;
    }
  }
  function exitBatch() {
    if (b.mode !== 'view' || !b.current || b.busy) return;
    const state = txt(b.current.state) || 'draft';
    b.mode = 'list';
    b.current = null;
    b.items = [];
    b.itemsSignature = '';
    b.activeJob = null;
    b.promptEditing = false;                 // 编辑态不跨批次残留
    stopPoll();
    resetCandidates();
    resetItemsView();
    renderBatch();
    loadBatches(true);
    window.scrollTo(0, 0);
    toast(state === 'draft'
      ? '已退出这个批次，草稿仍然保留；点「继续筛选」可以接着做。'
      : '已退回批次列表，批次数据不变。');
  }

  /* ---------- 步骤 A：建批次 ---------- */
  function setFormFrozen(frozen) {
    ['batch-no', 'batch-title', 'batch-image-file', 'batch-prompt-select', 'batch-prompt-body', 'batch-resolution'].forEach((id) => {
      const node = $(id);
      if (node) node.disabled = frozen;
    });
    // 6 栏位标签随批次冻结：整排一起禁用/解禁
    document.querySelectorAll('#batch-tags input[data-tag-slot]').forEach((input) => { input.disabled = frozen; });
    $('batch-create-btn').disabled = frozen;
    $('batch-drop-zone').classList.toggle('is-frozen', frozen);
    $('batch-a-hint').textContent = frozen
      ? '批次级设置已冻结：提示词、画质、时长、定妆图建立后不可改；要改请新建批次。'
      : '提示词、画质、时长在批次级冻结；建好后要改只能新建批次。';
  }
  // 已有批次：折叠冻结的建批次表单（省下一整屏）；新建批次时必须是展开的。
  function setBatchFormOpen(open) {
    const card = $('batch-step-a');
    const body = $('batch-a-body');
    const btn = $('batch-a-toggle');
    const summary = $('batch-a-summary');
    if (!card || !body || !btn) return;
    const collapsible = b.mode !== 'new';
    const collapsed = collapsible && !open;
    b.formOpen = !collapsed;
    card.classList.toggle('is-collapsed', collapsed);
    body.classList.toggle('hidden', collapsed);
    if (summary) summary.classList.toggle('hidden', !collapsed);
    btn.classList.toggle('hidden', !collapsible);
    btn.textContent = collapsed ? '展开设置' : '收起设置';
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    queueCandLayout();
  }
  function fillBatchForm(batch) {
    $('batch-no').value = txt(batch.batch_no);
    // 6 栏位标签：老批次只有 style_no 时映射到「上衣」栏位显示。
    const legacy = !batch.tags && txt(batch.style_no) ? { top: txt(batch.style_no) } : batch.tags;
    App.fillTagSlots('batch-tags', legacy || {});
    $('batch-title').value = txt(batch.title);
    if (txt(batch.resolution)) $('batch-resolution').value = txt(batch.resolution);
    const promptId = txt(batch.prompt_id);
    if (promptId && [...$('batch-prompt-select').options].some((option) => option.value === promptId)) {
      $('batch-prompt-select').value = promptId;
    }
    if (txt(batch.prompt)) $('batch-prompt-body').value = txt(batch.prompt);
    updateBatchPromptCount();
    const imageName = txt(batch.image_name);
    const imageUrl = txt(batch.image_url);
    $('batch-image-info').textContent = imageName ? `定妆图：${imageName}` : '这个批次还没有绑定定妆图。';
    const durationValue = batch.duration === null || batch.duration === undefined ? BATCH_DURATION_PARAM : num(batch.duration, BATCH_DURATION_PARAM);
    // 收起状态下这一行就是「这个批次是什么」的全部信息
    $('batch-a-summary').textContent = [
      txt(batch.batch_no) || '未命名批次',
      App.tagSummaryText(batch.tags, txt(batch.style_no)) || '',
      txt(batch.title) || '',
      txt(batch.resolution) || '—',
      durationValue > 0 ? `输出 ${durationValue} 秒` : '输出跟随素材时长',
      imageName ? `定妆图 ${imageName}` : '未绑定定妆图',
    ].filter(Boolean).join(' · ');
    const preview = $('batch-image-preview');
    if (imageUrl) {
      // 批次里存的定妆图，走 /api/uploads/{id}（后端只给 owner/admin 看）
      preview.src = imageUrl;
      preview.alt = imageName ? `定妆图：${imageName}` : '定妆图';
      preview.classList.remove('hidden');
    } else {
      preview.classList.add('hidden');
      preview.removeAttribute('src');
    }
    $('batch-drop-copy').classList.toggle('hidden', !!(imageUrl || imageName));
  }
  function resetBatchForm() {
    $('batch-no').value = '';
    App.fillTagSlots('batch-tags', {});
    $('batch-title').value = '';
    $('batch-prompt-body').value = '';
    $('batch-image-info').textContent = '';
    $('batch-image-file').value = '';
    const preview = $('batch-image-preview');
    preview.classList.add('hidden');
    preview.removeAttribute('src');
    $('batch-drop-copy').classList.remove('hidden');
    const promptSelect = $('batch-prompt-select');
    if (promptSelect && promptSelect.options.length) {
      promptSelect.selectedIndex = 0;
      $('batch-prompt-body').value = promptSelect.selectedOptions[0]?.dataset.body || '';
    }
    updateBatchPromptCount();
    clearBatchError();
    setFormFrozen(false);
  }
  function updateBatchPromptCount() {
    $('batch-prompt-length').textContent = `${$('batch-prompt-body').value.length} / 4000`;
  }
  function showBatchError(text) {
    const node = $('batch-create-error');
    node.textContent = text;
    node.classList.toggle('hidden', !text);
  }
  function clearBatchError() { showBatchError(''); }

  async function createBatch() {
    if (b.busy) return;
    const batchNo = $('batch-no').value.trim();
    const title = $('batch-title').value.trim();
    const promptBody = $('batch-prompt-body').value.trim();
    const promptId = $('batch-prompt-select').value;
    const resolution = $('batch-resolution').value;
    const file = $('batch-image-file').files[0];
    clearBatchError();
    if (batchNo.length < 1 || batchNo.length > 40) { showBatchError('批次号必填，1–40 个字符。'); $('batch-no').focus(); return; }
    if (!file) { showBatchError('请先上传定妆图。'); return; }
    if (!promptId) { showBatchError('请选择提示词模板。'); return; }
    if (!promptBody) { showBatchError('提示词不能为空，请选择模板或填写内容。'); return; }
    if (promptBody.length > 4000) { showBatchError('提示词最多 4000 个字符。'); return; }

    const btn = $('batch-create-btn');
    b.busy = true; btn.disabled = true; btn.textContent = '正在上传定妆图…';
    try {
      const uploaded = await api('/api/uploads', { method: 'POST', body: JSON.stringify({ filename: file.name, data: await readFile(file) }) });
      btn.textContent = '正在创建批次…';
      const tags = App.collectTagSlots('batch-tags');
      if (!tags.top) { showBatchError('请填写「上衣」款号（云端归档路径按它命名）。'); return; }
      const payload = { batch_no: batchNo, title, tags, prompt_id: promptId, prompt: promptBody, resolution, duration: BATCH_DURATION_PARAM, upload_id: uploaded.upload_id };
      const data = await api('/api/batches', { method: 'POST', body: JSON.stringify(payload) });
      if (!data.batch || !data.batch.id) throw new Error('服务端没有返回批次信息，请刷新后重试。');
      b.current = data.batch;
      b.mode = 'view';
      b.formOpen = false;   // 建好就收起冻结的设置，直接看候选
      resetCandidates();
      toast(`批次 ${txt(b.current.batch_no) || batchNo} 已建好，接着筛候选。`);
      renderBatch();
      await fetchCandidates();
    } catch (err) {
      showBatchError(err.message);
      toast(err.message);
    } finally {
      b.busy = false;
      btn.disabled = false;
      btn.textContent = '创建批次';
      renderBatch();
    }
  }

  /* ---------- 定妆图上传（复用现有交互与校验口径） ---------- */
  function bindDropZone() {
    const input = $('batch-image-file');
    const zone = $('batch-drop-zone');
    input.addEventListener('change', () => previewBatchFile(input.files[0]));
    zone.addEventListener('dragover', (event) => { event.preventDefault(); if (!zone.classList.contains('is-frozen')) zone.classList.add('drag'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
    zone.addEventListener('drop', (event) => {
      event.preventDefault();
      zone.classList.remove('drag');
      if (zone.classList.contains('is-frozen')) return;
      const file = event.dataTransfer.files[0];
      if (!file) return;
      input.files = event.dataTransfer.files;
      previewBatchFile(file);
    });
  }
  function previewBatchFile(file) {
    $('batch-image-info').textContent = '';
    if (!file) return;
    const okType = /\.(jpe?g|png|webp|bmp)$/i.test(file.name);
    if (!okType || file.size > 20 * 1024 * 1024) {
      $('batch-image-info').textContent = '请选择 JPG、PNG 或 WebP 图片，大小不超过 20MB。iPhone HEIC 请先另存为 JPG。';
      $('batch-image-file').value = '';
      return;
    }
    const url = URL.createObjectURL(file);
    $('batch-image-preview').src = url;
    $('batch-image-preview').classList.remove('hidden');
    $('batch-drop-copy').classList.add('hidden');
    $('batch-image-info').textContent = `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`;
  }

  /* ---------- 提示词 ---------- */
  function fillPromptSelect() {
    const select = $('batch-prompt-select');
    if (!select) return;
    const prompts = App.state.prompts || [];
    const previous = select.value;
    select.replaceChildren();
    for (const prompt of prompts) {
      const option = document.createElement('option');
      option.value = prompt.id;
      option.textContent = prompt.title;
      option.dataset.body = prompt.body || '';
      select.append(option);
    }
    if (previous && [...select.options].some((option) => option.value === previous)) select.value = previous;
    if (b.mode === 'new') {
      const first = select.selectedOptions[0];
      $('batch-prompt-body').value = first ? (first.dataset.body || '') : '';
    }
    updateBatchPromptCount();
  }

  /* ---------- 步骤 B：标签筛选与候选 ---------- */
  function renderBatchTagFilters() {
    const root = $('batch-tag-groups');
    if (!root) return;
    const st = App.state;
    const categories = (st.tagCategories && st.tagCategories.length) ? st.tagCategories : Object.keys(st.tagValues || {});
    const tagValues = st.tagValues || {};
    const hasAny = categories.some((category) => (tagValues[category] || []).length);
    // 标签面板默认收起：候选区要靠这一百多像素挤进一屏，展开交给「标签筛选」按钮。
    $('batch-tag-card').dataset.hasTags = hasAny ? '1' : '0';
    if (!hasAny) b.tagPanelOpen = false;
    applyTagPanel();
    root.replaceChildren();
    if (!hasAny) return;
    for (const category of categories) {
      const tags = tagValues[category] || [];
      if (!tags.length) continue;
      const group = el('fieldset', 'tag-filter-group');
      group.append(el('legend', null, category));
      const wrap = el('div', 'tag-filter-options');
      for (const tag of tags) {
        const label = el('label', 'tag-filter-option');
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.className = 'batch-tag-input';
        input.dataset.category = category;
        input.value = tag;
        input.checked = (b.tagFilters[category] || []).includes(tag);
        input.addEventListener('change', () => {
          b.tagFilters = collectBatchTagFilters();
          scheduleCandidatesRefresh();
        });
        label.append(input, el('span', null, tag));
        wrap.append(label);
      }
      group.append(wrap);
      root.append(group);
    }
  }
  function collectBatchTagFilters() {
    const filters = {};
    document.querySelectorAll('#batch-tag-groups .batch-tag-input:checked').forEach((input) => {
      (filters[input.dataset.category] || (filters[input.dataset.category] = [])).push(input.value);
    });
    return filters;
  }
  function syncTagInputs() {
    document.querySelectorAll('#batch-tag-groups .batch-tag-input').forEach((input) => {
      input.checked = (b.tagFilters[input.dataset.category] || []).includes(input.value);
    });
  }
  // 标签面板开合：收起时只留工具条上的按钮，候选区就能多吃几行。
  function applyTagPanel() {
    const card = $('batch-tag-card');
    const btn = $('batch-tag-toggle');
    if (!card || !btn) return;
    const hasTags = card.dataset.hasTags === '1';
    const open = hasTags && b.tagPanelOpen;
    card.classList.toggle('hidden', !open);
    btn.disabled = !hasTags;
    btn.classList.toggle('is-on', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.title = hasTags ? (open ? '收起标签筛选' : '展开标签筛选') : '模板还没有标签可选';
    queueCandLayout();
  }
  function toggleTagPanel() {
    b.tagPanelOpen = !b.tagPanelOpen;
    applyTagPanel();
  }
  function resetCandidates() {
    b.candidates = [];
    b.selected = new Set();
    b.candTagFilter.clear();
    b.onlySelected = false;
    b.candPage = 1;
    b.candPageItems = [];
    b.candPageBackup = 0;
    b.candTotal = 0; b.candReady = 0;
    if ($('batch-only-selected')) $('batch-only-selected').checked = false;
    clearPreview();
    renderCandChips();
    renderCandidates();
    $('batch-tag-summary').textContent = '';
    $('batch-cand-note').classList.add('hidden');
  }

  async function refreshStyleUsed(batch) {
    const top = txt((batch && batch.tags && batch.tags.top) || (batch && batch.style_no) || '');
    if (!top) { b.styleUsed = {}; return; }
    try {
      const data = await api(`/api/style-usage?top=${encodeURIComponent(top)}`);
      b.styleUsed = data.used || {};
    } catch { b.styleUsed = {}; }
  }

  async function fetchCandidates() {
    const batch = b.current;
    if (!batch || !batch.id) return;
    await refreshStyleUsed(batch);   // 先查该款号已用模板，候选行才能挂「已用」标记
    const btn = $('batch-candidates-btn');   // 按钮已移除，保留空值兜底以兼容旧调用点
    b.tagFilters = collectBatchTagFilters();
    if (btn) { btn.disabled = true; btn.textContent = '正在筛选…'; }
    $('batch-tag-summary').textContent = '正在匹配候选…';
    try {
      const data = await api(`/api/batches/${encodeURIComponent(batch.id)}/candidates`, {
        method: 'POST',
        body: JSON.stringify({
          tag_filters: b.tagFilters,
          limit: CAND_FETCH_LIMIT,
          rating_min: b.candRatingMin,
          sort: b.candSort,
        }),
      });
      const items = Array.isArray(data.items) ? data.items : [];
      b.candidates = items.map((item) => ({
        id: txt(item.id),
        name: txt(item.name) || `场景 ${txt(item.id)}`,
        tags: Array.isArray(item.tags) ? item.tags.map(txt) : [],
        ready: item.ready !== false,
        rating: Math.min(5, Math.max(0, num(item.rating, 0))),
      }));
      // filtered_total 是「筛选命中总数」（可能大于本次返回条数），total 只是这一批的条数。
      b.candTotal = num(data.filtered_total, num(data.total, b.candidates.length));
      b.candReady = num(data.ready_count, b.candidates.filter((item) => item.ready).length);
      // 默认不选中任何项：分页后「已选 480」与第一页可见的 50 张极易混淆，
      // 用户若误以为 480 是本页数量，锁定后连点几次「生产 100」会意外产生数千元费用。
      // 想全做就点一次「全选全部 N 条」，多一步换来不会误操作。
      b.selected = new Set();
      b.candTagFilter.clear();
      b.onlySelected = false;
      $('batch-only-selected').checked = false;
      b.candPage = 1;              // 换了筛选条件就回到第 1 页
      b.candPageBackup = 0;
      const matched = b.candTotal || b.candidates.length;
      $('batch-tag-summary').textContent = `匹配 ${matched} 个场景 · 可生产 ${b.candReady} 个`;
      if (b.candidates.length < matched) {
        showCandNote(`服务端只返回了前 ${b.candidates.length} 条候选（共 ${matched} 条），分页只覆盖返回的这批，请再用标签收窄范围。`);
      } else if (!b.candReady) {
        showCandNote('候选都还没上传到云端，现在只能预览，不能锁定生产。');
      } else {
        showCandNote('');
      }
      renderCandChips();
      renderCandidates();
      if (!b.candidates.length) toast('当前筛选没有匹配的场景，换一组标签试试。');
    } catch (err) {
      b.candidates = [];
      b.selected = new Set();
      renderCandChips();
      renderCandidates();
      $('batch-tag-summary').textContent = err.message;
      showCandNote(err.message);
      toast(err.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '刷新候选'; }
    }
  }

  /* 标签筛选改动后自动重匹配：300ms 去抖，连点多个标签只发一次请求 */
  let candRefreshTimer = null;
  function scheduleCandidatesRefresh() {
    clearTimeout(candRefreshTimer);
    candRefreshTimer = setTimeout(() => { fetchCandidates().catch(() => {}); }, 300);
  }

  /* 当前筛选命中的候选（只受标签快筛影响，不受「只看已选」影响）。
     「全选全部 N 条」的 N 就取这里的长度 —— 是筛选后的真实条数，不是 500 这个拉取上限。 */
  function candidateMatched() {
    let list = b.candidates;
    if (b.candTagFilter.size) {
      const wanted = [...b.candTagFilter];
      list = list.filter((item) => wanted.every((tag) => item.tags.includes(tag)));
    }
    return list;
  }
  function candidateVisible() {
    const list = candidateMatched();
    return b.onlySelected ? list.filter((item) => b.selected.has(item.id)) : list;
  }
  function candPageCount(total, size) {
    const per = Math.max(1, num(size, b.candPageSize) || CAND_PAGE_MIN);
    return total > 0 ? Math.ceil(total / per) : 1;
  }

  /* ---------- 每页条数：按候选网格的实测容量算 ----------
     列数从 getComputedStyle(grid).gridTemplateColumns 解析（auto-fill 解析完就是一串 px 轨道）；
     行数 = floor((可用高 + 行距) / (卡片高 + 行距))；卡片高优先量现存卡片，
     一张卡都没有时造一张同结构的隐藏探针卡，量完即删 —— 不写死 39px，字号/内距改了也不会错。
     ≤900px（手机/平板）候选网格交给页面滚动，没有「一屏内放几行」这回事，按保底条数分页。 */
  function candColumnCount(grid, style) {
    const raw = String(style.gridTemplateColumns || '').trim();
    const tracks = raw ? raw.split(/\s+/) : [];
    if (tracks.length && tracks.every((track) => /\d(px|%)?$/.test(track))) return tracks.length;
    // 兜底：解析不出轨道就按「容器宽 / 最小列宽」估，190px 是桌面 auto-fill 的下限
    const gap = parseFloat(style.columnGap) || 5;
    return Math.max(1, Math.floor((grid.clientWidth + gap) / (190 + gap)));
  }
  function candCardHeight(grid, width) {
    const existing = grid.querySelector('.batch-cand');
    if (existing) {
      const measured = existing.getBoundingClientRect().height;
      if (measured > 0) return measured;
    }
    // 探针卡：结构与真卡完全一致（同一个 candidateTile），绝对定位到网格外，量完立刻删，不参与布局
    const probe = candidateTile({ id: '__probe__', name: '探针卡片', tags: ['探针'], ready: true });
    probe.setAttribute('aria-hidden', 'true');
    probe.style.cssText = `position:absolute;left:-9999px;top:0;visibility:hidden;pointer-events:none;width:${Math.max(40, width)}px`;
    grid.append(probe);
    const measured = probe.getBoundingClientRect().height;
    probe.remove();
    return measured;
  }
  function candCapacity() {
    const grid = $('batch-cand-grid');
    if (!grid) return CAND_PAGE_MIN;
    if (window.innerWidth <= 900) return CAND_PAGE_MIN;
    const style = getComputedStyle(grid);
    const gap = parseFloat(style.rowGap) || 0;
    const cols = candColumnCount(grid, style);
    const paddingY = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
    const avail = grid.clientHeight - paddingY;
    // 列宽只用来给探针定宽；卡片高与宽度无关（文件名/标签都单行省略），不必精确到亚像素
    const cardH = candCardHeight(grid, grid.clientWidth / cols);
    if (!(avail > 0) || !(cardH > 0)) return CAND_PAGE_MIN;
    const rows = Math.max(1, Math.floor((avail + gap) / (cardH + gap)));
    return Math.max(CAND_PAGE_MIN, cols * rows);
  }

  /* 候选网格：本地分页，一次渲染一整页（条数 = 实测容量，保底 50 条）。
     **顺序不能反**：先定高度 → 再算容量 → 再切片（容量是按网格实际高度算出来的）。
     翻页不重建整份列表，只换这一页的卡片；b.selected 是跨页唯一的选中事实来源。 */
  function renderCandidates(settling) {
    const grid = $('batch-cand-grid');
    // 1) 高度：候选区已被 CSS 拉伸到与右列等高，这里只把「至少到视口底部」写进 --batch-cand-h 当下限
    syncCandHeight();
    // 2) 容量：网格实际高度 + 卡片实际高度 → 这一页放得下几条
    const size = candCapacity();
    b.candPageSize = size;
    const visible = candidateVisible();
    const pageCount = candPageCount(visible.length, size);
    // 筛选 / 反选 / 取消选中 / 视口变高都会让页数变少：夹住页码，别停在一页空网格上
    b.candPage = Math.min(Math.max(1, num(b.candPage, 1)), pageCount);
    const start = (b.candPage - 1) * size;
    const slice = visible.slice(start, start + size);
    b.candPageItems = slice;
    const fragment = document.createDocumentFragment();
    for (const item of slice) fragment.append(candidateTile(item));
    grid.replaceChildren(fragment);
    if (!visible.length) {
      grid.append(el('div', 'empty batch-cand-empty',
        b.candidates.length ? '当前过滤条件下没有候选，清掉「只看已选」或标签定位试试。' : '还没有候选，选好标签后会自动匹配。'));
    }
    renderCandPager(visible.length, pageCount, start, slice.length, size);
    updateBatchCounts();
    ensurePreview();
    // 探针量与真卡可能有亚像素差：有真卡后复核一次容量，不同就再排一次（只重排一次，不递归）
    if (!settling && slice.length && candCapacity() !== size) renderCandidates(true);
  }

  function renderCandPager(total, pageCount, start, shown, size) {
    const pager = $('batch-cand-pager');
    if (!pager) return;
    const empty = total === 0;
    pager.classList.toggle('hidden', empty);
    // 「全选全部 N 条」的数字跟着筛选走，按钮文案就是点下去会覆盖的范围
    const matched = candidateMatched();
    const readyMatched = matched.filter((item) => item.ready).length;
    const allBtn = $('batch-select-all');
    const label = `全选全部 ${matched.length} 条`;
    if (allBtn.textContent !== label) allBtn.textContent = label;
    allBtn.disabled = !matched.length;
    allBtn.title = readyMatched < matched.length
      ? `当前筛选共 ${matched.length} 条，其中 ${readyMatched} 条已上传云端可生产；未上传的 ${matched.length - readyMatched} 条选不了。`
      : `选中当前筛选下的全部 ${matched.length} 条（跨 ${candPageCount(matched.length, size)} 页），已选数量继续累计。`;
    if (empty) return;
    $('batch-page-prev').disabled = b.candPage <= 1;
    $('batch-page-next').disabled = b.candPage >= pageCount;
    const info = $('batch-page-info');
    info.textContent = `第 ${b.candPage} / ${pageCount} 页`;
    info.title = `本页显示第 ${start + 1}–${start + shown} 条（每页 ${size} 条），共 ${total} 条`;
  }

  function goCandPage(page) {
    const pageCount = candPageCount(candidateVisible().length, b.candPageSize);
    const next = Math.min(Math.max(1, page), pageCount);
    if (!Number.isFinite(next) || next === b.candPage) return;
    b.candPage = next;
    renderCandidates();
    scrollCandTop();
  }

  /* 翻页后把视线带回候选区顶部。桌面候选网格自己滚（改 scrollTop 即可），
     平板/手机是整页滚动，得把窗口拉回候选区。
     桌面一页正好一屏、页面本身不该有滚动条，这时绝不能动窗口 —— 一滚就把候选区推到视口上方，
     底下留出一大片空白（这正是之前那个 bug 的现场）。 */
  function scrollCandTop() {
    const grid = $('batch-cand-grid');
    const main = document.querySelector('.batch-cand-main');
    if (grid) grid.scrollTop = 0;
    if (!main) return;
    const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    if (maxScroll <= 0) return;   // 页面不可滚（桌面固定高度模式），交给网格自己
    const rect = main.getBoundingClientRect();
    if (rect.top >= 0 && rect.top < window.innerHeight * 0.5) return;
    const top = rect.top + (window.scrollY || window.pageYOffset || 0) - 8;
    window.scrollTo(0, Math.min(maxScroll, Math.max(0, Math.round(top))));
  }

  /* 密集候选行：一行勾选框 + 文件名，二行标签单行省略；整卡可点 = 选中 + 预览。
     未上传的候选不能勾选（服务端锁定会拒），但仍然可以点开看，才知道为什么不能用。 */
  function candidateTile(item) {
    const selected = b.selected.has(item.id);
    const tile = el('div', `batch-cand${selected ? ' is-selected' : ''}${item.ready ? '' : ' is-unready'}`);
    tile.dataset.id = item.id;
    tile.tabIndex = 0;
    tile.setAttribute('role', 'button');
    tile.setAttribute('aria-pressed', selected ? 'true' : 'false');
    tile.setAttribute('aria-label', item.ready
      ? `选中并预览 ${item.name}`
      : `预览 ${item.name}（未上传云端，不能选中）`);
    tile.title = item.ready
      ? `${item.name}（${item.id}）\n点击在右侧预览并选中；取消选择点左侧勾选框`
      : `${item.name}（${item.id}）\n还没有上传到云端，只能预览、不能选中`;

    const pick = el('label', 'batch-cand-pick');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'batch-cand-check';
    checkbox.checked = selected;
    checkbox.disabled = !item.ready;
    checkbox.setAttribute('aria-label', `选择 ${item.name}`);
    pick.append(checkbox);

    const line = el('span', 'batch-cand-line');
    const name = el('span', 'batch-cand-name', item.name);
    name.title = item.name;
    line.append(name);
    // 评分徽标：标签库打的分（1-5 星），没打分不显示，不占格子
    if (item.rating > 0) line.append(el('span', 'batch-cand-badge batch-cand-badge-rating', `★${item.rating}`));
    if (!item.ready) line.append(el('span', 'batch-cand-badge', '未上传'));
    const usedInfo = b.styleUsed[item.id];
    if (usedInfo) {
      line.append(el('span', 'batch-cand-badge batch-cand-badge-used',
        `已用${txt(usedInfo.batch_no) ? `·${txt(usedInfo.batch_no)}` : ''}`));
      tile.title += `\n款号已在批次「${txt(usedInfo.batch_no) || txt(usedInfo.batch_id) || '未知'}」用过该模板`;
    }
    // 标签全部展示：>5 个缩字号（is-dense），>12 个再缩一档（is-tiny），换行铺开不截断
    const tagCount = item.tags.length;
    const tags = item.tags.join(' / ');
    const meta = el('div', 'batch-cand-meta', `${item.id}${tags ? ` · ${tags}` : ''}`);
    if (tagCount > 12) meta.classList.add('is-tiny');
    else if (tagCount > 5) meta.classList.add('is-dense');
    meta.title = tagCount ? item.tags.join(' / ') : item.id;
    tile.append(pick, line, meta);

    const paint = () => {
      const on = b.selected.has(item.id);
      tile.classList.toggle('is-selected', on);
      tile.setAttribute('aria-pressed', on ? 'true' : 'false');
      checkbox.checked = on;
    };
    // 勾选框只负责选中/取消，不抢整卡的点击
    pick.addEventListener('click', (event) => event.stopPropagation());
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) b.selected.add(item.id);
      else b.selected.delete(item.id);
      paint();
      updateBatchCounts();
      if (b.onlySelected) renderCandidates();
    });

    const activate = () => {
      if (item.ready && !b.selected.has(item.id)) {
        // 点卡片是「选中」，不做反选：默认全选已就绪项时，预览一圈不该把候选悄悄取消掉
        b.selected.add(item.id);
        paint();
        updateBatchCounts();
        if (b.onlySelected) { renderCandidates(); return; }
      }
      previewCandidate(item, tile);
    };
    tile.addEventListener('click', activate);
    tile.addEventListener('keydown', (event) => {
      if (event.target !== tile) return;
      if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return;
      event.preventDefault();
      activate();
    });
    return tile;
  }

  const PREVIEW_PLACEHOLDER = '进入候选后会在这里自动播放第一条，点任意候选即可切换（整页只有一个播放器）。';

  function showPreviewNote(text, isError) {
    const node = $('batch-preview-note');
    if (!node) return;
    // 真实的加载失败优先：别被后面姗姗来迟的「自动播放被拦」提示盖掉
    const hasError = node.classList.contains('is-error') && !node.classList.contains('hidden');
    if (hasError && text && !isError) return;
    node.textContent = txt(text);
    node.classList.toggle('is-error', !!text && !!isError);
    node.classList.toggle('hidden', !text);
  }
  function clearPreview() {
    const video = $('batch-preview-video');
    b.previewId = '';
    if (video) {
      video.removeAttribute('src');
      video.muted = false;
      video.classList.add('is-empty');
      if (video.load) video.load();
    }
    $('batch-preview-name').textContent = PREVIEW_PLACEHOLDER;
    showPreviewNote('');
    document.querySelectorAll('.batch-cand.is-previewing').forEach((node) => node.classList.remove('is-previewing'));
  }
  // 整页只有这一个 <video>：切候选只改 src，绝不按候选数量建 video 标签。
  function previewCandidate(item, tile) {
    const video = $('batch-preview-video');
    if (!video || !item || !item.id) return;
    document.querySelectorAll('.batch-cand.is-previewing').forEach((node) => node.classList.remove('is-previewing'));
    if (tile) tile.classList.add('is-previewing');
    $('batch-preview-name').textContent = `${item.name}（${item.id}）`;
    if (!item.ready) {
      // 未上传的候选云端没有文件，/preview 只会 404 —— 与其给一块黑屏，不如直接说原因
      b.previewId = '';
      video.removeAttribute('src');
      video.muted = false;
      video.classList.add('is-empty');
      if (video.load) video.load();
      showPreviewNote('这条候选还没上传到云端，云端没有可播放的预览；上传后重新筛选就能勾选进清单。');
      return;
    }
    const src = `/api/templates/${encodeURIComponent(item.id)}/preview`;
    const changed = video.getAttribute('src') !== src;
    b.previewId = item.id;
    if (changed) {
      showPreviewNote('');
      video.classList.remove('is-empty');
      video.src = src;
      if (video.load) video.load();
    }
    if (changed) tryPlay(video);
  }
  function tryPlay(video) {
    let promise;
    try { promise = video.play(); } catch (_) {
      showPreviewNote('浏览器拦住了自动播放，点一下播放键即可查看。');
      return;
    }
    if (!promise || typeof promise.then !== 'function') return;
    promise.catch(() => {
      // 多数浏览器只允许静音自动播放：先静音再试一次，仍被拦就提示手动点（原生控件一直在）
      if (!video.muted) {
        video.muted = true;
        const retry = video.play();
        if (retry && typeof retry.then === 'function') {
          retry.catch(() => showPreviewNote('浏览器拦住了自动播放，点一下播放键即可查看。'));
        }
        return;
      }
      showPreviewNote('浏览器拦住了自动播放，点一下播放键即可查看。');
    });
  }
  // 进入候选（或翻到某一页）后自动把这一页第一条可用候选送进播放器（用户已手动选过就不打扰）
  function ensurePreview() {
    const panel = $('batch-step-b');
    const video = $('batch-preview-video');
    if (!panel || !video || panel.classList.contains('hidden')) return;
    if (b.previewId) return;
    const all = candidateVisible();
    const pageReady = (b.candPageItems || []).filter((item) => item.ready);
    const first = pageReady[0] || all.find((item) => item.ready) || all[0];
    if (!first) return;
    previewCandidate(first, tileById(first.id));
  }
  function tileById(id) {
    const grid = $('batch-cand-grid');
    if (!grid) return null;
    return [...grid.children].find((node) => node.dataset && node.dataset.id === id) || null;
  }

  /* 候选区高度：候选区现在**跟着右列（播放器）等高**，实际高度由 CSS 的 grid 行高决定。
     这里的实测值只作为**下限**写进 --batch-cand-h，含义是「候选区至少长到视口底部（再留 12px）」，
     防止在右列比视口还矮的极端屏上候选区缩成一条。它**不参与**决定播放器高度 ——
     播放器只认 .batch-preview-video 的 clamp(320px,72vh,760px)，一个像素都不许动。
     于是整页仍会被右列撑出纵向滚动，这是播放器面积的代价，不是 bug。 */
  const CAND_BOTTOM_GAP = 12;   // 候选区底边到视口底部留的空隙，与 .shell 的 12px 同口径
  const CAND_MIN_H = 240;

  function candPanelVisible() {
    const panel = $('batch-step-b');
    const work = $('batch-work');
    return !!panel && !!work && !panel.classList.contains('hidden') && !work.classList.contains('hidden');
  }

  function syncCandHeight() {
    const main = document.querySelector('.batch-cand-main');
    const layout = document.querySelector('.batch-cand-layout');
    if (!main || !layout) return;
    // 变量写在候选区与右列的**共同父级**上，便于统一调试/取值
    const host = layout;
    if (!candPanelVisible() || window.innerWidth <= 900) {
      host.style.removeProperty('--batch-cand-h');
      return;
    }
    const scrollY = window.scrollY || window.pageYOffset || 0;
    const topInDoc = main.getBoundingClientRect().top + scrollY;
    const height = Math.max(CAND_MIN_H, Math.round(window.innerHeight - topInDoc - CAND_BOTTOM_GAP));
    host.style.setProperty('--batch-cand-h', `${height}px`);
  }

  /* 视口 / 表单开合 / 标签面板开合 / 提示行显隐 → 候选网格高度变了 → 每页条数得重算。
     rAF 合并连续 resize；只有容量真的变了才重建这一页（页码由 renderCandidates 夹取）。 */
  function queueCandLayout() {
    if (b.resizeRaf) cancelAnimationFrame(b.resizeRaf);
    b.resizeRaf = requestAnimationFrame(() => {
      b.resizeRaf = 0;
      syncCandHeight();
      if (!candPanelVisible()) return;
      if (candCapacity() !== b.candPageSize) renderCandidates();
    });
  }
  function showCandNote(text) {
    const node = $('batch-cand-note');
    if (!node) return;
    node.textContent = txt(text);
    node.classList.toggle('hidden', !text);
    queueCandLayout();
  }
  function updateBatchCounts() {
    const chosen = b.selected.size;
    $('batch-count-label').textContent = `已选 ${chosen} / 候选 ${b.candidates.length}`;
    // 锁定条在候选区底部，标题只说数量；完整提示跟在数量后面，窄屏可换行。
    $('batch-lock-count').textContent = chosen ? `已选 ${chosen} 条` : '已选 0 条';
    $('batch-lock-btn').disabled = !chosen || b.busy;
    $('batch-lock-hint').textContent = chosen
      ? '锁定后清单不可更改，未勾选的候选不会生产。'
      : '至少勾选一条才能锁定。';
  }
  function renderCandChips() {
    const root = $('batch-cand-chips');
    root.replaceChildren();
    const counts = new Map();
    for (const item of b.candidates) for (const tag of item.tags) counts.set(tag, (counts.get(tag) || 0) + 1);
    const tags = [...counts.entries()].sort((left, right) => right[1] - left[1]).slice(0, 24);
    if (!tags.length) return;
    root.append(el('span', 'batch-chips-label', '按标签快速定位（需同时包含）：'));
    for (const [tag, count] of tags) {
      const chip = el('button', `batch-chip${b.candTagFilter.has(tag) ? ' is-on' : ''}`, `${tag} ${count}`);
      chip.type = 'button';
      chip.setAttribute('aria-pressed', b.candTagFilter.has(tag) ? 'true' : 'false');
      chip.addEventListener('click', () => {
        if (b.candTagFilter.has(tag)) b.candTagFilter.delete(tag);
        else b.candTagFilter.add(tag);
        b.candPage = 1;   // 筛选条件变了，回第 1 页
        renderCandChips();
        renderCandidates();
      });
      root.append(chip);
    }
  }

  async function lockBatch() {
    const batch = b.current;
    if (!batch || !batch.id || b.busy) return;
    const ids = [...b.selected];
    if (!ids.length) { toast('至少勾选一条候选。'); return; }
    // 同款号防重（软拦截）：勾了该款号已用过的模板，弹确认让使用者自己定夺。
    const reused = ids.filter((id) => b.styleUsed[id]);
    if (reused.length && !confirm(`当前款号有 ${reused.length} 条候选在之前的批次里用过。\n\n确定要重复使用吗？（历史记录只为防重提醒，不影响归档）`)) return;
    if (!confirm(`锁定 ${ids.length} 条为待生产清单？\n\n锁定后不能再增删条目，也不能换定妆图；要改只能新建批次。`)) return;
    const btn = $('batch-lock-btn');
    b.busy = true; btn.disabled = true; btn.textContent = '正在锁定…';
    $('batch-lock-error').classList.add('hidden');
    try {
      await api(`/api/batches/${encodeURIComponent(batch.id)}/lock`, {
        method: 'POST',
        body: JSON.stringify({ template_ids: ids, tag_filters: b.tagFilters, confirmed: true }),
      });
      toast(`已锁定 ${ids.length} 条清单，可以开始分批生产。`);
      await refreshBatch(false);
    } catch (err) {
      toast(err.message);
      const node = $('batch-lock-error');
      node.textContent = err.message;
      node.classList.remove('hidden');
    } finally {
      b.busy = false;
      btn.textContent = '锁定清单';
      renderBatch();
    }
  }

  /* ---------- 步骤 C / D：锁定摘要、分批生产 ---------- */
  // 摘要收成一条横条：左边定妆图缩略（48px），右边一排 chip。
  // 原来 8 个格子铺满整行、被定妆图撑到 200px 高，是这页最占地方的一块。
  function renderSummary(batch) {
    const root = $('batch-summary');
    root.replaceChildren();
    const total = num(batch.total_items, b.items.length);
    const done = num(batch.done);
    const failed = num(batch.failed);
    const durationValue = batch.duration === null || batch.duration === undefined
      ? BATCH_DURATION_PARAM : num(batch.duration, BATCH_DURATION_PARAM);
    const prompt = txt(batch.prompt);
    const filters = batch.tag_filters && typeof batch.tag_filters === 'object' ? batch.tag_filters : null;
    const filterText = filters
      ? Object.entries(filters).map(([category, values]) => `${category}: ${Array.isArray(values) ? values.join('/') : txt(values)}`).join('；')
      : '';
    const rows = [
      ['批次号', txt(batch.batch_no) || '—'],
      ['状态', BATCH_STATE_TEXT[txt(batch.state)] || txt(batch.state) || '—'],
      // 本页建出来的批次恒为 duration=7；-1 只可能来自接口直建的老批次，文案别硬说成 7 秒。
      ['画质 / 时长', `${txt(batch.resolution) || '—'} · ${durationValue > 0 ? `输出 ${durationValue} 秒` : '跟随素材时长'}`],
      ['清单', `${total} 条`],
      ['进度', `已结束 ${done + failed} / ${total}（成功 ${done} · 失败 ${failed}）`],
      ['锁定时间', fmtTime(batch.locked_at)],
      ['标签快照', filterText || '锁定前未选标签'],
    ];

    const bar = el('div', 'batch-sum-bar');
    // 定妆图缩略：有图才占位，点开看原图；整批共用这一张，缩略图 48px 够认人
    const imageUrl = txt(batch.image_url);
    if (imageUrl) {
      const link = el('a', 'batch-sum-thumb');
      link.href = imageUrl;
      link.target = '_blank';
      link.rel = 'noopener';
      link.title = `查看定妆图原图：${txt(batch.image_name) || '—'}`;
      const img = el('img', null);
      img.src = imageUrl;
      img.alt = txt(batch.image_name) || '定妆图';
      img.loading = 'lazy';
      link.append(img);
      bar.append(link);
    }

    const chips = el('div', 'batch-sum-chips');
    for (const [label, value] of rows) {
      const chip = el('span', 'batch-sum-chip');
      chip.append(el('small', null, label), el('b', null, value));
      chips.append(chip);
    }
    if (txt(batch.image_name)) {
      const chip = el('span', 'batch-sum-chip');
      chip.append(el('small', null, '定妆图'), el('b', null, txt(batch.image_name)));
      chips.append(chip);
    }
    bar.append(chips);
    root.append(bar);

    // 提示词：默认折叠两行，点「展开」看全文；锁定后仍可修改 —— 生产时才读取批次提示词，
    // 改完只影响之后的分批生产，已生成的视频不变。
    root.append(buildPromptBlock(batch, prompt));
    root.append(el('p', 'muted batch-summary-hint',
      '清单已锁定：条目不可增删，定妆图 / 画质 / 时长都用批次创建时的设置；提示词可改，只影响之后的分批生产。'));
  }

  function buildPromptBlock(batch, prompt) {
    const block = el('div', 'batch-prompt-block');
    const head = el('div', 'batch-prompt-head');
    head.append(el('small', null, `提示词 · ${prompt.length} 字`));
    const editable = txt(batch.state) !== 'closed' && !txt(batch.active_job_id);
    if (b.promptEditing) {
      const body = el('textarea', 'batch-prompt-edit');
      body.rows = 6;
      body.maxLength = 4000;
      body.spellcheck = false;
      body.value = b.promptDraft;
      body.setAttribute('aria-label', '编辑批次提示词');
      const count = el('span', 'muted batch-prompt-count', `${body.value.length} / 4000`);
      body.addEventListener('input', () => {
        b.promptDraft = body.value;   // 4 秒轮询会重渲染摘要，草稿必须实时落回状态，否则输入被冲掉
        count.textContent = `${body.value.length} / 4000`;
      });
      const actions = el('div', 'batch-prompt-actions');
      const save = el('button', 'primary btn-sm', '保存提示词');
      save.type = 'button';
      const cancel = el('button', 'quiet btn-sm', '取消');
      cancel.type = 'button';
      cancel.addEventListener('click', () => {
        b.promptEditing = false;
        b.promptDraft = '';
        renderSummary(batch);
      });
      save.addEventListener('click', async () => {
        const next = body.value.trim();
        if (!next) { toast('提示词不能为空。'); return; }
        if (next.length > 4000) { toast('提示词最多 4000 个字符。'); return; }
        save.disabled = true; cancel.disabled = true;
        try {
          await api(`/api/batches/${encodeURIComponent(txt(batch.id))}/prompt`, {
            method: 'POST',
            body: JSON.stringify({ prompt: next }),
          });
          b.promptEditing = false;
          b.promptDraft = '';
          toast('提示词已更新，之后的分批生产会用新提示词。');
          await loadBatchDetail(true);          // 拉回最新 batch.prompt 并重渲染
        } catch (err) {
          save.disabled = false; cancel.disabled = false;
          toast(err.message || '保存失败，请稍后重试');
        }
      });
      actions.append(save, cancel, count);
      block.append(head, body, actions);
    } else {
      const tools = el('div', 'batch-prompt-tools');
      // 折叠/展开：默认只露两行，长提示词不再把页面顶下去
      const toggle = el('button', 'quiet btn-sm batch-prompt-toggle', b.promptOpen ? '收起' : '展开');
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', b.promptOpen ? 'true' : 'false');
      toggle.title = b.promptOpen ? '收起提示词' : '展开看完整提示词';
      toggle.addEventListener('click', () => {
        b.promptOpen = !b.promptOpen;
        renderSummary(batch);
      });
      const edit = el('button', 'quiet btn-sm batch-prompt-edit-btn', '修改提示词');
      edit.type = 'button';
      edit.disabled = !editable;
      edit.title = editable ? '改完只影响之后的分批生产' : '有任务在跑或批次已关闭时不能改';
      edit.addEventListener('click', () => {
        b.promptDraft = txt(batch.prompt);
        b.promptEditing = true;
        renderSummary(batch);
        // 聚焦编辑框，光标落末尾，改起来顺手
        const body = document.querySelector('.batch-prompt-edit');
        if (body) { body.focus(); body.setSelectionRange(body.value.length, body.value.length); }
      });
      tools.append(toggle, edit);
      head.append(tools);
      const text = el('p', 'batch-prompt-text' + (b.promptOpen ? '' : ' is-clamped'), prompt || '—');
      block.append(head, text);
    }
    return block;
  }

  function renderProduce(batch) {
    if (!batch) return;
    const total = num(batch.total_items, b.items.length);
    const done = num(batch.done);
    const failed = num(batch.failed);
    const pending = Math.max(0, num(batch.pending, total - done - failed));
    const activeId = txt(batch.active_job_id);
    const closed = txt(batch.state) === 'closed';

    const counters = $('batch-counters');
    counters.replaceChildren();
    for (const [value, label] of [[pending, '待生产'], [done, '已生产'], [failed, '失败']]) {
      const box = el('div', 'stat');
      box.append(el('b', null, String(value)), el('small', null, label));
      counters.append(box);
    }

    const finished = done + failed;
    $('batch-progress-fill').style.width = `${total ? Math.min(100, (finished / total) * 100) : 0}%`;
    const job = b.activeJob;
    let progressText = total ? `已完成 ${finished} / 总数 ${total} · 剩余待生产 ${pending}` : '清单为空';
    if (activeId) {
      const jobDone = num(job && job.done);
      const jobTotal = num(job && job.total);
      progressText += jobTotal ? ` · 当前批次 ${jobDone}/${jobTotal}` : ' · 当前批次正在排队';
    } else if (pending === 0 && total) {
      progressText += ' · 全部跑完了';
    }
    $('batch-progress-text').textContent = progressText;

    const actions = $('batch-produce-actions');
    actions.replaceChildren();
    const plan = produceCounts(batch);
    // 服务端给了档位就按它的渲染（配置可能把单次上限调到小于 100），否则用本地三档。
    const limits = serviceLimits();
    const counts = plan.source === 'server' && plan.counts.length ? plan.counts : limits.choices;
    // 按钮按"实际会提交多少条"来显示：档位大于剩余待生产时截断为剩余数。
    // 例如选了 54 条：显示 生产 20 / 生产 50 / 生产剩余 54；
    // 点完 20 再剩 34：显示 生产 20 / 生产 34（50 和 100 截断后都是 34，只留一个按钮）。
    // 这样按钮永远不会虚报数量，也不会出现几个一模一样的按钮。
    const seen = new Set();
    for (const tier of counts) {
      const count = Math.min(tier, pending);
      if (count <= 0) continue;
      if (seen.has(count)) continue;
      seen.add(count);
      const clamped = count < tier;
      const btn = el('button', 'primary batch-produce-btn');
      btn.type = 'button';
      btn.textContent = clamped ? `生产剩余 ${count} 条` : `生产 ${count} 条`;
      btn.disabled = b.busy || !!activeId || closed || pending === 0;
      btn.title = activeId ? '当前批次还有任务在跑，跑完再点下一批'
        : closed ? '批次已关闭'
        : clamped ? `清单只剩 ${pending} 条，本次会把剩余的全部提交`
        : '点下去会立刻提交，不可撤回';
      btn.addEventListener('click', () => produce(count, batch));
      actions.append(btn);
    }
    renderProduceCustom(pending, activeId, closed);

    // 失败条目给一个重试入口（接口 #9），否则 failed 条目在页面上就是死路。
    const retryBox = $('batch-retry');
    retryBox.replaceChildren();
    if (failed > 0) {
      const retryCount = Math.min(failed, 100);
      const retry = el('button', 'quiet', `重试失败的 ${retryCount} 条`);
      retry.type = 'button';
      retry.disabled = b.busy || !!activeId || closed;
      retry.title = activeId ? '当前批次还有任务在跑' : '只重跑失败条目，不改清单';
      retry.addEventListener('click', () => retryFailed(batch, retryCount));
      retryBox.append(retry, el('span', 'muted', '只重跑失败条目，已完成的不会重复提交。'));
      retryBox.classList.remove('hidden');
    } else {
      retryBox.classList.add('hidden');
    }
  }

  /* ---------- 自定条数（档位之外的可变粒度：7、13、33 都行） ----------
     渲染只改 min/max/disabled 与提示文案，**不重写输入框的值**：
     这里每 4 秒被轮询重渲染一次，用户正敲「12」时被硬改成 5 是最难受的体验。
     越界纠正放到提交那一刻（produceCustom 里），用户能看懂为什么被改。 */
  function renderProduceCustom(pending, activeId, closed) {
    const input = $('batch-produce-count');
    const btn = $('batch-produce-apply');
    const hint = $('batch-produce-hint');
    if (!input || !btn) return;
    const limits = serviceLimits();
    const range = produceRange(b.current);
    const disabled = b.busy || !!activeId || closed || pending === 0;
    input.min = String(range.min);
    input.max = String(Math.max(range.min, range.cap));
    input.title = range.cap >= limits.produceMin
      ? `可填 ${limits.produceMin}–${range.cap} 之间的任意整数（7、13、33 都行）`
      : `清单只剩 ${range.cap} 条，可一次提交`;
    input.disabled = disabled;
    btn.disabled = disabled;
    if (hint) {
      hint.textContent = pending === 0 ? '清单已跑完'
        : range.cap < limits.produceMin ? `只剩 ${range.cap} 条`
          : `${limits.produceMin}–${range.cap} 任意整数`;
    }
  }

  function produceCustom() {
    const batch = b.current;
    if (b.busy || !batch || !batch.id) return;
    const input = $('batch-produce-count');
    const typed = Math.trunc(num(input && input.value, 0));
    if (!typed) {
      toast('先在「自定」里填要生产的条数。');
      if (input) input.focus();
      return;
    }
    const range = produceRange(batch);
    if (range.pending === 0) { toast('清单里已经没有待生产的条目了。'); return; }
    if (typed < range.min) {
      toast(range.min > 1
        ? `本次最少生产 ${range.min} 条；清单只剩 ${range.pending} 条时可一次提交全部剩余。`
        : `清单只剩 ${range.cap} 条，最多只能提交这么多。`);
      return;
    }
    const count = Math.min(typed, range.cap);
    if (input) input.value = String(count);   // 纠正后的值回显，避免以为提交了 typed 条
    if (count < typed) toast(`清单只剩 ${count} 条，本次按 ${count} 条提交。`);
    produce(count, batch);
  }

  async function retryFailed(batch, count) {
    if (b.busy || !batch || !batch.id) return;
    if (!confirm(`重试失败的 ${count} 条？\n\n提交后不能撤回。`)) return;
    b.busy = true;
    renderProduce(batch);
    try {
      const data = await api(`/api/batches/${encodeURIComponent(batch.id)}/retry-failed`, {
        method: 'POST',
        body: JSON.stringify({ count }),
      });
      toast(`已重新排入 ${num(data.taken, count)} 条失败条目。`);
      b.busy = false;
      await refreshBatch(false);
    } catch (err) {
      b.busy = false;
      toast(err.message);
      const node = $('batch-produce-error');
      node.textContent = err.message;
      node.classList.remove('hidden');
      renderProduce(batch);
    }
  }

  async function produce(count, batch) {
    if (b.busy || !batch || !batch.id) return;
    if (!confirm(`将立刻提交 ${count} 条生产任务。\n\n提交后不能撤回。是否继续？`)) return;
    b.busy = true;
    renderProduce(batch);
    $('batch-produce-error').classList.add('hidden');
    try {
      const data = await api(`/api/batches/${encodeURIComponent(batch.id)}/produce`, {
        method: 'POST',
        body: JSON.stringify({ count }),
      });
      const taken = num(data.taken, count);
      const blocked = Array.isArray(data.blocked) ? data.blocked.length : 0;
      const message = [taken < count ? `本批只剩 ${taken} 条，已全部提交。` : `已提交 ${taken} 条，正在生产。`];
      if (blocked) message.push(`${blocked} 条模板已不可用，已标为失败`);
      toast(message.join(' '));
      b.busy = false;
      await refreshBatch(false);
    } catch (err) {
      b.busy = false;
      toast(err.message);
      const node = $('batch-produce-error');
      node.textContent = err.message;
      node.classList.remove('hidden');
      renderProduce(batch);
    }
  }

  function itemStateClass(state) {
    if (state === 'done') return 'batch-done';
    if (state === 'failed') return 'batch-failed';
    if (state === 'producing') return 'batch-producing';
    return 'batch-pending';
  }
  function renderItems() {
    const root = $('batch-item-list');
    // 轮询每 4 秒来一次：条目状态没变就不重建 DOM，避免用户正在看的列表被刷掉。
    const signature = `${b.itemFilter}|${b.itemLimit}|${b.items.length}|`
      + b.items.map((item) => `${txt(item.template_id)}:${txt(item.state)}:${txt(item.job_id)}:${txt(item.error) ? 1 : 0}:${txt(item.verify_warn) ? 1 : 0}`).join(',');
    if (signature === b.itemsSignature && root.childElementCount) return;
    b.itemsSignature = signature;
    hideHoverPreview(); // 卡片要重建：正悬停的那张会被移走，小窗先收起
    root.replaceChildren();
    const filtered = b.itemFilter === 'all' ? b.items : b.items.filter((item) => txt(item.state) === b.itemFilter);
    if (!b.items.length) {
      root.append(el('div', 'empty', '清单还没有条目。'));
      $('batch-items-more').classList.add('hidden');
      return;
    }
    const slice = filtered.slice(0, b.itemLimit);
    if (!slice.length) {
      root.append(el('div', 'empty', '该状态下没有条目。'));
    } else {
      const fragment = document.createDocumentFragment();
      for (const item of slice) fragment.append(itemCard(item));
      root.append(fragment);
    }
    const more = $('batch-items-more');
    const rest = Math.max(0, filtered.length - slice.length);
    more.classList.toggle('hidden', rest === 0);
    more.textContent = `加载更多条目（还有 ${rest} 条）`;
  }
  /* ---------- 清单条目：悬浮预览 ----------
     卡片保持原大小与排布，播放改成悬浮小窗：悬停媒体区 250ms 后出现，
     离开/滚动即停。全页只有一个悬浮播放器，src 用完即清，不会多路并发拉流。 */
  const hoverPreview = { box: null, video: null, note: null, timer: null, tid: '' };

  function ensureHoverPreview() {
    if (hoverPreview.box) return;
    const box = el('div', 'batch-hover-preview');
    box.classList.add('hidden');
    const video = document.createElement('video');
    video.muted = true;      // 悬浮预览静音自动播，避免浏览器的自动播放拦截
    video.loop = true;
    video.playsInline = true;
    video.preload = 'auto';
    const note = el('div', 'batch-hover-note hidden');
    box.append(video, note);
    document.body.append(box);
    hoverPreview.box = box;
    hoverPreview.video = video;
    hoverPreview.note = note;
  }

  function hideHoverPreview() {
    if (hoverPreview.timer) { clearTimeout(hoverPreview.timer); hoverPreview.timer = null; }
    const box = hoverPreview.box;
    const video = hoverPreview.video;
    if (!box) return;
    try { video.pause(); } catch (_) { /* 已释放时 pause 可能抛错，忽略 */ }
    video.removeAttribute('src');
    if (video.load) video.load();
    box.classList.add('hidden');
    hoverPreview.note.classList.add('hidden');
    hoverPreview.tid = '';
  }

  function showHoverPreview(media, tid, name) {
    if (!tid) return;
    ensureHoverPreview();
    if (hoverPreview.tid === tid && !hoverPreview.box.classList.contains('hidden')) return;
    hideHoverPreview();
    hoverPreview.tid = tid;
    const box = hoverPreview.box;
    const video = hoverPreview.video;
    const note = hoverPreview.note;
    video.setAttribute('aria-label', `${name} 悬浮预览`);
    video.onerror = () => {
      note.textContent = '无法预览：素材未上传到云端，或视频是 H.265 编码（浏览器不支持）';
      note.classList.remove('hidden');
    };
    // 摆位：贴着媒体区，默认在其上方放不下就换到下方，左右不出视口
    const rect = media.getBoundingClientRect();
    const width = Math.min(280, window.innerWidth - 16);
    const estHeight = width * 10 / 16;
    const left = Math.min(Math.max(8, rect.left + rect.width / 2 - width / 2), window.innerWidth - width - 8);
    let top = rect.top - estHeight - 8;
    if (top < 8) top = rect.bottom + 8;
    if (top + estHeight > window.innerHeight - 8) top = Math.max(8, window.innerHeight - estHeight - 8);
    box.style.left = `${Math.round(left)}px`;
    box.style.top = `${Math.round(top)}px`;
    box.style.width = `${width}px`;
    box.classList.remove('hidden');
    video.src = `/api/templates/${encodeURIComponent(tid)}/preview`;
    const promise = video.play();
    if (promise && typeof promise.catch === 'function') promise.catch(() => { /* 静音被拦的概率极低，移开再悬停即可 */ });
  }

  function scheduleHoverPreview(media, tid, name) {
    if (!tid) return;
    if (hoverPreview.timer) clearTimeout(hoverPreview.timer);
    hoverPreview.timer = setTimeout(() => {
      hoverPreview.timer = null;
      showHoverPreview(media, tid, name);
    }, 250);
  }

  function itemCard(item) {
    const state = txt(item.state) || 'pending';
    const tid = txt(item.template_id);
    const name = txt(item.name) || tid || '未命名场景';
    const card = el('article', 'batch-item-card');
    card.dataset.state = state;

    const media = el('div', 'batch-item-media');
    if (tid) {
      // ▶ 只做视觉提示，不再可点：悬停媒体区 250ms 自动浮出预览小窗
      const play = el('button', 'batch-item-play', '▶');
      play.type = 'button';
      play.tabIndex = -1;
      play.setAttribute('aria-hidden', 'true');
      play.title = '悬停在此处自动预览';
      media.append(play);
      media.addEventListener('mouseenter', () => scheduleHoverPreview(media, tid, name));
      media.addEventListener('mouseleave', hideHoverPreview);
    } else {
      media.append(el('span', 'batch-item-media-note', '无素材编号'));
    }

    const body = el('div', 'batch-item-body');
    const title = el('div', 'batch-item-title');
    title.append(el('b', null, name), el('small', null, tid || '—'));
    const tags = el('div', 'batch-item-tags');
    tags.append(el('span', `status ${itemStateClass(state)}`, ITEM_STATE_TEXT[state] || state));
    // 一行只说一件事：错误 / 归档提醒 / 任务号，放不下就省略，悬停看全文
    const info = txt(item.error) || txt(item.archive_warning) || (item.job_id ? `任务 ${txt(item.job_id)}` : '');
    if (info) {
      const line = el('small', 'batch-item-info', info);
      line.title = info;
      tags.append(line);
    }
    // verify_warn：模型实际输出偏离预期（时长不符/文件过小）的质量提示，是提示不是失败
    const warn = txt(item.verify_warn);
    if (warn) {
      const line = el('small', 'batch-item-info batch-verify-warn', `质量提示：${warn}`);
      line.title = warn;
      tags.append(line);
    }
    body.append(title, tags);

    const actions = el('div', 'batch-item-actions');
    // items 契约里没有 download_url；done 条目按历史分组同一套路径拼，后端若以后直接返回就优先用。
    const downloadUrl = txt(item.download_url) || (state === 'done' && txt(item.job_id) && tid
      ? `/api/jobs/${encodeURIComponent(txt(item.job_id))}/download/${encodeURIComponent(tid)}`
      : '');
    if (downloadUrl) {
      const link = el('a', 'link-btn btn-sm', '下载');
      link.href = downloadUrl;
      actions.append(link);
    }
    if (txt(item.job_id)) {
      const btn = el('button', 'quiet btn-sm', '任务详情');
      btn.type = 'button';
      btn.addEventListener('click', () => openJobDetail(txt(item.job_id)));
      actions.append(btn);
    }
    if (!actions.childNodes.length) actions.append(el('span', 'muted batch-item-none', '—'));

    card.append(media, body, actions);
    return card;
  }

  /* ---------- 批次详情与轮询 ---------- */
  async function loadBatchDetail(silent = false) {
    const batch = b.current;
    if (!batch || !batch.id) return;
    try {
      const data = await api(`/api/batches/${encodeURIComponent(batch.id)}`);
      if (data.batch) b.current = data.batch;
      b.items = Array.isArray(data.items) ? data.items : [];
      renderBatch();
      maybePoll();
    } catch (err) {
      if (/不存在|404|Not Found/i.test(err.message)) {
        b.current = null; b.items = []; b.mode = 'list';
        renderBatch();
        loadBatches();
        toast('这个批次已不存在，已回到批次列表。');
        return;
      }
      if (!silent) toast(err.message);
    }
  }
  async function refreshBatch(silent = true) {
    if (!b.current || !b.current.id) { loadBatches(); return; }
    await loadBatchDetail(silent);
    const activeId = txt(b.current && b.current.active_job_id);
    if (activeId) {
      try {
        const data = await api(`/api/jobs/${encodeURIComponent(activeId)}`);
        b.activeJob = data.job || null;
      } catch (_) { b.activeJob = null; }
    } else {
      b.activeJob = null;
    }
    if (b.current) renderProduce(b.current);
  }
  function maybePoll() {
    if (txt(b.current && b.current.active_job_id)) startPoll();
    else stopPoll();
  }
  function startPoll() {
    if (b.pollTimer) return;
    b.pollTimer = setInterval(() => {
      if (document.hidden) return;
      // 离开大批量页就停表，回来时 enter() 会重新起表。
      if (!$('batch-view').classList.contains('active-view')) { stopPoll(); return; }
      if (b.busy) return;
      refreshBatch(true);
    }, POLL_MS);
  }
  function stopPoll() {
    if (b.pollTimer) { clearInterval(b.pollTimer); b.pollTimer = null; }
  }

  /* ---------- 批次列表 ---------- */
  async function loadBatches(silent = false) {
    try {
      const data = await api(`/api/batches?scope=${scopeParam()}`);
      b.batches = Array.isArray(data.batches) ? data.batches : [];
      b.listLoaded = true;
      renderBatchList();
    } catch (err) {
      b.batches = [];
      b.listLoaded = true;
      renderBatchList(err.message);
      if (!silent) toast(err.message);
    }
  }
  function renderBatchList(error) {
    const root = $('batch-list');
    root.replaceChildren();
    if (error) {
      root.append(el('div', 'empty', `批次列表暂时不可用：${error}`));
      return;
    }
    if (!b.batches.length) {
      root.append(el('div', 'empty', b.listLoaded ? '还没有批次。点右上角「新建批次」开始。' : '正在加载批次…'));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const batch of b.batches) fragment.append(batchCard(batch));
    root.append(fragment);
  }
  function batchCard(batch) {
    const total = num(batch.total_items);
    const done = num(batch.done);
    const failed = num(batch.failed);
    const pending = num(batch.pending, Math.max(0, total - done - failed));
    const state = txt(batch.state) || 'draft';
    const running = !!txt(batch.active_job_id);
    const card = el('article', 'card batch-card');
    const main = el('div', 'batch-card-main');
    const title = el('div', 'batch-card-title');
    title.append(el('b', null, txt(batch.batch_no) || '未命名批次'));
    title.append(el('span', `status ${running ? 'batch-producing' : `batch-${state}`}`, running ? '生产中' : (BATCH_STATE_TEXT[state] || state)));
    main.append(title);
    const meta = [fmtTime(batch.created_at), `已结束 ${done + failed} / ${total}`, `待生产 ${pending}`];
    if (failed) meta.push(`失败 ${failed}`);
    if (App.state.user && App.state.user.role === 'admin' && txt(batch.owner)) meta.push(txt(batch.owner));
    main.append(el('div', 'job-meta', meta.join(' · ')));
    const progress = el('div', 'progress');
    const fill = document.createElement('span');
    fill.style.width = `${total ? Math.min(100, ((done + failed) / total) * 100) : 0}%`;
    progress.append(fill);
    main.append(progress);
    const side = el('div', 'batch-card-side');
    // 删除放在「继续筛选/打开」左边：次要动作靠左、主操作靠右，不抢视觉。
    const del = el('button', 'quiet batch-card-del', '删除');
    del.type = 'button';
    del.setAttribute('aria-label', `删除批次 ${txt(batch.batch_no) || txt(batch.id)}`);
    del.addEventListener('click', () => deleteBatch(batch, del));
    const open = el('button', 'quiet', state === 'draft' ? '继续筛选' : '打开');
    open.type = 'button';
    open.addEventListener('click', () => openBatch(txt(batch.id)));
    side.append(del, open);
    card.append(main, side);
    return card;
  }
  // 删除批次：只删批次记录，不碰已生成的视频。二次确认文案与「锁定清单」同一套 confirm 模式。
  async function deleteBatch(batch, button) {
    const id = txt(batch && batch.id);
    if (!id) return;
    if (button && button.disabled) return;       // 请求进行中，忽略二次点击
    const label = txt(batch.batch_no) || id;
    const ask = `确定删除批次「${label}」？\n\n删除后批次记录不可恢复；已生成的视频不受影响，仍可在历史任务中下载。`;
    if (!confirm(ask)) return;
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    try {
      await api(`/api/batches/${encodeURIComponent(id)}`, { method: 'DELETE' });
      // 删掉的正好是当前打开的批次：退回列表态，避免后续轮询 404。
      if (b.current && txt(b.current.id) === id) {
        stopPoll();
        b.current = null; b.items = []; b.activeJob = null; b.mode = 'list';
      }
      // 从 b.batches 里摘掉再渲染，列表立刻少一张卡；再拉一次接口对齐后端真实数据。
      b.batches = b.batches.filter((item) => txt(item.id) !== id);
      renderBatchList();
      toast('批次已删除');
      await loadBatches(true);                   // silent：失败提示由这里统一给，不重复弹
    } catch (err) {
      const status = Number(err && err.status) || 0;
      const backend = txt(err && err.message);
      if (status === 403) {
        toast('只有创建者或管理员能删除');
      } else if (status === 409) {
        // 生产中的批次由后端给出具体原因，直接原样展示，不自己编文案。
        toast(backend || '批次正在生产中，请等待生产结束或取消任务后再删除');
      } else if (status === 404) {
        toast(backend || '批次不存在，可能已被删除');
        b.batches = b.batches.filter((item) => txt(item.id) !== id);
        renderBatchList();
        await loadBatches(true);
      } else if (!status) {
        toast('删除失败，请检查网络后重试');
      } else {
        toast(backend || '删除失败，请稍后重试');
      }
    } finally {
      // 卡片可能已被重绘掉，按钮还在 DOM 里才需要恢复。
      if (button && button.isConnected) { button.disabled = false; button.removeAttribute('aria-busy'); }
    }
  }
  async function openBatch(id) {
    if (!id) return;
    b.mode = 'view';
    b.formOpen = false;   // 打开已有批次默认收起冻结的设置表单，把高度留给候选
    b.promptEditing = false;
    b.promptDraft = '';
    b.current = b.batches.find((item) => txt(item.id) === id) || { id };
    b.items = [];
    resetCandidates();
    resetItemsView();
    stopPoll();
    renderBatch();
    await loadBatchDetail();
    if (b.current && (txt(b.current.state) || 'draft') === 'draft') {
      const snapshot = b.current.tag_filters;
      b.tagFilters = snapshot && typeof snapshot === 'object' ? snapshot : {};
      syncTagInputs();
      await fetchCandidates();
    }
  }
  function resetItemsView() {
    b.itemFilter = 'all';
    b.itemLimit = ITEM_PAGE;
    b.promptOpen = false;     // 换批次时提示词回到折叠态
    if ($('batch-item-filter')) $('batch-item-filter').value = 'all';
  }
  function newBatch() {
    b.mode = 'new';
    b.current = null;
    b.formOpen = true;    // 新建时必须填表
    b.items = [];
    resetCandidates();
    resetItemsView();
    stopPoll();
    b.activeJob = null;
    resetBatchForm();
    renderBatch();
    const no = $('batch-no');
    if (no) no.focus();
  }

  /* ---------- 历史任务：按批次分组 ---------- */
  function groupSignature(group) {
    return `${num(group.job_count)}|${num(group.success)}|${num(group.failed)}|${num(group.last_at)}`;
  }
  // 历史范围：全部 / 大批量 / 小批量。小批量是平铺列表（一轮 1–3 条，按批次分组反而多点一次），
  // 大批量与全部仍走批次分组；分组数据里带了 small_jobs / big_jobs，用来剔除纯试跑的「未分组」。
  let historyScope = 'all';
  function setHistoryScope(scope, silent) {
    historyScope = ['all', 'big', 'small'].includes(scope) ? scope : 'all';
    document.querySelectorAll('#history-scope button').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.scope === historyScope);
    });
    if (!silent) refreshHistory();
  }
  document.querySelectorAll('#history-scope button').forEach(btn => {
    btn.addEventListener('click', () => setHistoryScope(btn.dataset.scope));
  });

  async function refreshHistory(openId = '') {
    if (!App.state.user) return;
    // 不在历史页时不发请求：切回历史页时 switchView 会再调一次。
    if (!$('history-view').classList.contains('active-view')) return;
    if (historyScope === 'small') {
      $('history-groups').classList.add('hidden');
      $('jobs-list').classList.add('hidden');
      $('history-small').classList.remove('hidden');
      b.historyMode = 'small';
      await App.smallHistory('history').reload(scopeParam());
      if (openId) App.openJob(openId);
      return;
    }
    $('history-small').classList.add('hidden');
    try {
      const data = await api(`/api/history/groups?scope=${scopeParam()}`);
      b.groups = Array.isArray(data.groups) ? data.groups : [];
      b.historyMode = 'groups';
      $('jobs-list').classList.add('hidden');
      $('history-groups').classList.remove('hidden');
      renderGroups();
      const openKeys = [...b.openGroups];
      for (const key of openKeys) {
        const group = b.groups.find((item) => txt(item.key) === key);
        const signature = group ? groupSignature(group) : '';
        if (!b.groupDetails.has(key) || b.groupSig.get(key) !== signature) await loadGroupDetail(key, true);
        b.groupSig.set(key, signature);
      }
      if (openId) App.openJob(openId);
    } catch (err) {
      // 分组接口还没上线/不可用：退回旧的平铺任务列表，绝不白屏。
      b.historyMode = 'flat';
      $('history-groups').classList.add('hidden');
      $('jobs-list').classList.remove('hidden');
      if (!b.flatWarned) {
        b.flatWarned = true;
        toast(`批次分组暂不可用（${err.message}），已回退到任务列表。`);
      }
      await App.refreshFlatJobs(openId);
    }
  }
  function renderGroups() {
    const root = $('history-groups');
    root.replaceChildren();
    // 只看大批量时，把只有试跑记录的「未分组」整组隐掉。
    const groups = historyScope === 'big'
      ? b.groups.filter((group) => num(group.big_jobs, num(group.job_count)) > 0)
      : b.groups;
    if (!groups.length) {
      root.append(el('div', 'empty', historyScope === 'big'
        ? '还没有大批量任务。到「大批量生产」建一个批次试试。'
        : '暂无任务记录。先去「小批量试跑」或「大批量生产」提交一次。'));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const group of groups) fragment.append(groupCard(group));
    root.append(fragment);
  }
  function groupCard(group) {
    const key = txt(group.key);
    const isUngrouped = key === '__ungrouped__' || group.is_ungrouped === true;
    const open = b.openGroups.has(key);
    const card = el('article', 'card history-group');
    card.dataset.key = key;
    const head = el('button', 'history-group-head');
    head.type = 'button';
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    const titleWrap = el('div', 'history-group-title');
    titleWrap.append(el('b', null, isUngrouped ? '未分组（试跑与老任务）' : (txt(group.batch_no) || '未命名批次')));
    const parts = [`${num(group.job_count)} 个任务`, `成功 ${num(group.success)}`, `失败 ${num(group.failed)}`];
    // 管理员看全部用户时，分组标题要能区分是谁的任务（多人混组时后端给"多人"）。
    if (App.state.user && App.state.user.role === 'admin' && group.owner) parts.unshift(`账户 ${group.owner}`);
    titleWrap.append(el('small', null, `${parts.join(' · ')} · ${fmtTime(group.first_at)} → ${fmtTime(group.last_at)}`));
    const side = el('div', 'history-group-side');
    if (num(group.failed)) side.append(el('span', 'status failed', `失败 ${num(group.failed)}`));
    const chevron = el('span', 'history-chevron', open ? '收起' : '展开');
    side.append(chevron);
    head.append(titleWrap, side);
    const body = el('div', 'history-group-body');
    if (!open) body.classList.add('hidden');
    head.addEventListener('click', async () => {
      if (b.openGroups.has(key)) {
        b.openGroups.delete(key);
        body.classList.add('hidden');
        head.setAttribute('aria-expanded', 'false');
        chevron.textContent = '展开';
        return;
      }
      b.openGroups.add(key);
      body.classList.remove('hidden');
      head.setAttribute('aria-expanded', 'true');
      chevron.textContent = '收起';
      if (!b.groupDetails.has(key)) await loadGroupDetail(key, false);
      else renderGroupBody(key, body);
    });
    card.append(head, body);
    if (open) {
      renderGroupBody(key, body);
      if (!b.groupDetails.has(key)) loadGroupDetail(key, false);
    }
    return card;
  }
  function groupBody(key) {
    const cards = $('history-groups').querySelectorAll('.history-group');
    for (const card of cards) if (card.dataset.key === key) return card.querySelector('.history-group-body');
    return null;
  }
  async function loadGroupDetail(key, silent) {
    const body = groupBody(key);
    try {
      const data = await api(`/api/history/groups/${encodeURIComponent(key)}?offset=0&limit=${HISTORY_PAGE}${ownerParam()}`);
      b.groupDetails.set(key, {
        items: Array.isArray(data.items) ? data.items : [],
        total: num(data.total, (data.items || []).length),
        batchNo: txt(data.batch_no),
      });
      if (body) renderGroupBody(key, body);
    } catch (err) {
      if (!silent) toast(err.message);
      if (body) body.replaceChildren(el('p', 'error', err.message));
    }
  }
  async function loadMoreGroup(key, detail) {
    try {
      const data = await api(`/api/history/groups/${encodeURIComponent(key)}?offset=${detail.items.length}&limit=${HISTORY_PAGE}${ownerParam()}`);
      const more = Array.isArray(data.items) ? data.items : [];
      detail.items = detail.items.concat(more);
      detail.total = num(data.total, detail.items.length);
      const body = groupBody(key);
      if (body) renderGroupBody(key, body);
    } catch (err) { toast(err.message); }
  }
  function renderGroupBody(key, body) {
    const detail = b.groupDetails.get(key);
    body.replaceChildren();
    if (!detail) { body.append(el('p', 'muted', '正在加载明细…')); return; }
    if (!detail.items.length) { body.append(el('div', 'empty', '这一组没有明细。')); return; }
    const list = el('div', 'record-list');
    const fragment = document.createDocumentFragment();
    for (const item of detail.items) fragment.append(historyRow(item, key, detail.batchNo));
    list.append(fragment);
    body.append(list);
    const rest = detail.total - detail.items.length;
    if (rest > 0) {
      const more = el('button', 'quiet history-more', `加载更多（还有 ${rest} 条）`);
      more.type = 'button';
      more.addEventListener('click', () => loadMoreGroup(key, detail));
      body.append(more);
    }
    body.append(el('p', 'muted history-group-foot', `共 ${detail.total} 条${detail.batchNo ? ` · 批次 ${detail.batchNo}` : ''}`));
  }
  function historyRow(item, groupKey, batchNo) {
    const status = txt(item.status);
    const row = el('div', 'history-record');
    const nameCell = el('div', 'history-record-name');
    nameCell.append(el('b', null, txt(item.template_name) || txt(item.template_id) || '未命名场景'), el('small', null, txt(item.template_id)));
    const statusCell = el('div', 'history-record-state');
    const statusText = App.statusText || {};
    statusCell.append(el('span', `status ${status}`, statusText[status] || status || '—'));
    const infoCell = el('div', 'history-record-info');
    const parts = [];
    if (txt(item.job_id)) parts.push(`任务 ${txt(item.job_id)}`);
    if (num(item.seconds)) parts.push(`用时 ${num(item.seconds)} 秒`);
    if (txt(item.archive_warning)) parts.push(txt(item.archive_warning));
    if (parts.length) infoCell.append(el('small', null, parts.join(' · ')));
    // 与批次条目同口径：done 的质量提示，状态徽标仍是 success/done
    const warn = txt(item.verify_warn);
    if (warn) infoCell.append(el('small', 'history-verify-warn', `质量提示：${warn}`));
    const actions = el('div', 'record-actions');
    if (txt(item.download_url)) {
      // 对比：一个屏并排看 定妆图 / 生成的视频 / 它用的模板视频
      const cmp = el('button', 'quiet', '对比');
      cmp.type = 'button';
      cmp.title = '同屏对比定妆图、生成的视频与模板视频';
      cmp.addEventListener('click', () => openCompare(item, groupKey, batchNo));
      actions.append(cmp);
      const link = el('a', 'link-btn', '下载');
      link.href = txt(item.download_url);
      actions.append(link);
    } else {
      actions.append(el('span', 'muted', '无文件'));
    }
    if (txt(item.job_id)) {
      const btn = el('button', 'quiet', '任务详情');
      btn.type = 'button';
      btn.addEventListener('click', () => App.openJob(txt(item.job_id)));
      actions.append(btn);
    }
    row.append(nameCell, statusCell, infoCell, actions);
    return row;
  }

  /* ---------- 历史对比：定妆图 / 生成的视频 / 模板视频 同屏 ----------
     全页只建一个覆盖层；打开时才设 src 并自动静音循环播放，关闭即停并清 src。
     定妆图按分组 key（= 批次 id）查 /api/batches/<id> 拿 image_url，结果缓存；
     「未分组」没有批次，图面板直接隐藏，两个视频照常对比。 */
  const compareView = { root: null, title: null, imgPane: null, img: null, gen: null, tpl: null, imgCache: new Map() };

  function ensureCompareView() {
    if (compareView.root) return;
    const c = compareView;
    const root = el('div', 'compare-overlay hidden');
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-label', '生成结果对比');
    const head = el('div', 'compare-head');
    c.title = el('div', 'compare-title');
    const close = el('button', 'quiet compare-close', '关闭 ×');
    close.type = 'button';
    close.addEventListener('click', hideCompareView);
    head.append(c.title, close);
    const grid = el('div', 'compare-grid');

    const makePane = (label) => {
      const pane = el('div', 'compare-pane');
      pane.append(el('div', 'compare-label', label));
      return pane;
    };
    c.imgPane = makePane('定妆图');
    c.img = document.createElement('img');
    c.img.alt = '定妆图';
    c.imgPane.append(c.img);
    const genPane = makePane('生成的视频');
    c.gen = document.createElement('video');
    c.gen.controls = true; c.gen.loop = true; c.gen.muted = true; c.gen.playsInline = true; c.gen.preload = 'auto';
    genPane.append(c.gen);
    const tplPane = makePane('模板视频');
    c.tpl = document.createElement('video');
    c.tpl.controls = true; c.tpl.loop = true; c.tpl.muted = true; c.tpl.playsInline = true; c.tpl.preload = 'auto';
    tplPane.append(c.tpl);

    grid.append(c.imgPane, genPane, tplPane);
    root.append(grid);
    document.body.append(root);
    c.root = root;
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideCompareView(); });
    // 点空白处（遮罩/网格缝隙）也能关，面板内点击不关
    root.addEventListener('click', (e) => { if (e.target === root || e.target === grid) hideCompareView(); });
  }

  async function openCompare(item, groupKey, batchNo) {
    ensureCompareView();
    const c = compareView;
    const name = txt(item.template_name) || txt(item.template_id) || '未命名场景';
    const tid = txt(item.template_id);
    c.title.textContent = `${name}（${tid}）${batchNo ? ` · 批次 ${batchNo}` : ''}`;
    // 定妆图：分组 key 就是批次 id；未分组没有批次，图面板隐藏
    const batchId = groupKey && groupKey !== '__ungrouped__' ? groupKey : '';
    c.img.removeAttribute('src');
    c.imgPane.classList.add('hidden');
    if (batchId) {
      let image_url = c.imgCache.get(batchId);
      if (image_url === undefined) {
        try {
          const data = await api(`/api/batches/${encodeURIComponent(batchId)}`);
          image_url = txt(data.batch && data.batch.image_url);
        } catch { image_url = ''; }
        c.imgCache.set(batchId, image_url);
      }
      if (image_url) {
        c.img.src = image_url;
        c.imgPane.classList.remove('hidden');
      }
    }
    c.gen.src = txt(item.download_url);
    c.tpl.src = tid ? `/api/templates/${encodeURIComponent(tid)}/preview` : '';
    c.root.classList.remove('hidden');
    for (const video of [c.gen, c.tpl]) {
      if (!video.getAttribute('src')) continue;
      const promise = video.play();
      if (promise && typeof promise.catch === 'function') promise.catch(() => { /* 静音被拦极罕见，手动点控件即可 */ });
    }
  }

  function hideCompareView() {
    const c = compareView;
    if (!c.root || c.root.classList.contains('hidden')) return;
    for (const video of [c.gen, c.tpl]) {
      try { video.pause(); } catch (_) { /* 已释放时 pause 可能抛错 */ }
      video.removeAttribute('src');
      if (video.load) video.load();
    }
    c.img.removeAttribute('src');
    c.root.classList.add('hidden');
  }

  /* ---------- 事件绑定与入口 ---------- */
  function bindEvents() {
    bindDropZone();
    // 定妆图缩略图走 /api/uploads/{id}：取不到（会话过期/文件被清理）时安静隐藏，不留破图。
    $('batch-image-preview').addEventListener('error', () => $('batch-image-preview').classList.add('hidden'));
    $('batch-new-btn').addEventListener('click', newBatch);
    $('batch-refresh-btn').addEventListener('click', async () => {
      if (b.mode === 'view' && b.current && b.current.id) await refreshBatch(false);
      else await loadBatches();
    });
    $('batch-create-btn').addEventListener('click', createBatch);
    $('batch-a-toggle').addEventListener('click', () => setBatchFormOpen(!b.formOpen));
    $('batch-exit-btn').addEventListener('click', exitBatch);
    // 锁定/关闭态的「退出批次」：步骤 3 / 4 卡片头各一个，同一个处理函数
    for (const id of ['batch-c-exit-btn', 'batch-d-exit-btn']) {
      const btn = $(id);
      if (btn) btn.addEventListener('click', exitBatch);
    }
    $('batch-prompt-select').addEventListener('change', () => {
      $('batch-prompt-body').value = $('batch-prompt-select').selectedOptions[0]?.dataset.body || '';
      updateBatchPromptCount();
    });
    $('batch-prompt-body').addEventListener('input', updateBatchPromptCount);
    $('batch-tag-toggle').addEventListener('click', toggleTagPanel);
    $('batch-clear-tags').addEventListener('click', () => {
      document.querySelectorAll('#batch-tag-groups .batch-tag-input').forEach((input) => { input.checked = false; });
      b.tagFilters = {};
      scheduleCandidatesRefresh();
    });
    // 三个全选按钮 —— 已选集合跨页累计，「本页」的改动绝不碰其它页。
    // 全选本页：只加当前页的已就绪候选
    $('batch-select-page').addEventListener('click', () => {
      for (const item of b.candPageItems) if (item.ready) b.selected.add(item.id);
      renderCandidates();
    });
    // 反选本页：只反转当前页（未就绪的本来就不能选，跳过）
    $('batch-invert').addEventListener('click', () => {
      for (const item of b.candPageItems) {
        if (!item.ready) continue;
        if (b.selected.has(item.id)) b.selected.delete(item.id);
        else b.selected.add(item.id);
      }
      renderCandidates();
    });
    // 全选全部 N 条：N 是当前筛选命中总数，跨所有页
    $('batch-select-all').addEventListener('click', () => {
      for (const item of candidateMatched()) if (item.ready) b.selected.add(item.id);
      renderCandidates();
    });
    $('batch-only-selected').addEventListener('change', (event) => {
      const on = event.target.checked;
      // 打开「只看已选」从第 1 页看起；关掉时回到打开前的页码（页数变了，renderCandidates 会夹住）
      if (on) { b.candPageBackup = b.candPage; b.candPage = 1; }
      else { b.candPage = b.candPageBackup || 1; b.candPageBackup = 0; }
      b.onlySelected = on;
      renderCandidates();
    });
    // 评分筛选 / 排序：走服务端重拉（和标签筛选一个待遇），换条件后回到第 1 页由 fetchCandidates 复位
    $('batch-rating-filter').addEventListener('change', (event) => {
      b.candRatingMin = Math.min(5, Math.max(0, num(event.target.value, 0)));
      fetchCandidates().catch(() => {});
    });
    $('batch-rating-sort').addEventListener('change', (event) => {
      b.candSort = event.target.value === 'rating' ? 'rating' : '';
      fetchCandidates().catch(() => {});
    });
    $('batch-page-prev').addEventListener('click', () => goCandPage(b.candPage - 1));
    $('batch-page-next').addEventListener('click', () => goCandPage(b.candPage + 1));
    $('batch-lock-btn').addEventListener('click', lockBatch);
    $('batch-item-filter').addEventListener('change', (event) => {
      b.itemFilter = event.target.value || 'all';
      b.itemLimit = ITEM_PAGE;
      renderItems();
    });
    $('batch-items-more').addEventListener('click', () => { b.itemLimit += ITEM_PAGE; renderItems(); });
    // 自定条数：点「生产」或输入框回车都走同一条校验（下限/剩余/上限）
    const applyBtn = $('batch-produce-apply');
    if (applyBtn) applyBtn.addEventListener('click', produceCustom);
    const countInput = $('batch-produce-count');
    if (countInput) {
      countInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); produceCustom(); }
      });
      // 失焦时把明显越界的值收拢到合法区间，让输入框里看到的就等于会提交的
      countInput.addEventListener('blur', () => {
        const range = produceRange(b.current);
        const typed = Math.trunc(num(countInput.value, 0));
        if (!typed) return;
        const fixed = Math.max(range.min, Math.min(typed, Math.max(range.min, range.cap)));
        if (fixed !== typed) countInput.value = String(fixed);
      });
    }

    // 预览播放器：失败要有交代，成功/被拦各有提示，绝不留一块黑屏
    const video = $('batch-preview-video');
    video.addEventListener('error', () => {
      if (!video.getAttribute('src')) return;
      showPreviewNote('这条候选的视频加载失败（模板可能已下架或文件缺失），换一条试试。', true);
    });
    video.addEventListener('loadeddata', () => showPreviewNote(''));
    video.addEventListener('playing', () => showPreviewNote(''));
    window.addEventListener('resize', queueCandLayout);
    // 悬浮预览小窗跟着鼠标走：页面滚动（含内层容器）或窗口缩放时直接收起
    window.addEventListener('scroll', hideHoverPreview, true);
    window.addEventListener('resize', hideHoverPreview);
    watchCandGrid();
  }

  /* 候选网格的「盒子」尺寸一变就重核每页条数。
     只靠 window.resize 不够：等高之后候选区高度是被右列带着走的 —— 提示行出现、播放器文件名换行、
     字体回流、表单/标签面板开合都可能让网格变高变矮，而这些都不触发窗口事件。
     不补这一层就会出现「网格高了 20px 但一页还是 13 行，底下留一条空」的滞后。
     回调统一走 queueCandLayout（rAF 合并），且只在容量真的变了才重建，不会和 ResizeObserver 互相触发。 */
  function watchCandGrid() {
    const grid = $('batch-cand-grid');
    if (!grid || typeof ResizeObserver !== 'function' || b.candObserver) return;
    b.candObserver = new ResizeObserver(() => queueCandLayout());
    b.candObserver.observe(grid);
  }

  let initialized = false;
  function init() {
    if (initialized) return;
    initialized = true;
    bindEvents();
    fillPromptSelect();
    renderBatchTagFilters();
    renderBatch();
  }
  function onTemplates() {
    if (!initialized) fillPromptSelect();
    renderBatchTagFilters();
    if (b.mode === 'view' && b.current) renderBatch();
  }
  function enter() {
    init();
    // 切回大批量页时视口/页头可能变了，重新量一次候选区高度与每页条数
    queueCandLayout();
    if (b.mode === 'list') loadBatches();
    else refreshBatch(true);
  }

  window.BatchUI = {
    enter,
    onTemplates,
    refreshHistory,
    // 供调试/外部刷新使用
    refresh: () => (b.mode === 'list' ? loadBatches() : refreshBatch(false)),
  };
})();
