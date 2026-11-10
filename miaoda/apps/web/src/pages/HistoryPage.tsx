/**
 * 历史任务页（P2）。
 *
 * 对应原版 `#history-view` + `batch.js` 的历史分组段：
 * - 顶部：范围（我的/全部，管理员才有全部）、账户、分组方式（批次/账户/日期）、通道（全部/快速/大批量）
 * - 左：分组卡片（批次号或账户/日期、成功/失败/未完成计数、最近时间）
 * - 右：该分组下的记录行（轮次、状态、时长、下载），点「对比」打开三栏对比
 *
 * 与原版一致：分组卡片按 `last_at` 倒序，点开懒加载明细，明细按 offset/limit 翻页。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, errorText } from '@/api';
import { useSession, isAdmin } from '@/session';
import { toastError } from '@/ui/toast';
import type {
  HistoryGroupCard,
  HistoryGroupsResponse,
  HistoryItemsResponse,
  HistoryRow,
} from '@/lib/batch-types';

const PAGE_SIZE = 100;

const GROUP_BY_TEXT: Record<string, string> = {
  batch: '按批次',
  owner: '按账户',
  day: '按日期',
};

const LANE_TEXT: Record<string, string> = {
  '': '全部通道',
  small: '快速通道',
  big: '大批量',
};

const STATUS_TEXT: Record<string, string> = {
  success: '成功',
  failed: '失败',
  invalid: '无效',
  timeout: '超时',
  expired: '过期',
  canceled: '已取消',
  unknown: '结果未知',
  skipped: '跳过',
  pending: '待生成',
  producing: '生成中',
};

const JOB_STATUS_TEXT: Record<string, string> = {
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  canceled: '已取消',
  interrupted: '已中断',
};

function fmtTime(ts?: number | null): string {
  if (!ts) return '';
  const date = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

function statusTone(status: string): string {
  if (status === 'success') return 'chip chip-ok';
  if (status === 'failed' || status === 'invalid' || status === 'expired') return 'chip chip-bad';
  if (status === 'pending' || status === 'producing') return 'chip chip-busy';
  return 'chip chip-warn';
}

function groupTitle(group: HistoryGroupCard): string {
  if (group.group_kind === 'batch') return group.batch_no || '未命名批次';
  if (group.group_kind === 'owner') return group.owner || '未知账户';
  return group.day || '未知日期';
}

function groupSubtitle(group: HistoryGroupCard): string {
  const parts: string[] = [];
  if (group.group_kind === 'batch' && group.owner) parts.push(group.owner);
  if (group.is_ungrouped) parts.push('未归批');
  parts.push(`${group.job_count} 次任务`);
  if (group.big_jobs) parts.push(`大批量 ${group.big_jobs}`);
  if (group.small_jobs) parts.push(`快速 ${group.small_jobs}`);
  return parts.join(' · ');
}

interface CompareState {
  row: HistoryRow;
}

export function HistoryPage() {
  const session = useSession();
  const admin = isAdmin(session);

  const [scope, setScope] = useState<'mine' | 'all'>('mine');
  const [owner, setOwner] = useState('');
  const [groupBy, setGroupBy] = useState('batch');
  const [lane, setLane] = useState('');

  const [groups, setGroups] = useState<HistoryGroupCard[]>([]);
  const [emptyBatches, setEmptyBatches] = useState(0);
  const [loadingGroups, setLoadingGroups] = useState(true);
  const [groupsError, setGroupsError] = useState('');

  const [selectedKey, setSelectedKey] = useState('');
  const [detail, setDetail] = useState<HistoryItemsResponse | null>(null);
  const [offset, setOffset] = useState(0);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [compare, setCompare] = useState<CompareState | null>(null);

  useEffect(() => {
    // 普通账户不能看「全部」，切回「我的」
    if (!admin && scope === 'all') setScope('mine');
  }, [admin, scope]);

  const loadGroups = useCallback(async () => {
    setLoadingGroups(true);
    setGroupsError('');
    try {
      const params = new URLSearchParams({ scope, group_by: groupBy });
      if (lane) params.set('lane', lane);
      if (admin && owner.trim()) params.set('owner', owner.trim());
      const data = await api<HistoryGroupsResponse>(`/api/history/groups?${params.toString()}`);
      setGroups(data.groups || []);
      setEmptyBatches(data.empty_batches || 0);
      setSelectedKey((current) => {
        if (current && (data.groups || []).some((group) => group.key === current)) return current;
        return '';
      });
    } catch (err) {
      setGroupsError(errorText(err));
      setGroups([]);
    } finally {
      setLoadingGroups(false);
    }
  }, [scope, groupBy, lane, owner, admin]);

  useEffect(() => {
    void loadGroups();
  }, [loadGroups]);

  const loadDetail = useCallback(
    async (key: string, nextOffset: number) => {
      if (!key) {
        setDetail(null);
        return;
      }
      setLoadingDetail(true);
      try {
        const params = new URLSearchParams({
          offset: String(nextOffset),
          limit: String(PAGE_SIZE),
          scope,
        });
        if (lane) params.set('lane', lane);
        if (admin && owner.trim()) params.set('owner', owner.trim());
        const data = await api<HistoryItemsResponse>(
          `/api/history/groups/${encodeURIComponent(key)}?${params.toString()}`,
        );
        setDetail(data);
        setOffset(nextOffset);
      } catch (err) {
        toastError(err);
        setDetail(null);
      } finally {
        setLoadingDetail(false);
      }
    },
    [scope, lane, owner, admin],
  );

  useEffect(() => {
    if (!selectedKey) {
      setDetail(null);
      setOffset(0);
      return;
    }
    void loadDetail(selectedKey, 0);
  }, [selectedKey, loadDetail]);

  const selectedGroup = useMemo(
    () => groups.find((group) => group.key === selectedKey) || null,
    [groups, selectedKey],
  );

  const total = detail?.total || 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE);

  return (
    <div className="history-page">
      <div className="history-toolbar">
        <div className="segmented" role="group" aria-label="范围">
          <button
            type="button"
            className={scope === 'mine' ? 'is-on' : ''}
            onClick={() => setScope('mine')}
          >
            我的任务
          </button>
          {admin ? (
            <button
              type="button"
              className={scope === 'all' ? 'is-on' : ''}
              onClick={() => setScope('all')}
            >
              全部账户
            </button>
          ) : null}
        </div>

        {admin ? (
          <label className="field field-inline">
            <span>账户</span>
            <input
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
              placeholder="留空=全部"
            />
          </label>
        ) : null}

        <label className="field field-inline">
          <span>分组</span>
          <select value={groupBy} onChange={(event) => setGroupBy(event.target.value)}>
            <option value="batch">按批次</option>
            <option value="owner">按账户</option>
            <option value="day">按日期</option>
          </select>
        </label>

        <label className="field field-inline">
          <span>通道</span>
          <select value={lane} onChange={(event) => setLane(event.target.value)}>
            {Object.entries(LANE_TEXT).map(([value, label]) => (
              <option key={value || 'all'} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>

        <button type="button" className="quiet" onClick={() => void loadGroups()}>
          刷新
        </button>
      </div>

      {groupsError ? <p className="error-banner">{groupsError}</p> : null}

      <div className="history-grid">
        <aside className="panel history-groups">
          <div className="panel-head">
            <h2>{GROUP_BY_TEXT[groupBy] || '分组'}（{groups.length}）</h2>
            {emptyBatches ? <span className="chip chip-muted">空批次 {emptyBatches}</span> : null}
          </div>
          <div className="history-group-body">
            {loadingGroups ? <p className="muted">加载中…</p> : null}
            {!loadingGroups && !groups.length ? (
              <p className="muted">还没有历史记录。</p>
            ) : null}
            {groups.map((group) => (
              <button
                key={group.key}
                type="button"
                className={`history-card${group.key === selectedKey ? ' is-active' : ''}`}
                onClick={() => setSelectedKey(group.key)}
              >
                <div className="history-card-top">
                  <strong>{groupTitle(group)}</strong>
                  <span className="muted">{fmtTime(group.last_at)}</span>
                </div>
                <div className="history-card-meta">{groupSubtitle(group)}</div>
                <div className="history-card-chips">
                  <span className="chip chip-ok">{group.success} 成功</span>
                  {group.failed ? <span className="chip chip-bad">{group.failed} 失败</span> : null}
                  {group.unfinished ? (
                    <span className="chip chip-warn">{group.unfinished} 未完成</span>
                  ) : null}
                  <span className="chip chip-muted">{group.total} 条</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section className="panel history-detail">
          <div className="panel-head">
            <h2>{selectedGroup ? groupTitle(selectedGroup) : '选择左侧分组查看明细'}</h2>
            {detail ? (
              <span className="muted">
                共 {total} 条 · 第 {Math.min(page + 1, pageCount)}/{pageCount} 页
              </span>
            ) : null}
          </div>

          {!selectedKey ? (
            <p className="muted">点左边任意分组，这里会列出每一轮的成片与状态。</p>
          ) : null}

          {detail?.runs?.length ? (
            <div className="history-runs">
              {detail.runs.map((run) => (
                <span key={run.job_id} className={`chip${run.prompt_changed ? ' chip-warn' : ''}`}>
                  第 {run.no} 轮 · {run.count} 条
                  {run.prompt_changed ? ' · 提示词已改' : ''}
                </span>
              ))}
            </div>
          ) : null}

          {loadingDetail ? <p className="muted">加载明细…</p> : null}

          {detail && detail.items.length ? (
            <table className="data-table history-table">
              <thead>
                <tr>
                  <th>定妆图</th>
                  <th>模板</th>
                  <th>轮次</th>
                  <th>状态</th>
                  <th>时长</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {detail.items.map((row) => (
                  <tr key={`${row.job_id}-${row.template_id}`}>
                    <td>
                      {row.image_url ? (
                        <img className="history-thumb" src={row.image_url} alt="定妆图" />
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <div>{row.template_name || row.template_id}</div>
                      <div className="muted">
                        #{row.template_id}
                        {row.batch_no ? ` · ${row.batch_no}` : ''}
                      </div>
                    </td>
                    <td>
                      第 {row.run_no} 轮
                      {row.run_total > 1 ? <span className="muted"> / 共 {row.run_total}</span> : null}
                    </td>
                    <td>
                      <span className={statusTone(row.status)}>
                        {STATUS_TEXT[row.status] || row.status}
                      </span>
                      {row.job_status && row.job_status !== 'succeeded' ? (
                        <div className="muted">
                          任务：{JOB_STATUS_TEXT[row.job_status] || row.job_status}
                        </div>
                      ) : null}
                      {row.archive_warning ? (
                        <div className="muted">归档：{row.archive_warning}</div>
                      ) : null}
                      {row.verify_warn ? <div className="muted">核对：{row.verify_warn}</div> : null}
                    </td>
                    <td>{row.seconds ? `${row.seconds.toFixed(0)}s` : '—'}</td>
                    <td className="history-actions">
                      {row.download_url ? (
                        <a className="link-button" href={row.download_url}>
                          下载
                        </a>
                      ) : null}
                      {row.download_url ? (
                        <button type="button" className="link-button" onClick={() => setCompare({ row })}>
                          对比
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}

          {detail && !detail.items.length && !loadingDetail ? (
            <p className="muted">这个分组暂时没有可展示的条目。</p>
          ) : null}

          {total > PAGE_SIZE ? (
            <div className="history-pager">
              <button
                type="button"
                className="quiet"
                disabled={page <= 0 || loadingDetail}
                onClick={() => void loadDetail(selectedKey, Math.max(0, offset - PAGE_SIZE))}
              >
                上一页
              </button>
              <span className="muted">
                {Math.min(offset + 1, total)}–{Math.min(offset + PAGE_SIZE, total)} / {total}
              </span>
              <button
                type="button"
                className="quiet"
                disabled={offset + PAGE_SIZE >= total || loadingDetail}
                onClick={() => void loadDetail(selectedKey, offset + PAGE_SIZE)}
              >
                下一页
              </button>
            </div>
          ) : null}
        </section>
      </div>

      {compare ? <CompareOverlay row={compare.row} onClose={() => setCompare(null)} /> : null}
    </div>
  );
}

/** 三栏对比：定妆图 / 生成的视频 / 模板视频（与原版 `compare-overlay` 同构）。 */
function CompareOverlay({ row, onClose }: { row: HistoryRow; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return createPortal(
    <div
      className="compare-overlay"
      role="dialog"
      aria-label="对比"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="compare-panel">
        <div className="compare-head">
          <strong>
            #{row.template_id} {row.template_name}
          </strong>
          <button type="button" className="quiet" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="compare-body">
          <figure className="compare-col">
            <figcaption>定妆图</figcaption>
            {row.image_url ? <img src={row.image_url} alt="定妆图" /> : <p className="muted">无</p>}
          </figure>
          <figure className="compare-col">
            <figcaption>生成的视频</figcaption>
            <video src={row.download_url} controls playsInline preload="metadata" />
          </figure>
          <figure className="compare-col">
            <figcaption>模板视频</figcaption>
            <video src={`/api/templates/${encodeURIComponent(row.template_id)}/preview`} controls playsInline preload="metadata" />
          </figure>
        </div>
      </div>
    </div>,
    document.body,
  );
}
