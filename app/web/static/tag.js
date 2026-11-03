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
    page: 1, pageSize: 50,
    selected: new Set(), matched: [],
    previewId: '', mode: 'replace', lastMulti: false, pending: new Set(), drawerOpen: false,
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
    if (data.library) state.library = data.library;
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
    ['', 'ID', '文件名', '标签', '状态', '大小'].forEach((text) => {
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
      if (state.selected.has(video.id)) row.classList.add('is-picked');
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
      nameCell.textContent = video.name || video.relative_path || '(无文件名)';
      nameCell.title = video.relative_path || video.name || '';

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

      const stateCell = document.createElement('span');
      const info = rowState(video);
      stateCell.className = `tag-row-state ${info.cls}`;
      stateCell.textContent = info.text;
      if (video.deprecated && video.deprecated_by) stateCell.title = `${video.deprecated_by} 弃用`;

      const sizeCell = document.createElement('span');
      sizeCell.className = 'tag-row-state';
      sizeCell.textContent = fmtSize(video.size_mb);

      row.append(checkCell, idCell, nameCell, tagsCell, stateCell, sizeCell);
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

    const allDeprecated = rows.every(row => row.deprecated);
    $('tag-deprecate-btn').classList.toggle('hidden', allDeprecated);
    $('tag-restore-btn').classList.toggle('hidden', !allDeprecated);
    updateEditorHint(rows);
  }

  function updateEditorHint(rows) {
    const hint = $('tag-editor-hint');
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

  async function setDeprecated(flag) {
    const targets = editorTargets();
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
    return { create: '新建', rename: '改名', set_category: '改分类', merge: '合并', delete: '删除' }[action] || '更新';
  }

  /* ---------------------------------------------------------------- 标签库抽屉 */
  function renderLibrary() {
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
    const select = $('tag-lib-cat');
    if (!select.childElementCount) {
      for (const category of state.categories.length ? state.categories : ['景别', '适合季节', '场景', '姿势']) {
        const option = document.createElement('option');
        option.value = category; option.textContent = category;
        select.append(option);
      }
    }
    $('tag-drawer').classList.remove('hidden');
    renderLibrary();
  }

  function closeDrawer() {
    state.drawerOpen = false;
    $('tag-drawer').classList.add('hidden');
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
    if (event.key === 'Escape' && state.drawerOpen) closeDrawer();
  });
  window.addEventListener('beforeunload', () => {
    const player = $('tag-preview-video');
    if (player) player.pause();
  });

  window.TagUI = {
    enter() {
      // 每次进页面都做一次"带 revision 的探测"：没变化服务端只回 60 B，不轮询
      load();
    },
  };
})();
