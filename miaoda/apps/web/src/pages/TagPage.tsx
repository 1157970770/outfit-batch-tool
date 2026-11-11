import { useCallback, useEffect, useMemo, useState } from 'react';

import { api, apiUrl, errorText } from '@/api';
import { TagFilterPanel } from '@/components/TagFilterPanel';
import { useSession } from '@/session';
import type {
  TagLibrary,
  TagRow,
  TagSnapshot,
  TagStatusFilter,
  TagVersion,
  TagVersionsResponse,
  TagWriteResponse,
} from '@/lib/tag-types';
import type { TagFilterValue } from '@/lib/types';
import { toast, toastError } from '@/ui/toast';

const PAGE_SIZES = [50, 100, 200];

const STATUS_TEXT: Record<TagStatusFilter, string> = {
  all: '全部',
  active: '在用',
  deprecated: '已弃用',
  untagged: '未打标',
  notready: '未就绪',
};

/**
 * 标签页（视频库）：打标 / 打分 / 弃用 / 标签库维护 / 版本回滚 / 上传新视频。
 *
 * 同步口径与 Python 版一致：进页面拉一次快照，写成功后用响应里的 updated 行局部替换，
 * 靠 revision 乐观锁；旧 revision 写入一律 409，并自动重拉最新快照。
 */
