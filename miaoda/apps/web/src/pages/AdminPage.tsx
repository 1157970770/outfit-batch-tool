import { useCallback, useEffect, useState } from 'react';

import { api, mediaUrl, errorText } from '@/api';
import type {
  AdminStatus,
  AdminUserRow,
  AdminUsersResponse,
  ArchiveSearchResponse,
  ArchiveSignResponse,
  TemplateHealth,
  UsageResponse,
  WorkResponse,
} from '@/lib/admin-types';
import { toast, toastError } from '@/ui/toast';

/**
 * 管理端：服务状态 / 模板健康 / 账户用量（含金额与 CSV）/ 归档检索 / 账户增删改。
 *
 * 金额只在这里出现——试跑页、批量页、任务操作一律不显示费用（与 Python 版一致）。
 */
export function AdminPage() {
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [health, setHealth] = useState<TemplateHealth | null>(null);
  const [users, setUsers] = useState<AdminUserRow[]>([]);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [work, setWork] = useState<{ owner: string; data: WorkResponse } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const loadBase = useCallback(async () => {
    try {
      const [nextStatus, nextHealth, nextUsers] = await Promise.all([
        api<AdminStatus>('/api/admin/status'),
        api<TemplateHealth>('/api/admin/template-health'),
        api<AdminUsersResponse>('/api/admin/users'),
      ]);
      setStatus(nextStatus);
      setHealth(nextHealth);
      setUsers(nextUsers.users || []);
      setError('');
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void loadBase();
  }, [loadBase]);

  const loadUsage = async (owner = '') => {
    try {
      const data = await api<UsageResponse>(`/api/admin/usage${owner ? `?owner=${encodeURIComponent(owner)}` : ''}`);
      setUsage(data);
    } catch (err) {
      toastError(errorText(err));
    }
  };

  const loadWork = async (owner: string) => {
    try {
      const data = await api<WorkResponse>(`/api/admin/accounts/${encodeURIComponent(owner)}/work?limit=100`);
      setWork({ owner, data });
    } catch (err) {
      toastError(errorText(err));
    }
  };

  const reloadTemplates = async () => {
    setBusy(true);
    try {
      const result = await api<{ ok: true; count: number; source_ready_templates: number; manifest_warning?: string }>(
        '/api/admin/reload-templates',
        { method: 'POST' },
      );
      toast(`已重载：候选 ${result.count} 条，就绪 ${result.source_ready_templates} 条。`, 'success');
      await loadBase();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin-page">
      <header className="console-head">
        <div>
          <h1>管理</h1>
          <p>服务状态、模板健康、账户用量与权限。金额只在本页出现。</p>
        </div>
        <div className="console-head-actions">
          <button className="quiet btn-sm" onClick={() => void loadBase()} disabled={busy}>刷新</button>
          <button className="quiet btn-sm" onClick={() => void reloadTemplates()} disabled={busy}>重载视频场景</button>
          <button className="quiet btn-sm" onClick={() => void loadUsage()}>查看用量</button>
        </div>
      </header>

      {error && <p className="error-banner">{error}</p>}

      <section className="admin-grid">
        <StatusCard status={status} />
        <HealthCard health={health} />
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>账户用量</h3>
          <div className="admin-inline">
            <input
              placeholder="按账户过滤（可空）"
              onChange={(event) => {
                const value = event.target.value.trim();
                if (!value) void loadUsage();
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void loadUsage((event.target as HTMLInputElement).value.trim());
              }}
            />
            <button className="quiet btn-sm" onClick={() => void loadUsage()}>查询</button>
          </div>
        </div>
        {usage ? <UsageTable usage={usage} onPick={(owner) => void loadWork(owner)} /> : <p className="tag-empty">点右上角「查看用量」加载。</p>}
      </section>

      {work && (
        <section className="panel">
          <div className="panel-head">
            <h3>{work.owner} 的工作明细（{work.data.total} 条）</h3>
            <div className="admin-inline">
              <a className="quiet btn-sm" href={mediaUrl(`/api/admin/accounts/${encodeURIComponent(work.owner)}/work.csv`)}>导出 CSV</a>
              <button className="link-button" onClick={() => setWork(null)}>关闭</button>
            </div>
          </div>
          <WorkTable data={work.data} />
        </section>
      )}

      <ArchivePanel />

      <section className="panel">
        <div className="panel-head">
          <h3>账户与权限</h3>
          <span className="tag-hint">新增账户、改配额、重置密码、停用/启用、删除。</span>
        </div>
        <UserTable users={users} busy={busy} onChanged={loadBase} onError={toastError} />
        <AddUserForm onCreated={loadBase} />
      </section>
    </div>
  );
}

function StatusCard({ status }: { status: AdminStatus | null }) {
  return (
    <section className="panel">
      <div className="panel-head"><h3>服务状态</h3></div>
      {status ? (
        <dl className="admin-kv">
          <div><dt>版本</dt><dd>{status.version}</dd></div>
          <div><dt>可用模板</dt><dd>{status.templates}</dd></div>
          <div><dt>排队 / 在飞</dt><dd>{status.queued} / {status.active}</dd></div>
          <div><dt>大批量 / 快速通道</dt><dd>{status.big_active} / {status.small_active}</dd></div>
          <div><dt>快速通道上限</dt><dd>{status.small_lane.items} 条 · {status.small_lane.max_jobs} 个任务 · 在飞 {status.small_lane.inflight}</dd></div>
          <div><dt>每账户并发</dt><dd>{status.max_active_per_user}</dd></div>
          <div><dt>线程 / 在飞</dt><dd>{status.max_concurrency} / {status.limits?.max_inflight ?? '—'}</dd></div>
          <div><dt>磁盘剩余</dt><dd>{status.free_gb} GB</dd></div>
          <div><dt>账户数</dt><dd>{status.users.length}</dd></div>
        </dl>
      ) : (
        <p className="tag-empty">读取中…</p>
      )}
    </section>
  );
}

function HealthCard({ health }: { health: TemplateHealth | null }) {
  return (
    <section className="panel">
      <div className="panel-head"><h3>模板健康</h3></div>
      {health ? (
        <>
          <dl className="admin-kv">
            <div><dt>清单行数</dt><dd>{health.workbook_rows}</dd></div>
            <div><dt>已打标</dt><dd>{health.tagged_rows}</dd></div>
            <div><dt>云端就绪</dt><dd>{health.source_ready}</dd></div>
            <div><dt>未匹配</dt><dd>{health.unmatched}</dd></div>
            <div><dt>未打标</dt><dd>{health.untagged}</dd></div>
            <div><dt>就绪率</dt><dd>{Math.round(health.ready_ratio * 100)}%</dd></div>
          </dl>
          {health.manifest_warning ? <p className="chip-warn">{health.manifest_warning}</p> : null}
        </>
      ) : (
        <p className="tag-empty">读取中…</p>
      )}
    </section>
  );
}

function UsageTable({ usage, onPick }: { usage: UsageResponse; onPick: (owner: string) => void }) {
  return (
    <div className="admin-table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>账户</th>
            <th>角色</th>
            <th>任务</th>
            <th>条数</th>
            <th>成功</th>
            <th>失败</th>
            <th>超时</th>
            <th>在飞</th>
            <th>计费秒</th>
            <th>预估金额</th>
            <th>成功率</th>
            <th>最近活跃</th>
            <th>明细</th>
          </tr>
        </thead>
        <tbody>
          {usage.accounts.map((row) => (
            <tr key={row.owner}>
              <td>{row.owner}{row.enabled ? '' : '（已停用）'}</td>
              <td>{row.role}</td>
              <td>{row.jobs}</td>
              <td>{row.items}</td>
              <td>{row.success}</td>
              <td>{row.failed}</td>
              <td>{row.timeout}</td>
              <td>{row.unfinished}</td>
              <td>{row.billable_seconds}</td>
              <td>{row.estimated_cost.toFixed(2)}</td>
              <td>{Math.round((row.success_rate || 0) * 100)}%</td>
              <td>{formatTime(row.last_at)}</td>
              <td><button className="link-button" onClick={() => onPick(row.owner)}>查看</button></td>
            </tr>
          ))}
          <tr className="admin-total">
            <td>合计（{usage.totals.accounts} 个账户）</td>
            <td />
            <td>{usage.totals.jobs}</td>
            <td>{usage.totals.items}</td>
            <td>{usage.totals.success}</td>
            <td>{usage.totals.failed}</td>
            <td>{usage.totals.timeout}</td>
            <td>{usage.totals.unfinished}</td>
            <td>{usage.totals.billable_seconds}</td>
            <td>{usage.totals.estimated_cost.toFixed(2)}</td>
            <td colSpan={3} />
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function WorkTable({ data }: { data: WorkResponse }) {
  return (
    <div className="admin-table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>任务</th>
            <th>批次</th>
            <th>模板</th>
            <th>状态</th>
            <th>耗时(秒)</th>
            <th>计费秒</th>
            <th>金额</th>
            <th>画质</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((row) => (
            <tr key={`${row.job_id}-${row.template_id}-${row.created_at}`}>
              <td>{formatTime(row.created_at)}</td>
              <td>{row.job_id}</td>
              <td>{row.batch_ref || row.batch_id || '—'}</td>
              <td>#{row.template_id} {row.template_name}</td>
              <td>{row.status}</td>
              <td>{row.seconds ?? '—'}</td>
              <td>{row.billable_seconds ?? '—'}</td>
              <td>{row.estimated_cost == null ? '—' : row.estimated_cost.toFixed(2)}</td>
              <td>{row.resolution || '—'}</td>
            </tr>
          ))}
          {data.items.length === 0 && <tr><td colSpan={9} className="tag-empty">这个账户还没有产出记录。</td></tr>}
        </tbody>
      </table>
      <p className="tag-hint">合计：计费 {data.total_billable_seconds} 秒 · 约 {data.total_cost.toFixed(2)} 元（成功 {data.success} / 失败 {data.failed} / 未完成 {data.unfinished}）。</p>
    </div>
  );
}

function ArchivePanel() {
  const [style, setStyle] = useState('');
  const [result, setResult] = useState<ArchiveSearchResponse | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [links, setLinks] = useState<ArchiveSignResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const search = async () => {
    if (!style.trim()) {
      toastError('请输入要搜索的款号。');
      return;
    }
    setBusy(true);
    try {
      const data = await api<ArchiveSearchResponse>(`/api/admin/archive-search?style=${encodeURIComponent(style.trim())}`);
      setResult(data);
      setPicked(new Set());
      setLinks(null);
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const sign = async () => {
    if (picked.size === 0) {
      toastError('请先勾选要生成链接的视频。');
      return;
    }
    setBusy(true);
    try {
      const data = await api<ArchiveSignResponse>('/api/admin/archive-sign', {
        method: 'POST',
        body: { keys: [...picked] },
      });
      setLinks(data);
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>归档检索</h3>
        <div className="admin-inline">
          <input
            value={style}
            placeholder="按款号搜索云端归档"
            onChange={(event) => setStyle(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') void search(); }}
          />
          <button className="quiet btn-sm" onClick={() => void search()} disabled={busy}>搜索</button>
          <button className="primary btn-sm" onClick={() => void sign()} disabled={busy || picked.size === 0}>
            生成下载链接（{picked.size}）
          </button>
        </div>
      </div>
      {result ? (
        <>
          <p className="tag-hint">款号「{result.style}」命中 {result.total} 条（来源：{result.source === 'ledger' ? '本机任务账本' : '云端列举'}）。</p>
          <div className="admin-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th><input type="checkbox" onChange={(event) => setPicked(event.target.checked ? new Set(result.entries.map((entry) => entry.key)) : new Set())} /></th>
                  <th>对象 Key</th>
                  <th>任务</th>
                  <th>批次号</th>
                  <th>账户</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {result.entries.map((entry) => (
                  <tr key={entry.key}>
                    <td>
                      <input
                        type="checkbox"
                        checked={picked.has(entry.key)}
                        onChange={() => setPicked((prev) => {
                          const next = new Set(prev);
                          if (next.has(entry.key)) next.delete(entry.key);
                          else next.add(entry.key);
                          return next;
                        })}
                      />
                    </td>
                    <td className="admin-key">{entry.key}</td>
                    <td>{entry.job_id || '—'}</td>
                    <td>{entry.batch_no || entry.batch_ref || '—'}</td>
                    <td>{entry.owner || '—'}</td>
                    <td>{entry.created_at ? formatTime(entry.created_at) : '—'}</td>
                  </tr>
                ))}
                {result.entries.length === 0 && <tr><td colSpan={6} className="tag-empty">没有找到这个款号的归档。</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <p className="tag-empty">按款号搜索，命中后勾选可批量签 7 天下载链接。</p>
      )}
      {links && (
        <div className="admin-links">
          <p className="tag-hint">已生成 {links.links.length} 条链接，{links.failed.length} 条失败。</p>
          <ul>
            {links.links.map((link) => (
              <li key={link.key}>
                <a href={mediaUrl(link.url)} target="_blank" rel="noreferrer">{link.key}</a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function UserTable(props: {
  users: AdminUserRow[];
  busy: boolean;
  onChanged: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const act = async (path: string, body: Record<string, unknown> | null, done: string, method = 'POST') => {
    try {
      await api(path, { method, body: body ?? undefined });
      toast(done, 'success');
      await props.onChanged();
    } catch (err) {
      props.onError(errorText(err));
    }
  };

  return (
    <div className="admin-table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>账户</th>
            <th>角色</th>
            <th>状态</th>
            <th>配额</th>
            <th>已用</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {props.users.map((user) => (
            <tr key={user.username}>
              <td>{user.username}</td>
              <td>{user.role}</td>
              <td>{user.enabled ? '启用' : '已停用'}</td>
              <td>
                <input
                  className="admin-quota"
                  type="number"
                  min={0}
                  defaultValue={user.quota}
                  disabled={props.busy}
                  onBlur={(event) => {
                    const value = Number(event.target.value);
                    if (!Number.isFinite(value) || value === user.quota) return;
                    void act(`/api/admin/users/${encodeURIComponent(user.username)}/quota`, { quota: value }, '配额已更新。');
                  }}
                />
                <span className="tag-hint">{user.quota === 0 ? '（0 = 不限）' : ''}</span>
              </td>
              <td>{user.used}</td>
              <td className="admin-ops">
                <button
                  className="link-button"
                  onClick={() => {
                    const password = window.prompt(`给「${user.username}」设置新密码（至少 6 位）`, '');
                    if (!password) return;
                    void act(`/api/admin/users/${encodeURIComponent(user.username)}/reset`, { password }, '密码已重置。');
                  }}
                >
                  重置密码
                </button>
                <button
                  className="link-button"
                  onClick={() => void act(
                    `/api/admin/users/${encodeURIComponent(user.username)}/${user.enabled ? 'disable' : 'enable'}`,
                    null,
                    user.enabled ? '已停用。' : '已启用。',
                  )}
                >
                  {user.enabled ? '停用' : '启用'}
                </button>
                <button
                  className="link-button"
                  onClick={() => {
                    if (!window.confirm(`删除账户「${user.username}」后不能再登录，历史任务仍保留。确认吗？`)) return;
                    void act(`/api/admin/users/${encodeURIComponent(user.username)}`, null, '账户已删除。', 'DELETE');
                  }}
                >
                  删除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AddUserForm({ onCreated }: { onCreated: () => Promise<void> }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState('user');
  const [quota, setQuota] = useState(0);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await api('/api/admin/users', { method: 'POST', body: { username, password, role, quota } });
      toast(`已新增账户「${username}」。`, 'success');
      setUsername('');
      setPassword('');
      await onCreated();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin-add-user">
      <input value={username} placeholder="用户名" onChange={(event) => setUsername(event.target.value)} />
      <input value={password} type="password" placeholder="初始密码（≥6 位）" onChange={(event) => setPassword(event.target.value)} />
      <select value={role} onChange={(event) => setRole(event.target.value)}>
        <option value="user">普通账户</option>
        <option value="admin">管理员</option>
      </select>
      <input
        className="admin-quota"
        type="number"
        min={0}
        value={quota}
        onChange={(event) => setQuota(Number(event.target.value))}
        title="生成条数上限，0 表示不限"
      />
      <button className="primary btn-sm" disabled={busy || !username.trim() || password.length < 6} onClick={() => void submit()}>
        新增账户
      </button>
    </div>
  );
}

function formatTime(value: number | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
