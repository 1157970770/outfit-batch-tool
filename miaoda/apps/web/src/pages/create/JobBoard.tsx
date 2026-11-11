/**
 * 试跑历史 + 任务详情。列表每 3.5 秒刷新（只在有排队/生成中的任务且页面可见时）。
 * 详情里的下载走 `/api/jobs/<id>/download/<tid>`，播放用 `X-Playback` 的 inline 变体。
 */
import { useEffect, useState } from 'react';
import { apiUrl } from '@/api';
import type { JobView } from '../../lib/types';

export const JOB_STATUS_TEXT: Record<string, string> = {
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  canceled: '已取消',
  interrupted: '已中断',
};

export function statusTone(status: string): string {
  if (status === 'succeeded') return 'ok';
  if (status === 'partial') return 'warn';
  if (status === 'failed' || status === 'interrupted') return 'bad';
  if (status === 'canceled') return 'muted';
  return 'busy';
}

const RECORD_STATUS_TEXT: Record<string, string> = {
  pending: '待跑',
  running: '生成中',
  success: '成功',
  failed: '失败',
  invalid: '无效',
  timeout: '超时',
  expired: '过期',
  canceled: '已取消',
  unknown: '结果未知',
  skipped: '跳过',
};

export interface JobBoardProps {
  jobs: JobView[];
  selected: JobView | null;
  onSelect: (id: string) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onRecheck: (id: string) => void;
  busy?: boolean;
}

export function JobBoard({ jobs, selected, onSelect, onCancel, onRetry, onRecheck, busy }: JobBoardProps) {
  const [showLog, setShowLog] = useState(false);
  useEffect(() => setShowLog(false), [selected?.id]);

  const cancellable = selected && (selected.status === 'queued' || selected.status === 'running');
  const retryable = selected && ['partial', 'failed', 'canceled', 'interrupted'].includes(selected.status);
  // 超时/下载失败的条目要「复查」而不是「重试」：云端可能已出片并在计费，重提 = 二次计费。
  const recheckable =
    selected &&
    !cancellable &&
    selected.records.some((record) => record.status === 'timeout' || record.download_failed);

  return (
    <div className="job-board">
      <div className="job-list">
        {!jobs.length && <p className="create-note">还没有试跑任务。左侧选好图片和场景后提交，任务会出现在这里。</p>}
        {jobs.map((job) => (
          <button
            type="button"
            key={job.id}
            className={`job-row${selected?.id === job.id ? ' is-active' : ''}`}
            onClick={() => onSelect(job.id)}
          >
            <span className="job-row-main">
              <b>{job.id}</b>
              <span className={`chip chip-${statusTone(job.status)}`}>{JOB_STATUS_TEXT[job.status] || job.status}</span>
            </span>
            <span className="job-row-meta">
              {job.created_at} · {job.done}/{job.total} 条 · {job.resolution}
              {job.lane === 'small' ? ' · 快速通道' : ''}
            </span>
            {job.message && <span className="job-row-message">{job.message}</span>}
          </button>
        ))}
      </div>

      <div className="job-detail">
        {!selected ? (
          <p className="create-note">点左侧任意一条任务，这里显示逐条结果、下载与日志。</p>
        ) : (
          <>
            <header className="job-detail-head">
              <div>
                <h4>{selected.id}</h4>
                <p className="muted">
                  {selected.created_at} · {selected.total} 条 · {selected.resolution} ·{' '}
                  {selected.duration > 0 ? `${selected.duration} 秒` : '跟随场景'}
                </p>
              </div>
              <span className={`chip chip-${statusTone(selected.status)}`}>
                {JOB_STATUS_TEXT[selected.status] || selected.status}
              </span>
            </header>

            {selected.message && <p className="job-message">{selected.message}</p>}

            {selected.image_urls?.length > 0 && (
              <div className="job-images">
                {selected.image_urls.map((url, index) => (
                  <img key={url} src={apiUrl(url)} alt={`图${index + 1}`} title={`图${index + 1}`} />
                ))}
              </div>
            )}

            <div className="job-actions">
              {cancellable && (
                <button type="button" className="quiet btn-sm" disabled={busy} onClick={() => onCancel(selected.id)}>
                  取消任务
                </button>
              )}
              {retryable && (
                <button type="button" className="quiet btn-sm" disabled={busy} onClick={() => onRetry(selected.id)}>
                  重试失败项
                </button>
              )}
              {recheckable && (
                <button
                  type="button"
                  className="quiet btn-sm"
                  disabled={busy || Boolean(selected.recheck?.running)}
                  onClick={() => onRecheck(selected.id)}
                  title="按原 taskId 向云端查询一次，出片就补下载；不会重新提交，不会二次计费"
                >
                  {selected.recheck?.running ? '复查中…' : '复查超时任务'}
                </button>
              )}
              {selected.download_all && (
                <a className="quiet btn-sm" href={selected.download_all}>
                  下载整包 ZIP
                </a>
              )}
              {selected.report_url && (
                <a className="quiet btn-sm" href={selected.report_url}>
                  下载报告 CSV
                </a>
              )}
              {selected.log_tail && (
                <button type="button" className="link-button" onClick={() => setShowLog((prev) => !prev)}>
                  {showLog ? '收起日志' : '查看日志'}
                </button>
              )}
            </div>

            {selected.recheck?.message && (
              <p className={`recheck-line${selected.recheck.running ? ' is-running' : ''}`}>
                {selected.recheck.message}
              </p>
            )}

            <table className="job-records">
              <thead>
                <tr>
                  <th>模板</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                {selected.records.map((record) => (
                  <tr key={record.id}>
                    <td>#{record.id}</td>
                    <td>
                      <span className={`chip chip-${record.status === 'success' ? 'ok' : record.status === 'running' ? 'busy' : 'bad'}`}>
                        {RECORD_STATUS_TEXT[record.status] || record.status}
                      </span>
                    </td>
                    <td>{record.seconds ? `${Math.round(record.seconds)}s` : '—'}</td>
                    <td>
                      {record.download_url ? (
                        <a href={`${apiUrl(record.download_url)}?play=1`} target="_blank" rel="noreferrer">
                          预览
                        </a>
                      ) : null}
                      {record.error ? <span className="job-record-error">{record.error}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {showLog && selected.log_tail && <pre className="job-log">{selected.log_tail}</pre>}
          </>
        )}
      </div>
    </div>
  );
}
