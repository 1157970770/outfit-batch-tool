/* 视频标签页：列表 / 筛选 / 单条与批量打标 / 弃用恢复 / 标签库维护。
 *
 * 同步策略（用户拍定：最省资源）：不轮询。
 *   · 进页面时带 revision 拉一次快照，没变化服务端只回 {unchanged:true}（约 60 B）
 *   · 保存后用响应里的 updated 行局部替换，不重拉整表
 *   · 并发靠 revision 乐观锁：409 就提示并自动重拉
 * 安全：全部 addEventListener + textContent，没有内联脚本（CSP: script-src 'self'）。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const app = () => window.OutfitApp || {};
  const api = (path, options) => app().api(path, options);
  const toast = (text) => (app().toast ? app().toast(text) : console.log(text));

  const state = {
    enabled: false, loaded: false, loading: false,
    revision: 0, updatedAt: 0, updatedBy: '',
    videos: [], byId: new Map(), categories: [],
    library: { tags: [], categories: {}, usage: {} },
    stats: {}, filters: {}, query: '', status: 'active', sort: 'id', onlyReady: false,
    ratingFilter: 'all',
    page: 1, pageSize: 50,
    selected: new Set(), matched: [],
    previewId: '', mode: 'replace', lastMulti: false, pending: new Set(), drawerOpen: false,
    versions: [], versionOpen: false,
  };

  /* ---------------------------------------------------------------- 工具 */
  function fmtSize(value) {
    const size = Number(value);
    return Number.isFinite(size) && size > 0 ? `${size.toFixed(1)} MB` : '—';
  }

  function fmtTime(seconds) {
    if (!seconds) return '';
    const date = new Date(Number(seconds) * 1000);
    if (Number.isNaN(date.getTime())) return '';
    const pad = (value) => String(value).padStart(2, '0');
    return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function rowState(video) {
    if (video.deprecated) return { text: '已弃用', cls: 'is-off' };
    if (!video.tags.length) return { text: '未打标', cls: 'is-off' };
    if (video.ready) return { text: '可生成', cls: 'is-ok' };
    return { text: video.upload_state || '待上传', cls: '' };
  }

  /* -------------------------------------------------------------- 评分 */
  const RATING_MAX = 5;   // 展示 5 颗星
  const RATING_MIN = 1;   // 1-5 星全部可打（0=清除）

  function buildRatingCell(video) {
    const cell = document.createElement('span');
    cell.className = 'tag-row-rating';
    const rating = Number(video.rating || 0);
    for (let star = 1; star <= RATING_MAX; star++) {
      const starEl = document.createElement('button');
      starEl.type = 'button';
      starEl.textContent = '★';
      starEl.className = 'rating-star' + (star <= rating ? ' is-on' : '');
      if (star < RATING_MIN) {
        starEl.disabled = true;
        starEl.classList.add('is-locked');
        starEl.title = '评分从 3 星起';
      } else {
        starEl.title = rating === star ? `清除评分（${video.id}）` : `评为 ${star} 星（${video.id}）`;
        starEl.addEventListener('click', (event) => {
          event.stopPropagation();  // 别触发行点击 → 预览
          rateVideos([video], rating === star ? 0 : star);
        });
      }
      cell.append(starEl);
    }
    return cell;
  }

  async function rateVideos(videos, value) {
    const table = $('tag-table');
    const top = table.scrollTop;  // 保存后整表重绘，把滚动位置还回去
    const data = await post('/api/tags/videos/rating', { ids: videos.map(v => v.id), rating: value });
    table.scrollTop = top;
    if (data) toast(value ? `已评 ${value} 星（${videos.length} 条）` : `已清除评分（${videos.length} 条）`);
  }

  function activeLibrary() {
    return state.library && Array.isArray(state.library.tags) ? state.library.tags : [];
  }

  function categoryOf(tag) {
    return (state.library.categories && state.library.categories[tag])
      || (state.videos.find(v => v.tag_categories && v.tag_categories[tag]) || {}).tag_categories?.[tag]
      || '场景';
  }

  function tagsByCategory() {
    const groups = {};
    for (const category of state.categories) groups[category] = [];
    for (const tag of activeLibrary()) {
      const category = groups[categoryOf(tag)] ? categoryOf(tag) : state.categories[0];
      (groups[category] = groups[category] || []).push(tag);
    }
    return groups;
  }

  /* ---------------------------------------------------------------- 数据 */
  function applySnapshot(data) {
    state.enabled = !!data.enabled;
    state.revision = Number(data.revision || 0);
    state.updatedAt = Number(data.updated_at || 0);
    state.updatedBy = data.updated_by || '';
    state.categories = data.categories || [];
    state.library = data.library || { tags: [], categories: {}, usage: {} };
    state.videos = data.videos || [];
    state.byId = new Map(state.videos.map(video => [video.id, video]));
    state.stats = data.stats || {};
    // 别人删掉的视频不能继续留在勾选里
    for (const id of [...state.selected]) if (!state.byId.has(id)) state.selected.delete(id);
    if (state.previewId && !state.byId.has(state.previewId)) state.previewId = '';
    if (!state.enabled) {
      $('tag-table').replaceChildren();
      $('tag-table').append(emptyNode(
        '标签真源尚未启用。请在主机执行 python run_web.py --import-tags，然后刷新本页。'));
    }
    renderAll();
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      // 首次/强制刷新不带 rev → 取全量；之后带 rev → 没变化只回 60 B
      const rev = state.loaded ? state.revision : '';
      const data = await api(`/api/tags/snapshot?rev=${rev}`);
      if (!data.unchanged) applySnapshot(data);
      state.loaded = true;
    } catch (err) {
      toast(err.message);
    } finally {
      state.loading = false;
    }
  }

  function replaceRows(rows) {
    for (const row of rows || []) {
      const id = String(row.id || '');
      if (!id) continue;
      const index = state.videos.findIndex(video => video.id === id);
      if (index >= 0) state.videos[index] = row;
      else state.videos.push(row);
      state.byId.set(id, row);
    }
  }

  function applyWrite(data) {
    if (!data) return;
    state.revision = Number(data.revision || state.revision);
    state.stats = data.stats || state.stats;
    if (data.library) {
      state.library = data.library;
      const order = data.library.category_order;
      if (Array.isArray(order) && order.length) {
        state.categories = order.slice();
        renderCategoryOptions();
      }
    }
    replaceRows(data.updated);
    renderAll();
    if (app().reloadTemplates) Promise.resolve(app().reloadTemplates()).catch(() => {});
  }

  async function post(path, body) {
    try {
      const data = await api(path, { method: 'POST', body: JSON.stringify({ revision: state.revision, ...body }) });
      applyWrite(data);
      return data;
    } catch (err) {
      if (err.status === 409) {
        toast('有人刚改过，正在刷新到最新…');
        state.revision = Number(err.data?.revision || 0);
        state.loaded = false;
        await load();
        return null;
      }
      toast(err.message);
      return null;
    }
  }

  /* ---------------------------------------------------------------- 渲染 */
  function emptyNode(text) {
    const div = document.createElement('div');
    div.className = 'tag-empty';
    div.textContent = text;
    return div;
  }

  function renderAll() {
    renderRevision();
    renderFilters();
    applyFilters();
    renderChips();
    renderTable();
    renderPager();
    renderBulk();
    renderEditor();
    if (state.drawerOpen) renderLibrary();
  }

  function renderRevision() {
    const parts = [];
    if (state.enabled) {
      parts.push(`r${state.revision}`);
      if (state.updatedAt) parts.push(fmtTime(state.updatedAt));
      if (state.updatedBy) parts.push(state.updatedBy);
    } else {
      parts.push('未启用');
    }
    $('tag-rev').textContent = parts.join(' · ');
    const stats = state.stats || {};
    if (state.enabled && stats.total) {
      $('tag-summary').textContent =
        `共 ${stats.total} · 在用 ${stats.active} · 弃用 ${stats.deprecated || 0} · 未打标 ${stats.untagged || 0} · 可生成 ${stats.ready || 0}`;
    } else {
      $('tag-summary').textContent = '';
    }
  }

  function renderFilters() {
    const card = $('tagf-card');
    const root = $('tagf-groups');
    root.replaceChildren();
    const groups = tagsByCategory();
    const hasAny = Object.values(groups).some(list => list.length);
    card.classList.toggle('hidden', !hasAny || card.dataset.open !== '1');
    $('tag-filter-toggle').setAttribute('aria-expanded', card.dataset.open === '1' ? 'true' : 'false');
    if (!hasAny) return;
    for (const category of state.categories) {
      const tags = groups[category] || [];
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
        input.type = 'checkbox';
        input.className = 'tag-filter-input';
        input.dataset.category = category;
        input.value = tag;
        input.checked = (state.filters[category] || []).includes(tag);
        input.addEventListener('change', () => {
          const current = new Set(state.filters[category] || []);
          if (input.checked) current.add(tag); else current.delete(tag);
          state.filters[category] = [...current];
          state.page = 1;
          renderAll();
        });
        const text = document.createElement('span');
        text.textContent = tag;
        label.append(input, text);
        options.append(label);
      }
      group.append(options);
      root.append(group);
    }
  }

  function renderChips() {
    const root = $('tag-chips');
    root.replaceChildren();
    const chosen = [];
    for (const [category, values] of Object.entries(state.filters)) {
      for (const tag of values || []) chosen.push({ category, tag });
    }
    if (!chosen.length) return;
    const label = document.createElement('span');
    label.className = 'batch-chips-label';
    label.textContent = '已选条件';
    root.append(label);
    for (const { category, tag } of chosen) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'batch-chip is-on';
      chip.textContent = `${category}：${tag} ×`;
      chip.addEventListener('click', () => {
        state.filters[category] = (state.filters[category] || []).filter(value => value !== tag);
        state.page = 1;
        renderAll();
      });
      root.append(chip);
    }
    const summary = $('tagf-summary');
    if (summary) summary.textContent = `命中 ${state.matched.length} 条`;
  }

  function applyFilters() {
    const query = state.query.trim().toLowerCase();
    const list = state.videos.filter((video) => {
      if (state.status === 'active' && (video.deprecated || !video.tags.length)) return false;
      if (state.status === 'deprecated' && !video.deprecated) return false;
      if (state.status === 'untagged' && (video.deprecated || video.tags.length)) return false;
      if (state.onlyReady && !video.ready) return false;
      if (state.ratingFilter !== 'all') {
        const rating = Number(video.rating || 0);
        if (state.ratingFilter === '0') { if (rating) return false; }
        else if (rating !== Number(state.ratingFilter)) return false;
      }
      if (query) {
        const haystack = `${video.id} ${video.name} ${video.relative_path}`.toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      for (const [category, values] of Object.entries(state.filters)) {
        if (!values || !values.length) continue;
        const hit = video.tags.some(tag => values.includes(tag) && categoryOf(tag) === category);
        if (!hit) return false;
      }
      return true;
    });
    const ORDER = {
      id: (a, b) => Number(a.id) - Number(b.id),
      uploaded: (a, b) => {
        const av = Number(a.uploaded_at || 0);
        const bv = Number(b.uploaded_at || 0);
        // 有真实上传时间的一律按它排；都没有时退回最近修改时间，至少顺序稳定可预期
        if (av !== bv) return bv - av;
        return Number(b.touched_at || 0) - Number(a.touched_at || 0);
      },
      size: (a, b) => Number(b.size_mb || 0) - Number(a.size_mb || 0),
      tags: (a, b) => b.tags.length - a.tags.length,
      updated: (a, b) => Number(b.touched_at || 0) - Number(a.touched_at || 0),
    };
    list.sort(ORDER[state.sort] || ORDER.id);
    state.matched = list;
    const pages = Math.max(1, Math.ceil(list.length / state.pageSize));
    if (state.page > pages) state.page = pages;
  }

  function renderTable() {
    const table = $('tag-table');
    table.replaceChildren();
    if (!state.enabled) {
      if (!table.childElementCount) table.append(emptyNode('标签真源尚未启用。'));
      return;
    }
    const head = document.createElement('div');
    head.className = 'tag-row is-head';
    ['', 'ID', '文件名', '标签', '评分', '状态', '大小', ''].forEach((text) => {
      const cell = document.createElement('span');
      cell.textContent = text;
      head.append(cell);
    });
    table.append(head);

    const start = (state.page - 1) * state.pageSize;
    const items = state.matched.slice(start, start + state.pageSize);
    if (!items.length) {
      table.append(emptyNode('当前筛选没有匹配的视频。'));
      return;
    }
    for (const video of items) {
      const row = document.createElement('div');
      row.className = 'tag-row';
      row.dataset.vid = video.id;
      if (state.selected.has(video.id)) row.classList.add('is-picked');
      if (video.id === state.previewId) row.classList.add('is-playing');
      if (video.deprecated) row.classList.add('is-deprecated');

      const checkCell = document.createElement('span');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = state.selected.has(video.id);
      box.setAttribute('aria-label', `选择 ${video.id}`);
      box.addEventListener('click', (event) => event.stopPropagation());
      box.addEventListener('change', () => {
        const before = state.selected.size;
        if (box.checked) state.selected.add(video.id); else state.selected.delete(video.id);
        row.classList.toggle('is-picked', box.checked);
        // 单条 → 批量时清空勾选，避免把预览那条的标签带到别人身上
        if (before <= 1 && state.selected.size > 1) state.pending = new Set();
        if (state.selected.size === 1 && before !== 1) {
          const only = state.byId.get([...state.selected][0]);
          state.pending = new Set(only ? only.tags : []);
        }
        if (!state.selected.size) {
          const current = state.byId.get(state.previewId);
          state.pending = new Set(current ? current.tags : []);
        }
        renderBulk();
        renderEditor();
      });
      checkCell.append(box);

      const idCell = document.createElement('span');
      idCell.className = 'tag-row-id';
      idCell.textContent = video.id;

      const nameCell = document.createElement('span');
      nameCell.className = 'tag-row-name';
      // 早期上传的行没有 uploaded_at（字段是后加的），退回最近修改时间，排序与「新」标记才一致
      const uploadedAt = Number(video.uploaded_at || 0);
      const touchedAt = Number(video.touched_at || 0);
      // 「新」徽标只认真实上传时间：touched_at 是改标签时刷的，拿它冒充会把老素材标成新上传
      const isNew = uploadedAt > 0 && (Date.now() / 1000 - uploadedAt) < 72 * 3600;
      if (isNew) {
        const badge = document.createElement('span');
        badge.className = 'tag-new';
        badge.textContent = '新';
        nameCell.append(badge);
      }
      nameCell.append(document.createTextNode(video.name || video.relative_path || '(无文件名)'));
      nameCell.title = uploadedAt
        ? `上传于 ${fmtDateTime(uploadedAt)}`
        : (touchedAt ? `上传时间未记录（最近修改于 ${fmtDateTime(touchedAt)}）`
          : (video.relative_path || video.name || ''));

      const tagsCell = document.createElement('span');
      tagsCell.className = 'tag-row-tags';
      if (video.tags.length) {
        // 全部标签都要看得见：不做 slice 折叠，放不下就换行
        for (const tag of video.tags) {
          const chip = document.createElement('span');
          chip.className = 'chip';
          chip.textContent = tag;
          chip.title = `${video.tag_categories?.[tag] || ''}：${tag}`;
          tagsCell.append(chip);
        }
      } else {
        tagsCell.textContent = '—';
      }

      const ratingCell = buildRatingCell(video);

      const stateCell = document.createElement('span');
      const info = rowState(video);
      stateCell.className = `tag-row-state ${info.cls}`;
      stateCell.textContent = info.text;
      if (video.deprecated && video.deprecated_by) stateCell.title = `${video.deprecated_by} 弃用`;

      const sizeCell = document.createElement('span');
      sizeCell.className = 'tag-row-state';
      sizeCell.textContent = fmtSize(video.size_mb);

      // 行内快捷「弃 / 恢」：不用先选中，点一下就和大按钮走同一套确认与接口
      const actCell = document.createElement('span');
      actCell.className = 'tag-row-act';
      const act = document.createElement('button');
      act.type = 'button';
      act.className = 'tag-mini' + (video.deprecated ? ' is-restore' : '');
      act.textContent = video.deprecated ? '恢' : '弃';
      act.title = video.deprecated
        ? '恢复使用：重新进入生产候选（标签不变）'
        : '弃用：不再参与生产，可随时恢复；不删除云端对象与本机文件';
      act.addEventListener('click', (event) => {
        event.stopPropagation();
        setDeprecated(!video.deprecated, [video.id]);
      });
      actCell.append(act);

      row.append(checkCell, idCell, nameCell, tagsCell, ratingCell, stateCell, sizeCell, actCell);
      row.addEventListener('click', () => selectPreview(video.id));
      table.append(row);
    }
  }

  function renderPager() {
    const pages = Math.max(1, Math.ceil(state.matched.length / state.pageSize));
    $('tag-page-info').textContent = `第 ${state.page} / ${pages} 页`;
    $('tag-pager').classList.toggle('hidden', state.matched.length <= state.pageSize);
    $('tag-count').textContent = `筛选命中 ${state.matched.length} 条 / 共 ${state.videos.length} 条`;
    $('tag-page-prev').disabled = state.page <= 1;
    $('tag-page-next').disabled = state.page >= pages;
    $('tag-select-all').textContent = `全选筛选结果 ${state.matched.length} 条`;
  }

  function pageIds() {
    const start = (state.page - 1) * state.pageSize;
    return state.matched.slice(start, start + state.pageSize).map(video => video.id);
  }

  function renderBulk() {
    const count = state.selected.size;
    $('tag-bulk').classList.toggle('hidden', count === 0);
    $('tag-bulk-count').textContent = `已选 ${count} 条`;
  }

  function editorTargets() {
    if (state.selected.size) return [...state.selected];
    return state.previewId ? [state.previewId] : [];
  }

  function renderEditor() {
    const box = $('tag-editor');
    const empty = $('tag-editor-empty');
    const targets = editorTargets();
    if (!targets.length) {
      box.classList.add('hidden');
      empty.classList.remove('hidden');
      return;
    }
    box.classList.remove('hidden');
    empty.classList.add('hidden');
    const rows = targets.map(id => state.byId.get(id)).filter(Boolean);
    if (!rows.length) {
      box.classList.add('hidden');
      empty.classList.remove('hidden');
      return;
    }
    $('tag-editor-title').textContent = rows.length === 1
      ? `编辑 ${rows[0].id} · ${rows[0].name || ''}`
      : `批量编辑 ${rows.length} 条`;
    // 单条：勾选即最终结果（覆盖），不摆方式选择；多条才给「追加/移除/覆盖」
    const multi = rows.length > 1;
    if (multi && !state.lastMulti) state.mode = 'add';
    if (!multi) state.mode = 'replace';
    state.lastMulti = multi;
    $('tag-mode-wrap').classList.toggle('hidden', !multi);
    $('tag-mode').value = state.mode;

    const groups = tagsByCategory();
    const root = $('tag-editor-groups');
    root.replaceChildren();
    for (const category of state.categories) {
      const tags = groups[category] || [];
      if (!tags.length) continue;
      const group = document.createElement('div');
      group.className = 'tag-editor-group';
      const title = document.createElement('b');
      title.textContent = category;
      group.append(title);
      const options = document.createElement('div');
      options.className = 'tag-editor-options';
      for (const tag of tags) {
        const label = document.createElement('label');
        label.className = 'tag-option';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = state.pending.has(tag);
        input.addEventListener('change', () => {
          if (input.checked) state.pending.add(tag); else state.pending.delete(tag);
          label.classList.toggle('is-on', input.checked);
          updateEditorHint(rows);
        });
        const text = document.createElement('span');
        text.textContent = tag;
        label.classList.toggle('is-on', input.checked);
        label.append(input, text);
        options.append(label);
      }
      group.append(options);
      root.append(group);
    }

    renderEditorRating(rows);

    const allDeprecated = rows.every(row => row.deprecated);
    const many = rows.length > 1;
    const deprecateBtn = $('tag-deprecate-btn');
    const restoreBtn = $('tag-restore-btn');
    deprecateBtn.textContent = many ? `弃用 ${rows.length} 条` : '弃用此视频';
    restoreBtn.textContent = many ? `恢复 ${rows.length} 条` : '恢复使用';
    deprecateBtn.classList.toggle('hidden', allDeprecated);
    restoreBtn.classList.toggle('hidden', !allDeprecated);
    updateEditorHint(rows);
  }

  /* 编辑面板的大号打分组件：与行内小星星同一套接口，单条/批量都作用到当前编辑对象 */
  function renderEditorRating(rows) {
    const box = $('tag-editor-rating');
    const note = $('tag-editor-rating-note');
    box.replaceChildren();
    if (!rows || !rows.length) { if (note) note.textContent = ''; return; }
    const ratings = rows.map(row => Number(row.rating || 0));
    const uniform = ratings.every(r => r === ratings[0]) ? ratings[0] : -1;
    if (uniform < 0) { if (note) note.textContent = '选中视频评分不一致'; }
    else if (note) note.textContent = uniform ? `当前 ${uniform} 星` : '未打分';
    for (let star = 1; star <= RATING_MAX; star++) {
      const starEl = document.createElement('button');
      starEl.type = 'button';
      starEl.textContent = '★';
      starEl.className = 'rating-star rating-star-lg' + (uniform >= star ? ' is-on' : '');
      if (star < RATING_MIN) {
        starEl.disabled = true;
        starEl.classList.add('is-locked');
        starEl.title = '评分从 3 星起';
      } else {
        starEl.title = uniform === star ? `清除评分（${rows.length} 条）` : `评为 ${star} 星（${rows.length} 条）`;
        starEl.addEventListener('click', (event) => {
          event.stopPropagation();
          rateVideos(rows, uniform === star ? 0 : star);
        });
      }
      box.append(starEl);
    }
  }

  function updateEditorHint(rows) {    const hint = $('tag-editor-hint');
    const chosen = [...state.pending];
    if (!rows || !rows.length) { hint.textContent = ''; return; }
    if (state.mode === 'replace') {
      hint.textContent = chosen.length
        ? `覆盖后这 ${rows.length} 条只剩：${chosen.join('、')}`
        : `覆盖为空会让这 ${rows.length} 条退出候选（等同未打标）`;
    } else if (state.mode === 'add') {
      hint.textContent = chosen.length ? `给这 ${rows.length} 条追加：${chosen.join('、')}` : '勾选要追加的标签';
    } else {
      hint.textContent = chosen.length ? `从这 ${rows.length} 条移除：${chosen.join('、')}` : '勾选要移除的标签';
    }
  }

  function selectPreview(id) {
    state.previewId = id;
    // 不重绘整表：长列表里点一行就跳回顶部很难用；只原地切换「正在播放」高亮
    const table = $('tag-table');
    const prev = table.querySelector('.tag-row.is-playing');
    if (prev) prev.classList.remove('is-playing');
    const now = table.querySelector(`.tag-row[data-vid="${CSS.escape(String(id))}"]`);
    if (now) now.classList.add('is-playing');
    const video = state.byId.get(id);
    const player = $('tag-preview-video');
    const note = $('tag-preview-note');
    if (!video) return;
    $('tag-preview-name').textContent = `${video.id} · ${video.name || ''} · ${fmtSize(video.size_mb)}`;
    note.classList.add('hidden');
    if (!video.ready) {
      player.removeAttribute('src');
      player.classList.add('hidden');
      note.textContent = video.deprecated
        ? '已弃用。恢复后可继续使用；预览需要它曾上传到云端。'
        : '这条视频还没上传到云端，暂时无法预览，但可以先打标签。';
      note.classList.remove('hidden');
    } else {
      player.classList.remove('hidden');
      note.classList.add('hidden');
      // 浏览器解不了 H.265（这批素材约七成是 HEVC）时不要在空白播放器上干等
      player.onloadeddata = () => {
        note.classList.add('hidden');
      };
      player.onerror = () => {
        const code = player.error && player.error.code;
        player.classList.add('hidden');
        note.textContent = code === 4
          ? `${video.name || video.id} 的编码浏览器不支持（多为 H.265 / HEVC）。用桌面打标器可以预览，或请管理员转成 H.264。`
          : `这条视频加载失败（${code || '未知原因'}），换一条试试或刷新页面。`;
        note.classList.remove('hidden');
      };
      player.src = `/api/templates/${encodeURIComponent(video.id)}/preview?allow_deprecated=1`;
      player.play().catch(() => {});
    }
    // 单条编辑时默认带上它现有的标签；批量时清空，避免误覆盖
    state.pending = new Set(state.selected.size ? [] : video.tags);
    state.mode = state.selected.size ? 'add' : 'replace';
    // 不重绘整表：长列表里点一行就跳回顶部很难用
    renderEditor();
  }

  /* ---------------------------------------------------------------- 写操作 */
  async function saveTags() {
    const targets = editorTargets();
    if (!targets.length) return;
    const tags = [...state.pending];
    if (state.mode === 'replace' && !tags.length) {
      const ok = confirm(`这 ${targets.length} 条会被清空全部标签，从而退出小批量/大批量候选（等同未打标）。\n\n要保留记录请改用「弃用」。仍要继续吗？`);
      if (!ok) return;
    }
    if (state.mode !== 'replace' && !tags.length) {
      toast('请先勾选要' + (state.mode === 'add' ? '追加' : '移除') + '的标签');
      return;
    }
    const data = await post('/api/tags/videos/tags', { ids: targets, tags, mode: state.mode });
    if (data) {
      toast(`已保存：${data.changed} 条更新`);
      state.pending = new Set();
      renderEditor();
    }
  }

  async function setDeprecated(flag, idsArg) {
    const targets = (idsArg && idsArg.length) ? idsArg : editorTargets();
    if (!targets.length) return;
    if (flag) {
      let impact = null;
      try {
        impact = await api('/api/tags/impact', {
          method: 'POST', body: JSON.stringify({ action: 'deprecate', ids: targets }),
        });
      } catch (_) { /* 提醒拿不到不阻断弃用 */ }
      const batches = (impact && impact.locked_batches) || [];
      const extra = batches.length
        ? `\n\n注意：${batches.length} 个未完成批次用到这些模板（${batches.map(b => b.batch_no).join('、')}），`
          + '生产时会被标为「模板已不可用」。'
        : '';
      const ok = confirm(
        `将弃用 ${targets.length} 条视频：不再出现在小批量/大批量候选里；`
        + `云端对象与本机文件都不会删除，随时可恢复。${extra}\n\n确认弃用？`);
      if (!ok) return;
    }
    const data = await post('/api/tags/videos/deprecate', { ids: targets, deprecated: flag });
    if (data) toast(flag ? `已弃用 ${data.changed} 条` : `已恢复 ${data.changed} 条`);
  }

  async function libraryAction(body, confirmText) {
    if (['merge', 'delete', 'rename'].includes(body.action)) {
      let impact = null;
      try {
        impact = await api('/api/tags/impact', { method: 'POST', body: JSON.stringify(body) });
      } catch (err) {
        toast(err.message);
        return null;
      }
      const extra = impact.locked_batches && impact.locked_batches.length
        ? `\n另有 ${impact.locked_batches.length} 个未完成批次用到它，生产时会标为「模板已不可用」。`
        : '';
      const untagged = impact.becomes_untagged
        ? `，其中 ${impact.becomes_untagged} 条会变成未打标（不再进候选）` : '';
      const ok = confirm(`${confirmText}\n影响 ${impact.affected} 条视频${untagged}。${extra}\n\n确认继续？`);
      if (!ok) return null;
      body.confirm = true;
    }
    const data = await post('/api/tags/library', body);
    if (data) toast(data.action === 'create' ? '标签已新建' : `已${labelOf(data.action)}，影响 ${data.affected || 0} 条`);
    return data;
  }

  function labelOf(action) {
    return {
      create: '新建', rename: '改名', set_category: '改分类', merge: '合并', delete: '删除',
      category_create: '新增', category_rename: '改名', category_delete: '删除',
    }[action] || '更新';
  }

  /* ---------------------------------------------------------------- 大类管理 */
  function renderCategoryOptions() {
    const select = $('tag-lib-cat');
    if (!select) return;
    const current = select.value;
    select.replaceChildren();
    for (const category of state.categories) {
      const option = document.createElement('option');
      option.value = category;
      option.textContent = category;
      select.append(option);
    }
    if (state.categories.includes(current)) select.value = current;
  }

  function renderCategoryList() {
    const root = $('tag-cat-list');
    if (!root) return;
    root.replaceChildren();
    for (const category of state.categories) {
      const tagCount = activeLibrary().filter(tag => categoryOf(tag) === category).length;
      const row = document.createElement('div');
      row.className = 'tag-cat-row';

      const name = document.createElement('span');
      name.className = 'tag-cat-name';
      name.textContent = category;
      const count = document.createElement('em');
      count.textContent = `${tagCount} 个标签`;
      name.append(count);

      const actions = document.createElement('span');
      actions.className = 'tag-cat-actions';
      const rename = document.createElement('button');
      rename.type = 'button'; rename.className = 'quiet'; rename.textContent = '改名';
      rename.addEventListener('click', () => {
        const value = prompt(`把大类「${category}」改成：`, category);
        if (!value || value.trim() === category) return;
        libraryAction({ action: 'category_rename', name: category, new_name: value.trim() },
          `大类改名为「${value.trim()}」？该大类下的 ${tagCount} 个标签会跟着改。`);
      });
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'quiet'; remove.textContent = '删除';
      remove.addEventListener('click', () => {
        if (tagCount) { toast(`「${category}」下还有 ${tagCount} 个标签，先把标签改到别的类或删掉`); return; }
        if (!confirm(`删除大类「${category}」？`)) return;
        libraryAction({ action: 'category_delete', name: category }, `删除大类「${category}」？`);
      });
      actions.append(rename, remove);
      row.append(name, actions);
      root.append(row);
    }
    if (!root.childElementCount) root.append(emptyNode('还没有大类'));
  }

  /* ---------------------------------------------------------------- 标签库抽屉 */
  function renderLibrary() {
    renderCategoryList();
    renderCategoryOptions();
    const root = $('tag-lib-list');
    root.replaceChildren();
    const groups = tagsByCategory();
    const usage = state.library.usage || {};
    for (const category of state.categories) {
      const tags = (groups[category] || []).slice().sort();
      if (!tags.length) continue;
      const title = document.createElement('div');
      title.className = 'tag-lib-group-title';
      title.textContent = `${category}（${tags.length}）`;
      root.append(title);
      for (const tag of tags) {
        const row = document.createElement('div');
        row.className = 'tag-lib-row';

        const name = document.createElement('span');
        name.className = 'tag-lib-name';
        name.textContent = tag;
        name.title = tag;
        const count = document.createElement('em');
        count.textContent = `${usage[tag] || 0} 条`;
        name.append(count);

        const select = document.createElement('select');
        select.setAttribute('aria-label', `${tag} 的分类`);
        for (const option of state.categories) {
          const item = document.createElement('option');
          item.value = option;
          item.textContent = option;
          item.selected = option === category;
          select.append(item);
        }
        select.addEventListener('change', () => {
          libraryAction({ action: 'set_category', name: tag, category: select.value },
            `把「${tag}」改到「${select.value}」分类？`);
        });

        const actions = document.createElement('span');
        actions.className = 'tag-lib-actions';
        const rename = document.createElement('button');
        rename.type = 'button'; rename.className = 'quiet'; rename.textContent = '改名';
        rename.addEventListener('click', () => {
          const value = prompt(`把「${tag}」改成：`, tag);
          if (!value || value.trim() === tag) return;
          libraryAction({ action: 'rename', name: tag, new_name: value.trim() },
            `把「${tag}」改名为「${value.trim()}」？所有用到它的视频会一起改名。`);
        });
        const merge = document.createElement('button');
        merge.type = 'button'; merge.className = 'quiet'; merge.textContent = '合并';
        merge.addEventListener('click', () => {
          const value = prompt(`把「${tag}」合并到哪个标签？（输入已存在的标签名）`, '');
          if (!value || !value.trim() || value.trim() === tag) return;
          if (!activeLibrary().includes(value.trim())) { toast(`标签不存在：${value.trim()}`); return; }
          libraryAction({ action: 'merge', source: tag, target: value.trim() },
            `把「${tag}」合并到「${value.trim()}」？「${tag}」将从标签库消失。`);
        });
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'quiet'; remove.textContent = '删除';
        remove.addEventListener('click', () => {
          libraryAction({ action: 'delete', name: tag },
            `删除标签「${tag}」？它会被从所有视频上抹掉。`);
        });
        actions.append(rename, merge, remove);
        row.append(name, select, actions);
        root.append(row);
      }
    }
    if (!root.childElementCount) root.append(emptyNode('标签库是空的：先在主机导入清单，或在上方新建标签。'));
  }

  function openDrawer() {
    state.drawerOpen = true;
    if (state.versionOpen) closeVersions();
    renderCategoryOptions();
    $('tag-drawer').classList.remove('hidden');
    renderLibrary();
  }

  function closeDrawer() {
    state.drawerOpen = false;
    $('tag-drawer').classList.add('hidden');
  }

  /* ---------------------------------------------------------------- 版本历史 */
  const VERSION_ACTION = {
    set_tags: '改标签', deprecate: '弃用/恢复', library_create: '新建标签',
    library_rename: '标签改名', library_set_category: '改标签分类',
    library_merge: '合并标签', library_delete: '删除标签',
    import_xlsx: '导入清单', project_sync: '桌面端同步',
    restore_version: '回滚版本', manual: '手动版本', before_restore: '回滚前备份',
  };

  function fmtDateTime(seconds) {
    if (!seconds) return '—';
    const date = new Date(Number(seconds) * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    const pad = (value) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
      + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  async function loadVersions() {
    const root = $('version-list');
    root.replaceChildren(emptyNode('正在加载…'));
    try {
      const data = await api('/api/tags/versions?limit=100');
      state.versions = data.versions || [];
      renderVersions();
    } catch (err) {
      root.replaceChildren(emptyNode(err.message));
      toast(err.message);
    }
  }

  function renderVersions() {
    const root = $('version-list');
    root.replaceChildren();
    if (!state.versions.length) {
      root.append(emptyNode('还没有版本。改一次标签，或点上方「保存当前为版本」。'));
      return;
    }
    for (const version of state.versions) {
      const row = document.createElement('div');
      row.className = 'version-row' + (version.kind === 'manual' ? ' is-pinned' : '');

      const head = document.createElement('div');
      head.className = 'version-head';
      const badge = document.createElement('span');
      badge.className = 'version-badge' + (version.kind === 'manual' ? ' is-manual' : '');
      badge.textContent = version.kind === 'manual' ? '手动' : '自动';
      const time = document.createElement('span');
      time.className = 'version-time';
      time.textContent = fmtDateTime(version.at);
      const count = document.createElement('span');
      count.className = 'version-meta';
      count.textContent = `r${version.revision} · ${version.count} 条`;
      head.append(badge, time, count);

      const meta = document.createElement('div');
      meta.className = 'version-meta';
      const action = VERSION_ACTION[version.action] || version.action || '变更';
      meta.textContent = `${action} · ${version.user || '未知'}`
        + (version.note ? ` · ${version.note}` : '');
      meta.title = meta.textContent;

      const restoreBtn = document.createElement('button');
      restoreBtn.type = 'button';
      restoreBtn.className = 'quiet btn-sm';
      restoreBtn.textContent = '回滚到此版本';
      restoreBtn.addEventListener('click', () => restoreVersion(version));

      row.append(head, meta, restoreBtn);
      root.append(row);
    }
  }

  async function restoreVersion(version) {
    const stamp = fmtDateTime(version.at);
    const ok = confirm(
      `把视频库与标签回滚到 ${stamp}（r${version.revision}，${version.count} 条）？\n\n`
      + '当前状态会先自动备份成「回滚前备份」版本，随时可以再撤回。');
    if (!ok) return;
    try {
      const data = await api('/api/tags/versions/restore', {
        method: 'POST',
        body: JSON.stringify({ name: version.name, revision: state.revision }),
      });
      toast(`已回滚到 ${stamp}：${data.restored} 条${data.backup ? '，已备份当前状态' : ''}`);
      closeVersions();
      state.loaded = false;
      state.revision = 0;
      await load();
      if (app().reloadTemplates) Promise.resolve(app().reloadTemplates()).catch(() => {});
    } catch (err) {
      if (err.status === 409) {
        toast('有人刚改过，已刷新到最新，请再点一次回滚');
        state.revision = Number(err.data?.revision || 0);
        state.loaded = false;
        await load();
      } else {
        toast(err.message);
      }
    }
  }

  async function createVersion(event) {
    event.preventDefault();
    const note = $('version-note').value.trim();
    try {
      await api('/api/tags/versions', { method: 'POST', body: JSON.stringify({ note }) });
      $('version-note').value = '';
      toast('已保存当前版本');
      await loadVersions();
    } catch (err) {
      toast(err.message);
    }
  }

  function openVersions() {
    state.versionOpen = true;
    if (state.drawerOpen) closeDrawer();
    $('version-drawer').classList.remove('hidden');
    loadVersions();
  }

  function closeVersions() {
    state.versionOpen = false;
    $('version-drawer').classList.add('hidden');
  }

  /* ---------------------------------------------------------------- 上传新视频 */
  const UPLOAD_MAX_MB = 50;
  const UPLOAD_EXT = ['.mp4', '.mov', '.m4v'];
  state.upload = { items: [], tags: new Set(), busy: false };

  function openUpload() {
    if (state.upload.busy) { toast('正在上传，请稍候'); return; }
    state.upload.items = [];
    state.upload.tags = new Set();
    $('upload-error').classList.add('hidden');
    $('upload-files').value = '';
    renderUploadTags();
    renderUploadList();
    $('upload-modal').classList.remove('hidden');
  }

  function closeUpload() {
    if (state.upload.busy) { toast('正在上传，完成后会自动关闭'); return; }
    $('upload-modal').classList.add('hidden');
  }

  function renderUploadTags() {
    const root = $('upload-tag-groups');
    root.replaceChildren();
    const groups = tagsByCategory();
    for (const category of state.categories) {
      const tags = groups[category] || [];
      if (!tags.length) continue;
      const group = document.createElement('div');
      group.className = 'tag-editor-group';
      const title = document.createElement('b');
      title.textContent = category;
      group.append(title);
      const options = document.createElement('div');
      options.className = 'tag-editor-options';
      for (const tag of tags) {
        const label = document.createElement('label');
        label.className = 'tag-option' + (state.upload.tags.has(tag) ? ' is-on' : '');
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = state.upload.tags.has(tag);
        input.addEventListener('change', () => {
          if (input.checked) state.upload.tags.add(tag); else state.upload.tags.delete(tag);
          label.classList.toggle('is-on', input.checked);
          renderUploadList();
        });
        const text = document.createElement('span');
        text.textContent = tag;
        label.append(input, text);
        options.append(label);
      }
      group.append(options);
      root.append(group);
    }
    if (!root.childElementCount) {
      root.append(emptyNode('标签库是空的，请先在「标签库管理」里新建标签。'));
    }
  }

  function addUploadFiles(fileList) {
    let skipped = 0;
    for (const file of fileList || []) {
      const suffix = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
      if (!UPLOAD_EXT.includes(suffix)) { skipped += 1; continue; }
      if (file.size > UPLOAD_MAX_MB * 1024 * 1024) { skipped += 1; continue; }
      if (state.upload.items.some(item => item.file.name === file.name && item.file.size === file.size)) continue;
      state.upload.items.push({ file, status: '等待', progress: 0, error: '', renamed: false });
    }
    if (skipped) toast(`已跳过 ${skipped} 个文件（只收 mp4/mov/m4v，单个 ≤ ${UPLOAD_MAX_MB}MB）`);
    renderUploadList();
  }

  function renderUploadList() {
    const root = $('upload-file-list');
    root.replaceChildren();
    for (const item of state.upload.items) {
      const row = document.createElement('div');
      row.className = 'upload-item';
      const name = document.createElement('span');
      name.className = 'upload-item-name';
      name.textContent = item.file.name;
      name.title = `${item.file.name} · ${(item.file.size / 1024 / 1024).toFixed(1)}MB`;
      const bar = document.createElement('div');
      bar.className = 'upload-progress';
      const fill = document.createElement('span');
      fill.style.width = `${item.progress}%`;
      bar.append(fill);
      const status = document.createElement('span');
      status.className = 'upload-status'
        + (item.status === '完成' ? ' is-ok' : '') + (item.status === '失败' ? ' is-bad' : '');
      const savedAs = item.savedAs && item.savedAs !== item.file.name ? item.savedAs : '';
      status.textContent = item.status + (savedAs ? ` → ${savedAs}` : '');
      status.title = [item.error, savedAs ? `已存为 ${savedAs}` : ''].filter(Boolean).join('；');
      row.append(name, bar, status);
      root.append(row);
    }
    const pending = state.upload.items.filter(item => item.status === '等待').length;
    const done = state.upload.items.filter(item => item.status === '完成').length;
    $('upload-summary').textContent = state.upload.items.length
      ? `共 ${state.upload.items.length} 个 · 待传 ${pending} · 完成 ${done}`
      : '还没有选择文件';
    $('upload-start').disabled = state.upload.busy || !pending || !state.upload.tags.size;
    $('upload-start').textContent = state.upload.busy ? '上传中…' : '开始上传';
  }

  function uploadOne(item, tags) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/tags/videos/upload');
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('X-File-Name', encodeURIComponent(item.file.name));
      xhr.setRequestHeader('X-Tags', encodeURIComponent(JSON.stringify(tags)));
      xhr.setRequestHeader('X-Auto-Number', $('upload-auto-number').checked ? '1' : '0');
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        item.progress = Math.min(99, Math.round((event.loaded / event.total) * 100));
        renderUploadList();
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) { data = {}; }
        if (xhr.status >= 200 && xhr.status < 300) {
          item.progress = 100;
          item.status = '完成';
          item.renamed = !!data.renamed;
          item.savedAs = data.row ? data.row.name : '';
          if (data.warning) item.error = data.warning;
        } else {
          item.status = '失败';
          item.error = data.error || `上传失败（${xhr.status}）`;
        }
        renderUploadList();
        resolve();
      };
      xhr.onerror = () => {
        item.status = '失败';
        item.error = '网络中断';
        renderUploadList();
        resolve();
      };
      xhr.send(item.file);
    });
  }

  async function startUpload() {
    if (state.upload.busy) return;
    const tags = [...state.upload.tags];
    if (!tags.length) { toast('请至少选择一个标签'); return; }
    const queue = state.upload.items.filter(item => item.status === '等待');
    if (!queue.length) return;
    state.upload.busy = true;
    $('upload-error').classList.add('hidden');
    renderUploadList();
    let ok = 0;
    let failed = 0;
    for (const item of queue) {
      item.status = '上传中';
      renderUploadList();
      await uploadOne(item, tags);
      if (item.status === '完成') ok += 1; else failed += 1;
    }
    state.upload.busy = false;
    renderUploadList();
    const error = $('upload-error');
    if (failed) {
      error.textContent = `${failed} 个失败：${state.upload.items.filter(i => i.status === '失败')
        .map(i => `${i.file.name}（${i.error || '未知错误'}）`).join('；')}`;
      error.classList.remove('hidden');
    }
    if (ok) {
      toast(`已上传 ${ok} 条，立即可以用于生产`);
      state.loaded = false;
      state.revision = 0;
      await load();
      if (app().reloadTemplates) Promise.resolve(app().reloadTemplates()).catch(() => {});
      if (!failed) closeUpload();
    }
  }

  /* ---------------------------------------------------------------- 事件 */
  let searchTimer = null;
  $('tag-search').addEventListener('input', (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value;
    searchTimer = setTimeout(() => { state.query = value; state.page = 1; renderAll(); }, 150);
  });
  $('tag-status').addEventListener('change', (event) => {
    state.status = event.target.value; state.page = 1; renderAll();
  });
  $('tag-rating-filter').addEventListener('change', (event) => {
    state.ratingFilter = event.target.value; state.page = 1; renderAll();
  });
  $('tag-sort').addEventListener('change', (event) => {
    state.sort = event.target.value; state.page = 1; renderAll();
  });
  $('tag-only-ready').addEventListener('change', (event) => {
    state.onlyReady = event.target.checked; state.page = 1; renderAll();
  });
  $('tag-filter-toggle').addEventListener('click', () => {
    const card = $('tagf-card');
    card.dataset.open = card.dataset.open === '1' ? '0' : '1';
    renderFilters();
  });
  $('tagf-clear').addEventListener('click', () => {
    state.filters = {}; state.page = 1; renderAll();
  });
  $('tag-refresh').addEventListener('click', () => {
    state.loaded = false;
    state.revision = 0;   // 强制取全量，确保拿到别人刚改的
    load().then(() => toast('已刷新'));
  });
  $('tag-page-prev').addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; renderAll(); }
  });
  $('tag-page-next').addEventListener('click', () => {
    const pages = Math.max(1, Math.ceil(state.matched.length / state.pageSize));
    if (state.page < pages) { state.page += 1; renderAll(); }
  });
  $('tag-page-size').addEventListener('change', (event) => {
    state.pageSize = Number(event.target.value) || 50;
    state.page = 1;
    renderAll();
  });
  $('tag-select-page').addEventListener('click', () => {
    pageIds().forEach(id => state.selected.add(id));
    renderAll();
  });
  $('tag-invert').addEventListener('click', () => {
    pageIds().forEach(id => {
      if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    });
    renderAll();
  });
  $('tag-select-all').addEventListener('click', () => {
    state.matched.forEach(video => state.selected.add(video.id));
    renderAll();
  });
  $('tag-clear-sel').addEventListener('click', () => {
    state.selected.clear();
    renderAll();
  });
  $('tag-mode').addEventListener('change', (event) => {
    state.mode = event.target.value;
    renderEditor();
  });
  $('tag-save').addEventListener('click', saveTags);
  $('tag-clear-tags').addEventListener('click', () => {
    state.pending = new Set();
    renderEditor();
  });
  $('tag-deprecate-btn').addEventListener('click', () => setDeprecated(true));
  $('tag-restore-btn').addEventListener('click', () => setDeprecated(false));
  $('tag-bulk-deprecate').addEventListener('click', () => setDeprecated(true));
  $('tag-bulk-restore').addEventListener('click', () => setDeprecated(false));
  $('tag-library-btn').addEventListener('click', openDrawer);
  $('tag-drawer-close').addEventListener('click', closeDrawer);
  $('tag-versions-btn').addEventListener('click', openVersions);
  $('version-close').addEventListener('click', closeVersions);
  $('version-create').addEventListener('submit', createVersion);
  $('tag-upload-btn').addEventListener('click', openUpload);
  $('upload-close').addEventListener('click', closeUpload);
  $('upload-files').addEventListener('change', (event) => addUploadFiles(event.target.files));
  $('upload-start').addEventListener('click', startUpload);
  const uploadDrop = $('upload-drop');
  ['dragenter', 'dragover'].forEach(name => uploadDrop.addEventListener(name, (event) => {
    event.preventDefault();
    uploadDrop.classList.add('is-over');
  }));
  ['dragleave', 'drop'].forEach(name => uploadDrop.addEventListener(name, (event) => {
    event.preventDefault();
    uploadDrop.classList.remove('is-over');
  }));
  uploadDrop.addEventListener('drop', (event) => {
    event.preventDefault();
    addUploadFiles(event.dataTransfer ? event.dataTransfer.files : null);
  });
  $('tag-cat-create').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = $('tag-cat-name').value.trim();
    if (!name) return;
    const data = await libraryAction({ action: 'category_create', name }, `新增大类「${name}」？`);
    if (data) {
      $('tag-cat-name').value = '';
      renderLibrary();
      toast(`已新增大类「${name}」`);
    }
  });
  $('tag-lib-create').addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = $('tag-lib-error');
    error.classList.add('hidden');
    const name = $('tag-lib-name').value.trim();
    if (!name) return;
    const data = await libraryAction({ action: 'create', name, category: $('tag-lib-cat').value },
      `新建标签「${name}」？`);
    if (data) { $('tag-lib-name').value = ''; renderLibrary(); }
    else if (!error.textContent) error.classList.add('hidden');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('upload-modal').classList.contains('hidden')) { closeUpload(); return; }
    if (state.versionOpen) { closeVersions(); return; }
    if (state.drawerOpen) closeDrawer();
  });
  window.addEventListener('beforeunload', () => {
    const player = $('tag-preview-video');
    if (player) player.pause();
  });

  // 库里大部分素材是 H.265，Chrome 只有在系统装了 HEVC 解码器时才能播，
  // 提前把这件事说清楚，免得同事以为是系统坏了
  function canPlayHevc() {
    const probe = document.createElement('video');
    return ['video/mp4; codecs="hvc1.1.6.L93.B0"',
            'video/mp4; codecs="hev1.1.6.L93.B0"']
      .some(type => probe.canPlayType(type) !== '');
  }

  function checkCodec() {
    const box = $('tag-codec-hint');
    if (!box) return;
    if (canPlayHevc()) {
      box.classList.add('hidden');
      return;
    }
    box.textContent = '这个浏览器不支持 H.265（HEVC）解码，库里大部分视频点开会提示无法播放。'
      + '在系统里安装 Microsoft Store 的「HEVC 视频扩展」后重启浏览器即可；也可以先用桌面打标器预览。';
    box.classList.remove('hidden');
  }

  window.TagUI = {
    enter() {
      // 每次进页面都做一次"带 revision 的探测"：没变化服务端只回 60 B，不轮询
      checkCodec();
      load();
    },
  };
})();