export function TagPage() {
  const session = useSession();
  const [snapshot, setSnapshot] = useState<TagSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<TagStatusFilter>('all');
  const [ratingMin, setRatingMin] = useState(0);
  const [sort, setSort] = useState<'id' | 'rating'>('id');
  const [onlyReady, setOnlyReady] = useState(false);
  const [filters, setFilters] = useState<TagFilterValue>({ filters: {}, excludes: {} });
  const [filterOpen, setFilterOpen] = useState(true);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [previewId, setPreviewId] = useState('');
  const [editing, setEditing] = useState<TagRow | null>(null);
  const [editTags, setEditTags] = useState<string[]>([]);
  const [drawer, setDrawer] = useState<'library' | 'versions' | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await api<TagSnapshot>('/api/tags/snapshot');
      setSnapshot(data);
      setError(data.enabled ? '' : '标签真源尚未启用，请先在主机执行 python run_web.py --import-tags。');
      setSelected(new Set());
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = snapshot?.videos || [];
  const categories = snapshot?.categories || [];
  const library: TagLibrary = snapshot?.library || { version: 2, tags: [], categories: {}, usage: {} };

  const filtered = useMemo(() => {
    const text = query.trim().toLowerCase();
    const result = rows.filter((row) => {
      if (status === 'active' && row.deprecated) return false;
      if (status === 'deprecated' && !row.deprecated) return false;
      if (status === 'untagged' && row.tags.length > 0) return false;
      if (status === 'notready' && row.ready) return false;
      if (onlyReady && !row.ready) return false;
      if (ratingMin > 0 && row.rating < ratingMin) return false;
      if (text) {
        const hay = `${row.id} ${row.name} ${row.tags.join(' ')}`.toLowerCase();
        if (!hay.includes(text)) return false;
      }
      return matchesTags(row, filters);
    });
    return result.slice().sort((a, b) => {
      if (sort === 'rating' && b.rating !== a.rating) return b.rating - a.rating;
      return numericId(a.id) - numericId(b.id);
    });
  }, [rows, status, onlyReady, ratingMin, query, filters, sort]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(page, totalPages);
  const pageRows = filtered.slice((current - 1) * pageSize, current * pageSize);
  const previewRow = rows.find((row) => row.id === previewId) || null;

  /** 写成功：只替换受影响的行，避免整表重拉导致滚动位置 / 选中状态乱跳。 */
  const applyWrite = useCallback((result: TagWriteResponse) => {
    setSnapshot((prev) => {
      if (!prev) return prev;
      const index = new Map(result.updated.map((row) => [row.id, row]));
      const videos = prev.videos.map((row) => index.get(row.id) || row);
      return {
        ...prev,
        revision: result.revision ?? prev.revision,
        videos,
        stats: result.stats ? { ...prev.stats, ...result.stats } : prev.stats,
        library: result.library ? result.library : prev.library,
      };
    });
    if (result.catalog_error) toast(result.catalog_error, 'info');
  }, []);

  const write = useCallback(
    async (path: string, body: Record<string, unknown>, done: string) => {
      if (!snapshot) return null;
      setBusy(true);
      try {
        const result = await api<TagWriteResponse>(path, {
          method: 'POST',
          body: { ...body, revision: snapshot.revision },
        });
        applyWrite(result);
        toast(done, 'success');
        if (result.warning) toast(result.warning, 'info');
        return result;
      } catch (err) {
        const text = errorText(err);
        toastError(text);
        // 409 说明别人先写了：重拉最新 revision，让用户再点一次即可。
        if (/409|冲突|revision/i.test(text)) await load(true);
        return null;
      } finally {
        setBusy(false);
      }
    },
    [snapshot, applyWrite, load],
  );

  const toggleRow = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const togglePage = (checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const row of pageRows) {
        if (checked) next.add(row.id);
        else next.delete(row.id);
      }
      return next;
    });
  };

  const bulkTag = async (tag: string, mode: 'replace' | 'append') => {
    const ids = [...selected];
    if (!ids.length) return;
    await write('/api/tags/tags', { ids, tags: [tag], mode }, mode === 'append' ? '已追加标签。' : '已覆盖标签。');
  };

  const bulkRating = async (rating: number) => {
    const ids = [...selected];
    if (!ids.length) return;
    await write('/api/tags/rating', { ids, rating }, rating ? `已打 ${rating} 星。` : '已清除评分。');
  };

  const bulkDeprecate = async (deprecated: boolean) => {
    const ids = [...selected];
    if (!ids.length) return;
    if (deprecated) {
      // 弃用前先看影响面：还在未完成批次清单里的素材，生产时会被标成「模板已不可用」。
      try {
        const impact = await api<{ affected?: number; locked_batches?: Array<{ batch_no: string; items: number }> }>(
          '/api/tags/impact',
          { method: 'POST', body: { action: 'deprecate', ids } },
        );
        const batches = impact.locked_batches || [];
        const extra = batches.length
          ? `\n\n还有 ${batches.length} 个未完成批次在等这些素材：${batches
              .map((item) => `${item.batch_no}（${item.items} 条）`)
              .join('、')}，生产时会被标成「模板已不可用」。`
          : '';
        if (!window.confirm(`确定弃用选中的 ${ids.length} 条视频吗？${extra}\n\n弃用是软删除：不删本机视频、不删 COS 对象，随时可以恢复。`)) {
          return;
        }
      } catch (err) {
        toastError(errorText(err));
        return;
      }
    }
    await write(
      '/api/tags/deprecate',
      { ids, deprecated, reason: deprecated ? '标签页批量弃用' : '' },
      deprecated ? '已弃用。' : '已恢复。',
    );
  };

  const saveEditing = async () => {
    if (!editing) return;
    await write('/api/tags/tags', { ids: [editing.id], tags: editTags, mode: 'replace' }, '标签已保存。');
    setEditing(null);
  };

  if (loading && !snapshot) {
    return (
      <div className="tag-page">
        <p className="tag-empty">正在读取标签真源…</p>
      </div>
    );
  }

  return (
    <div className="tag-page">
      <header className="console-head">
        <div>
          <h1>视频库</h1>
          <p>
            标签真源 <code>{snapshot?.path || 'template_tags.json'}</code> · revision {snapshot?.revision ?? 0}
            {snapshot?.updated_by ? ` · 最近由 ${snapshot.updated_by} 修改` : ''}
          </p>
        </div>
        <div className="console-head-actions">
          <button className="quiet btn-sm" onClick={() => void load()} disabled={busy}>刷新</button>
          <button className="quiet btn-sm" onClick={() => setDrawer('versions')}>版本</button>
          <button className="quiet btn-sm" onClick={() => setDrawer('library')}>标签库</button>
          <button className="quiet btn-sm" onClick={() => setUploadOpen(true)}>上传视频</button>
          <button
            className="primary btn-sm"
            onClick={() => { setEditing(previewRow); setEditTags(previewRow?.tags || []); }}
            disabled={!previewRow}
          >
            编辑当前
          </button>
        </div>
      </header>

      {error && <p className="error-banner">{error}</p>}

      <div className="tag-stats">
        <span>共 {rows.length} 条</span>
        <span>就绪 {rows.filter((row) => row.ready).length}</span>
        <span>已弃用 {rows.filter((row) => row.deprecated).length}</span>
        <span>未打标 {rows.filter((row) => row.tags.length === 0).length}</span>
        <span>候选集 {snapshot?.stats?.candidates ?? 0}</span>
        {snapshot?.manifest?.warning ? <span className="chip-warn">清单：{snapshot.manifest.warning}</span> : null}
      </div>

      <section className="tag-toolbar">
        <input
          value={query}
          placeholder="搜索编号 / 文件名 / 标签"
          onChange={(event) => { setQuery(event.target.value); setPage(1); }}
        />
        <select value={status} onChange={(event) => { setStatus(event.target.value as TagStatusFilter); setPage(1); }}>
          {(Object.keys(STATUS_TEXT) as TagStatusFilter[]).map((key) => (
            <option key={key} value={key}>{STATUS_TEXT[key]}</option>
          ))}
        </select>
        <select value={ratingMin} onChange={(event) => { setRatingMin(Number(event.target.value)); setPage(1); }}>
          {[0, 1, 2, 3, 4, 5].map((value) => (
            <option key={value} value={value}>{value ? `${value} 星以上` : '评分不限'}</option>
          ))}
        </select>
        <select value={sort} onChange={(event) => setSort(event.target.value as 'id' | 'rating')}>
          <option value="id">按编号</option>
          <option value="rating">按评分</option>
        </select>
        <label className="tag-check">
          <input type="checkbox" checked={onlyReady} onChange={(event) => setOnlyReady(event.target.checked)} />
          只看就绪
        </label>
        <span className="tag-count">筛选后 {filtered.length} 条</span>
        <button className="quiet btn-sm" onClick={() => setFilterOpen((prev) => !prev)}>
          {filterOpen ? '收起筛选' : '标签筛选'}
        </button>
      </section>

      {filterOpen && (
        <TagFilterPanel
          categories={categories}
          values={tagValues(library)}
          value={filters}
          allowExclude
          onChange={(next) => { setFilters(next); setPage(1); }}
        />
      )}

      {selected.size > 0 && (
        <BulkBar
          count={selected.size}
          tags={library.tags}
          disabled={busy}
          onClear={() => setSelected(new Set())}
          onTag={bulkTag}
          onRating={bulkRating}
          onDeprecate={bulkDeprecate}
        />
      )}

      <div className="tag-body">
        <section className="tag-table-wrap">
          <table className="data-table tag-table">
            <thead>
              <tr>
                <th className="tag-check">
                  <input
                    type="checkbox"
                    checked={pageRows.length > 0 && pageRows.every((row) => selected.has(row.id))}
                    onChange={(event) => togglePage(event.target.checked)}
                  />
                </th>
                <th>编号</th>
                <th>文件名</th>
                <th>标签</th>
                <th>评分</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row) => (
                <tr
                  key={row.id}
                  className={`${previewId === row.id ? 'is-current' : ''} ${row.deprecated ? 'is-deprecated' : ''}`}
                  onClick={() => setPreviewId(row.id)}
                >
                  <td className="tag-check" onClick={(event) => event.stopPropagation()}>
                    <input type="checkbox" checked={selected.has(row.id)} onChange={() => toggleRow(row.id)} />
                  </td>
                  <td className="tag-id">{row.id || '—'}</td>
                  <td className="tag-name" title={row.relative_path}>{row.name || '—'}</td>
                  <td>
                    <div className="tag-chips">
                      {row.tags.length === 0 && <span className="chip-muted">未打标</span>}
                      {row.tags.map((tag) => <span key={tag} className="tag-chip">{tag}</span>)}
                    </div>
                  </td>
                  <td className="tag-rating">{row.rating ? '★'.repeat(row.rating) : '—'}</td>
                  <td><StateChip row={row} /></td>
                  <td className="tag-ops" onClick={(event) => event.stopPropagation()}>
                    <button className="link-button" onClick={() => { setEditing(row); setEditTags(row.tags); }}>编辑</button>
                    <button
                      className="link-button"
                      onClick={() =>
                        void write(
                          '/api/tags/deprecate',
                          { ids: [row.id], deprecated: !row.deprecated, reason: row.deprecated ? '' : '标签页单条弃用' },
                          row.deprecated ? '已恢复。' : '已弃用。',
                        )
                      }
                    >
                      {row.deprecated ? '恢复' : '弃用'}
                    </button>
                  </td>
                </tr>
              ))}
              {pageRows.length === 0 && (
                <tr><td colSpan={7} className="tag-empty">没有符合筛选条件的视频。</td></tr>
              )}
            </tbody>
          </table>

          <div className="tag-pager">
            <button className="quiet btn-sm" disabled={current <= 1} onClick={() => setPage(current - 1)}>上一页</button>
            <span>{current} / {totalPages}</span>
            <button className="quiet btn-sm" disabled={current >= totalPages} onClick={() => setPage(current + 1)}>下一页</button>
            <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}>
              {PAGE_SIZES.map((size) => <option key={size} value={size}>{size} 条/页</option>)}
            </select>
          </div>
        </section>

        <aside className="tag-preview">
          {previewRow ? (
            <>
              <h3>预览 #{previewRow.id}</h3>
              {previewRow.ready ? (
                <video
                  key={previewRow.id}
                  src={apiUrl(`/api/templates/${encodeURIComponent(previewRow.id)}/preview`)}
                  controls
                  playsInline
                  preload="metadata"
                />
              ) : (
                <p className="tag-empty">这条视频还没有云端地址（未就绪），只能看标签。</p>
              )}
              <dl className="tag-meta">
                <div><dt>文件名</dt><dd>{previewRow.name || '—'}</dd></div>
                <div><dt>路径</dt><dd>{previewRow.relative_path || '—'}</dd></div>
                <div><dt>时长</dt><dd>{previewRow.duration ? `${previewRow.duration}s` : '—'}</dd></div>
                <div><dt>大小</dt><dd>{previewRow.size_mb ? `${previewRow.size_mb} MB` : '—'}</dd></div>
                <div><dt>状态</dt><dd>{uploadStateText(previewRow)}</dd></div>
                <div><dt>最近修改</dt><dd>{previewRow.touched_by ? `${previewRow.touched_by} · ${previewRow.touched_at || ''}` : '—'}</dd></div>
              </dl>
              {previewRow.deprecated && (
                <p className="chip-warn">已弃用：{previewRow.deprecated_by || '—'} {previewRow.deprecated_at || ''}</p>
              )}
            </>
          ) : (
            <p className="tag-empty">点左侧任意一行查看预览与标签。</p>
          )}
        </aside>
      </div>

      {editing && (
        <div className="tag-modal-mask" onClick={() => setEditing(null)}>
          <div className="tag-modal" onClick={(event) => event.stopPropagation()}>
            <h3>编辑 #{editing.id} 的标签</h3>
            <p className="tag-hint">保存会整条覆盖这条视频的标签；标签库之外的名字要先在「标签库」里新增。</p>
            <div className="tag-picker">
              {groupTags(library).map(([category, tags]) => (
                <div key={category} className="tag-picker-group">
                  <span className="tag-picker-cat">{category}</span>
                  <div className="tag-chips">
                    {tags.map((tag) => (
                      <button
                        key={tag}
                        type="button"
                        className={`tag-chip ${editTags.includes(tag) ? 'is-on' : ''}`}
                        onClick={() => setEditTags((prev) => (prev.includes(tag) ? prev.filter((item) => item !== tag) : [...prev, tag]))}
                      >
                        {tag}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="tag-modal-actions">
              <button className="quiet" onClick={() => setEditing(null)}>取消</button>
              <button className="primary" onClick={() => void saveEditing()} disabled={busy}>保存</button>
            </div>
          </div>
        </div>
      )}

      {drawer === 'library' && (
        <LibraryDrawer
          library={library}
          busy={busy}
          onClose={() => setDrawer(null)}
          onWrite={async (body, done) => {
            const result = await write('/api/tags/library', body, done);
            if (result) await load(true);
          }}
        />
      )}

      {drawer === 'versions' && (
        <VersionsDrawer
          revision={snapshot?.revision ?? 0}
          busy={busy}
          onClose={() => setDrawer(null)}
          onRestored={async () => { await load(true); }}
        />
      )}

      {uploadOpen && (
        <UploadModal
          library={library}
          onClose={() => setUploadOpen(false)}
          onDone={async (message) => { setUploadOpen(false); toast(message, 'success'); await load(); }}
        />
      )}

      {session.user?.role !== 'admin' && (
        <p className="tag-footnote">普通账号也能改标签：每次写入都记审计，并受 revision 乐观锁保护。</p>
      )}
    </div>
  );
}

function numericId(id: string): number {
  const value = Number(id);
  return Number.isFinite(value) ? value : 0;
}

function tagValues(library: TagLibrary): Record<string, string[]> {
  const grouped: Record<string, string[]> = {};
  for (const tag of library.tags) {
    const category = library.categories?.[tag] || '场景';
    (grouped[category] = grouped[category] || []).push(tag);
  }
  return grouped;
}

function groupTags(library: TagLibrary): Array<[string, string[]]> {
  const grouped = tagValues(library);
  const order = library.category_order?.length ? library.category_order : Object.keys(grouped);
  const keys = new Set<string>([...order, ...Object.keys(grouped)]);
  return [...keys].filter((key) => grouped[key]?.length).map((key) => [key, grouped[key]]);
}

/** 同类任一命中即通过、跨类必须同时满足；排除优先（与建任务页同一套口径）。 */
function matchesTags(row: TagRow, value: TagFilterValue): boolean {
  for (const tags of Object.values(value.excludes || {})) {
    if (tags?.some((tag) => row.tags.includes(tag))) return false;
  }
  for (const tags of Object.values(value.filters || {})) {
    if (!tags?.length) continue;
    if (!tags.some((tag) => row.tags.includes(tag))) return false;
  }
  return true;
}

function uploadStateText(row: TagRow): string {
  if (row.deprecated) return '已弃用';
  if (row.missing) return '文件不存在';
  if (row.changed) return '文件已变化';
  if (row.tags.length === 0) return '未打标';
  return row.ready ? '就绪' : '待上传';
}

function StateChip({ row }: { row: TagRow }) {
  const text = uploadStateText(row);
  const tone = text === '就绪' ? 'ok' : text === '已弃用' ? 'muted' : text === '未打标' ? 'warn' : 'bad';
  return <span className={`chip-${tone}`}>{text}</span>;
}

function BulkBar(props: {
  count: number;
  tags: string[];
  disabled: boolean;
  onClear: () => void;
  onTag: (tag: string, mode: 'replace' | 'append') => Promise<void>;
  onRating: (rating: number) => Promise<void>;
  onDeprecate: (deprecated: boolean) => Promise<void>;
}) {
  const [tag, setTag] = useState('');
  const [mode, setMode] = useState<'append' | 'replace'>('append');
  return (
    <section className="tag-bulk">
      <strong>已选 {props.count} 条</strong>
      <select value={tag} onChange={(event) => setTag(event.target.value)}>
        <option value="">选择要写入的标签…</option>
        {props.tags.map((item) => <option key={item} value={item}>{item}</option>)}
      </select>
      <select value={mode} onChange={(event) => setMode(event.target.value as 'append' | 'replace')}>
        <option value="append">追加</option>
        <option value="replace">覆盖</option>
      </select>
      <button className="primary btn-sm" disabled={props.disabled || !tag} onClick={() => void props.onTag(tag, mode)}>写标签</button>
      <span className="tag-bulk-sep" />
      {[1, 2, 3, 4, 5].map((value) => (
        <button key={value} className="quiet btn-sm" disabled={props.disabled} onClick={() => void props.onRating(value)}>
          {value}★
        </button>
      ))}
      <button className="quiet btn-sm" disabled={props.disabled} onClick={() => void props.onRating(0)}>清评分</button>
      <span className="tag-bulk-sep" />
      <button className="quiet btn-sm" disabled={props.disabled} onClick={() => void props.onDeprecate(true)}>弃用</button>
      <button className="quiet btn-sm" disabled={props.disabled} onClick={() => void props.onDeprecate(false)}>恢复</button>
      <button className="link-button" onClick={props.onClear}>清除选择</button>
    </section>
  );
}

function LibraryDrawer(props: {
  library: TagLibrary;
  busy: boolean;
  onClose: () => void;
  onWrite: (body: Record<string, unknown>, done: string) => Promise<void>;
}) {
  const [newTag, setNewTag] = useState('');
  const [newCategory, setNewCategory] = useState('场景');
  const [newCategoryName, setNewCategoryName] = useState('');
  const grouped = groupTags(props.library);

  return (
    <div className="tag-drawer-mask" onClick={props.onClose}>
      <aside className="tag-drawer" onClick={(event) => event.stopPropagation()}>
        <header>
          <h3>标签库</h3>
          <button className="link-button" onClick={props.onClose}>关闭</button>
        </header>
        <p className="tag-hint">
          共 {props.library.tags.length} 个标签、{Object.keys(props.library.categories || {}).length} 个类别。
          合并 / 删除 / 改名会级联改写视频行，服务端会先要求确认。
        </p>

        <div className="tag-lib-add">
          <input value={newTag} placeholder="新标签名" onChange={(event) => setNewTag(event.target.value)} />
          <select value={newCategory} onChange={(event) => setNewCategory(event.target.value)}>
            {[...new Set([...grouped.map(([key]) => key), '场景'])].map((key) => (
              <option key={key} value={key}>{key}</option>
            ))}
          </select>
          <button
            className="primary btn-sm"
            disabled={props.busy || !newTag.trim()}
            onClick={async () => {
              const name = newTag.trim();
              await props.onWrite({ action: 'create', name, category: newCategory }, `已新增标签「${name}」。`);
              setNewTag('');
            }}
          >
            新增
          </button>
        </div>

        <div className="tag-lib-list">
          {grouped.map(([category, tags]) => (
            <section key={category}>
              <h4>{category}（{tags.length}）</h4>
              <ul>
                {tags.map((tag) => (
                  <li key={tag}>
                    <span className="tag-chip">{tag}</span>
                    <span className="tag-usage">用过 {props.library.usage?.[tag] || 0} 次</span>
                    <button
                      className="link-button"
                      onClick={async () => {
                        const target = window.prompt(`把「${tag}」合并到哪个标签？`, '');
                        if (!target || target === tag) return;
                        await props.onWrite({ action: 'merge', source: tag, target }, `已把「${tag}」合并到「${target}」。`);
                      }}
                    >
                      合并
                    </button>
                    <button
                      className="link-button"
                      onClick={async () => {
                        const next = window.prompt(`把「${tag}」改名为？`, tag);
                        if (!next || next === tag) return;
                        await props.onWrite({ action: 'rename', name: tag, new_name: next }, `已改名「${tag}」→「${next}」。`);
                      }}
                    >
                      改名
                    </button>
                    <button
                      className="link-button"
                      onClick={async () => {
                        if (!window.confirm(`删除标签「${tag}」会从所有视频上摘掉它，确认吗？`)) return;
                        await props.onWrite({ action: 'delete', name: tag }, `已删除标签「${tag}」。`);
                      }}
                    >
                      删除
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>

        <div className="tag-lib-add">
          <input value={newCategoryName} placeholder="新类别名" onChange={(event) => setNewCategoryName(event.target.value)} />
          <button
            className="quiet btn-sm"
            disabled={props.busy || !newCategoryName.trim()}
            onClick={async () => {
              const name = newCategoryName.trim();
              await props.onWrite({ action: 'category_create', name }, `已新增类别「${name}」。`);
              setNewCategoryName('');
            }}
          >
            新增类别
          </button>
        </div>
      </aside>
    </div>
  );
}

function VersionsDrawer(props: {
  revision: number;
  busy: boolean;
  onClose: () => void;
  onRestored: () => Promise<void>;
}) {
  const [versions, setVersions] = useState<TagVersion[]>([]);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [currentRevision, setCurrentRevision] = useState(props.revision);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api<TagVersionsResponse>('/api/tags/versions?limit=100');
      setVersions(data.versions || []);
      setCurrentRevision(data.revision);
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <div className="tag-drawer-mask" onClick={props.onClose}>
      <aside className="tag-drawer" onClick={(event) => event.stopPropagation()}>
        <header>
          <h3>版本快照</h3>
          <button className="link-button" onClick={props.onClose}>关闭</button>
        </header>
        <p className="tag-hint">自动保留 50 份 + 手动 50 份。回滚前服务端会自动再存一份「回滚前备份」。</p>
        <div className="tag-lib-add">
          <input value={note} placeholder="这次快照的备注（可空）" onChange={(event) => setNote(event.target.value)} />
          <button
            className="primary btn-sm"
            disabled={props.busy}
            onClick={async () => {
              try {
                await api('/api/tags/versions', { method: 'POST', body: { note } });
                setNote('');
                toast('已创建版本快照。', 'success');
                await load();
              } catch (err) {
                toastError(errorText(err));
              }
            }}
          >
            立即快照
          </button>
        </div>
        {loading ? (
          <p className="tag-empty">读取中…</p>
        ) : (
          <ul className="tag-version-list">
            {versions.map((version) => (
              <li key={version.name}>
                <div>
                  <strong>{version.name}</strong>
                  <span className="chip-muted">{version.kind === 'auto' ? '自动' : '手动'}</span>
                  {version.note ? <p className="tag-hint">{version.note}</p> : null}
                </div>
                <button
                  className="quiet btn-sm"
                  disabled={props.busy}
                  onClick={async () => {
                    if (!window.confirm(`回滚到「${version.name}」会覆盖当前整份真源（视频库 + 标签），确认吗？`)) return;
                    try {
                      await api('/api/tags/restore', { method: 'POST', body: { name: version.name, revision: currentRevision } });
                      toast('已回滚到所选版本。', 'success');
                      await props.onRestored();
                      await load();
                    } catch (err) {
                      toastError(errorText(err));
                    }
                  }}
                >
                  回滚
                </button>
              </li>
            ))}
            {versions.length === 0 && <li className="tag-empty">还没有版本快照。</li>}
          </ul>
        )}
      </aside>
    </div>
  );
}

function UploadModal(props: {
  library: TagLibrary;
  onClose: () => void;
  onDone: (message: string) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [autoNumber, setAutoNumber] = useState(true);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!file) return;
    if (picked.length === 0) {
      toastError('请至少选择一个标签');
      return;
    }
    setBusy(true);
    try {
      const buffer = await file.arrayBuffer();
      const response = await api<{ ok: true; warning?: string; row?: TagRow }>('/api/tags/videos/upload', {
        method: 'POST',
        raw: buffer,
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-File-Name': encodeURIComponent(file.name),
          'X-Tags': encodeURIComponent(JSON.stringify(picked)),
          'X-Auto-Number': autoNumber ? '1' : '0',
        },
      });
      await props.onDone(response.warning || `已登记「${file.name}」。`);
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="tag-modal-mask" onClick={props.onClose}>
      <div className="tag-modal" onClick={(event) => event.stopPropagation()}>
        <h3>上传新视频</h3>
        <p className="tag-hint">
          一次一条，只支持 mp4 / mov / m4v，单个 ≤50MB。当前阶段只登记清单到标签真源，云端上传在 P4 接入。
        </p>
        <div className="tag-lib-add">
          <input type="file" accept=".mp4,.mov,.m4v" onChange={(event) => setFile(event.target.files?.[0] || null)} />
        </div>
        <label className="tag-check">
          <input type="checkbox" checked={autoNumber} onChange={(event) => setAutoNumber(event.target.checked)} />
          自动编号（沿用真源里的下一个编号）
        </label>
        <div className="tag-picker">
          {groupTags(props.library).map(([category, tags]) => (
            <div key={category} className="tag-picker-group">
              <span className="tag-picker-cat">{category}</span>
              <div className="tag-chips">
                {tags.map((tag) => (
                  <button
                    key={tag}
                    type="button"
                    className={`tag-chip ${picked.includes(tag) ? 'is-on' : ''}`}
                    onClick={() => setPicked((prev) => (prev.includes(tag) ? prev.filter((item) => item !== tag) : [...prev, tag]))}
                  >
                    {tag}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="tag-modal-actions">
          <button className="quiet" onClick={props.onClose}>取消</button>
          <button className="primary" disabled={busy || !file} onClick={() => void submit()}>
            {busy ? '上传中…' : '上传'}
          </button>
        </div>
      </div>
    </div>
  );
}
