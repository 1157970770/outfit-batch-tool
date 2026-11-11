/**
 * 大批量生产页（P2）。
 *
 * 与原版 `batch.js` 的四步向导一一对应：
 * 1. 建批次（草稿，可先不绑图）
 * 2. 绑图 + 标签筛选 + 候选清单 → 锁定
 * 3. 概览（锁定后只能增删细节图、改提示词）
 * 4. 生产（5/10/20/50/100 一轮，失败重试、复查补下载、关闭、整批下载）
 *
 * 轮询：只在「有在跑的任务」或「复查进行中」时按 4000ms 拉详情，页面隐藏时暂停。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiUrl, errorText } from '@/api';
import { useSession } from '@/session';
import { toast, toastError } from '@/ui/toast';
import { useVisiblePolling } from '@/lib/useVisiblePolling';
import { useImageSlots } from '@/lib/image-slots';
import { TagFilterPanel } from '@/components/TagFilterPanel';
import { ImageSlotGrid } from '@/pages/create/ImageSlotGrid';
import {
  PRODUCE_CHOICES,
  PRODUCE_MIN,
  type PromptItem,
  type ServiceInfo,
} from '@outfit/shared';
import type {
  BatchDetailResponse,
  BatchItemView,
  BatchListResponse,
  BatchProduceResponse,
  BatchView,
} from '@/lib/batch-types';
import type { TagFilterValue, TemplateItem } from '@/lib/types';

const POLL_MS = 4000;
const RETRY_MAX = 100;

type Step = 1 | 2 | 3 | 4;

interface CandidateRow {
  id: string;
  name: string;
  tags: string[];
  ready: boolean;
  rating: number;
}

interface CandidatesResponse {
  ok: boolean;
  items: CandidateRow[];
  total: number;
  filtered_total: number;
  ready_count: number;
  used_skipped: number;
  exclude_skipped: number;
}

interface BatchPageProps {
  service: ServiceInfo | null;
  prompts: PromptItem[];
  isAdmin: boolean;
}
const STATE_TEXT: Record<string, string> = { draft: '草稿', locked: '已锁定', closed: '已关闭' };
const ITEM_TEXT: Record<string, string> = {
  pending: '待生产',
  producing: '生产中',
  done: '已完成',
  failed: '失败',
};

function fmtTime(ts?: number | null): string {
  if (!ts) return '';
  const date = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

export function BatchPage() {
  const session = useSession();
  const service: Partial<ServiceInfo> = session.service || {};
  const prompts = session.prompts || [];

  const [batches, setBatches] = useState<BatchView[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<BatchDetailResponse | null>(null);
  const [step, setStep] = useState<Step>(1);
  const [busy, setBusy] = useState(false);
  const [formOpen, setFormOpen] = useState(true);

  // ---- 新建批次表单
  const [batchNo, setBatchNo] = useState('');
  const [title, setTitle] = useState('');
  const [promptId, setPromptId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [resolution, setResolution] = useState('720P');
  const [duration, setDuration] = useState(7);
  const [tags, setTags] = useState<Record<string, string>>({
    top: '',
    lining: '',
    pants: '',
    shoes: '',
    scene: '',
    note: '',
  });
  const images = useImageSlots();
  const [promptDraft, setPromptDraft] = useState('');

  // ---- 候选清单
  const [candidates, setCandidates] = useState<CandidateRow[]>([]);
  const [candLoading, setCandLoading] = useState(false);
  const [candInfo, setCandInfo] = useState({ filtered: 0, ready: 0, usedSkipped: 0, excludeSkipped: 0 });
  const [picked, setPicked] = useState<string[]>([]);
  const [tagFilters, setTagFilters] = useState<TagFilterValue>({ filters: {}, excludes: {} });
  const [tagValues, setTagValues] = useState<Record<string, string[]>>({});

  // ---- 生产
  const [produceCount, setProduceCount] = useState(50);
  const [rechecking, setRechecking] = useState(false);

  const batch = detail?.batch || null;
  const items = detail?.items || [];

  const refreshList = useCallback(async () => {
    try {
      const data = await api<BatchListResponse>('/api/batches');
      setBatches(data.batches || []);
    } catch (err) {
      toastError(errorText(err));
    }
  }, []);

  const refreshDetail = useCallback(async (id: string) => {
    if (!id) return;
    try {
      const data = await api<BatchDetailResponse>(`/api/batches/${id}`);
      setDetail(data);
    } catch (err) {
      toastError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  useEffect(() => {
    if (selectedId) void refreshDetail(selectedId);
    else setDetail(null);
  }, [selectedId, refreshDetail]);

  // 提示词库变化时给一个默认值（与原版一致：默认第一条）。
  useEffect(() => {
    if (!promptId && prompts.length) {
      setPromptId(prompts[0].id);
      setPrompt(prompts[0].body || '');
    }
  }, [prompts, promptId]);

  // 批次切换或提示词被保存后，把「改提示词」输入框同步成批次当前值。
  useEffect(() => {
    if (batch) setPromptDraft(batch.prompt);
  }, [batch?.id, batch?.prompt]);

  const hasActive = Boolean(batch?.active_job_id) || rechecking;

  useVisiblePolling(
    () => {
      void refreshList();
      if (selectedId) void refreshDetail(selectedId);
    },
    POLL_MS,
    { enabled: hasActive },
  );

  const filtered = useMemo(() => {
    if (!batch) return [];
    return items;
  }, [batch, items]);

  /** 载入候选：服务端做标签/评分/已用筛选，前端只负责勾选与分页展示。 */
  const loadCandidates = useCallback(
    async (withTagFilters = true) => {
      if (!selectedId) return;
      setCandLoading(true);
      try {
        const data = await api<CandidatesResponse>(`/api/batches/${selectedId}/candidates`, {
          method: 'POST',
          body: {
            tag_filters: withTagFilters ? tagFilters.filters : {},
            tag_excludes: withTagFilters ? tagFilters.excludes : {},
            limit: service?.max_batch_items || 500,
          },
        });
        setCandidates(data.items || []);
        setCandInfo({
          filtered: data.filtered_total || 0,
          ready: data.ready_count || 0,
          usedSkipped: data.used_skipped || 0,
          excludeSkipped: data.exclude_skipped || 0,
        });
        // 勾选集合收敛到新候选集，避免锁定已不在候选里的模板。
        const alive = new Set((data.items || []).map((row) => row.id));
        setPicked((prev) => prev.filter((id) => alive.has(id)));
      } catch (err) {
        toastError(errorText(err));
      } finally {
        setCandLoading(false);
      }
    },
    [selectedId, tagFilters, service],
  );

  // 标签值字典：从 bootstrap 带回的 service 里拿不到，这里用候选的一次性拉取兜底。
  useEffect(() => {
    if (!selectedId || Object.keys(tagValues).length) return;
    void (async () => {
      try {
        const data = await api<{ tag_values: Record<string, string[]> }>('/api/templates');
        setTagValues(data.tag_values || {});
      } catch {
        /* 标签字典拉不到不影响主流程 */
      }
    })();
  }, [selectedId, tagValues]);

  const uploadIds = async (): Promise<string[]> => {
    const ready = images.ids();
    const pending = images.slots.filter((slot) => slot.file && !slot.uploadId).length;
    if (!pending) return ready;
    const result = await images.uploadAll();
    if (!result.ok) {
      toast(`${result.failures.length} 张图片上传失败，请检查后重试。`, 'error');
    }
    return result.uploadIds;
  };

  const createBatch = async () => {
    // 与 Python `batches.create()` 同口径的前端预检，省一次往返
    const no = batchNo.trim();
    if (no.length < 1 || no.length > 40) {
      toast('批次号需要 1–40 个字符。', 'error');
      return;
    }
    if (/[\r\n\t]/.test(no)) {
      toast('批次号不能包含换行或制表符。', 'error');
      return;
    }
    if (title.trim().length > 60) {
      toast('备注最多 60 个字。', 'error');
      return;
    }
    if (!tags.top.trim()) {
      toast('请填写「上衣」款号（云端归档路径按它命名）', 'error');
      return;
    }
    setBusy(true);
    try {
      const ids = await uploadIds();
      const data = await api<{ batch: BatchView }>('/api/batches', {
        method: 'POST',
        body: {
          batch_no: batchNo,
          title,
          prompt_id: promptId,
          prompt,
          resolution,
          duration,
          tags,
          upload_ids: ids,
        },
      });
      toast('批次已创建，接着挑场景。', 'success');
      setSelectedId(data.batch.id);
      setFormOpen(false);
      setStep(2);
      images.reset();
      await refreshList();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const lockBatch = async () => {
    if (!selectedId) return;
    if (!picked.length) {
      toast('请先勾选要生产的视频场景。', 'error');
      return;
    }
    setBusy(true);
    try {
      await api(`/api/batches/${selectedId}/lock`, {
        method: 'POST',
        body: {
          template_ids: picked,
          confirmed: true,
          tag_filters: tagFilters.filters,
          tag_excludes: tagFilters.excludes,
        },
      });
      toast(`清单已锁定（${picked.length} 条）。`, 'success');
      setStep(3);
      await refreshDetail(selectedId);
      await refreshList();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const savePrompt = async () => {
    if (!selectedId) return;
    if (!promptDraft.trim()) {
      toast('提示词不能为空。', 'error');
      return;
    }
    setBusy(true);
    try {
      await api(`/api/batches/${selectedId}/prompt`, {
        method: 'POST',
        body: { prompt: promptDraft.trim() },
      });
      toast('提示词已保存，下一轮生产生效。', 'success');
      await refreshDetail(selectedId);
      await refreshList();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const produce = async () => {
    if (!selectedId) return;
    setBusy(true);
    try {
      const data = await api<BatchProduceResponse>(`/api/batches/${selectedId}/produce`, {
        method: 'POST',
        body: { count: produceCount },
      });
      const skipped = data.skipped_download_failed || 0;
      toast(
        skipped
          ? `已开始生产 ${data.taken || produceCount} 条；跳过 ${skipped} 条「下载失败待复查」的条目。`
          : `已开始生产 ${data.taken || produceCount} 条。`,
        'success',
      );
      setStep(4);
      await refreshDetail(selectedId);
      await refreshList();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const retryFailed = async () => {
    if (!selectedId) return;
    setBusy(true);
    try {
      await api(`/api/batches/${selectedId}/retry-failed`, {
        method: 'POST',
        body: { count: RETRY_MAX },
      });
      toast('已重新排队失败条目。', 'success');
      await refreshDetail(selectedId);
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const recheckBatch = async () => {
    if (!selectedId) return;
    setBusy(true);
    setRechecking(true);
    try {
      await api(`/api/batches/${selectedId}/recheck`, { method: 'POST' });
      toast('已开始向云端复查超时条目，完成后条目会自动转「已完成」。', 'info');
      await refreshDetail(selectedId);
    } catch (err) {
      setRechecking(false);
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const closeBatch = async () => {
    if (!selectedId) return;
    setBusy(true);
    try {
      await api(`/api/batches/${selectedId}/close`, { method: 'POST' });
      toast('批次已关闭。', 'success');
      await refreshDetail(selectedId);
      await refreshList();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const deleteBatch = async () => {
    if (!selectedId) return;
    setBusy(true);
    try {
      await api(`/api/batches/${selectedId}`, { method: 'DELETE' });
      toast('批次记录已删除（成片与任务记录保留）。', 'success');
      setSelectedId('');
      setDetail(null);
      await refreshList();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const downloadZip = async () => {
    if (!selectedId) return;
    try {
      const probe = await api<{ count: number; download_url: string }>(
        `/api/batches/${selectedId}/files.zip?probe=1`,
      );
      if (!probe.count) {
        toast('暂时没有可下载的视频。', 'error');
        return;
      }
      window.location.href = apiUrl(probe.download_url);
    } catch (err) {
      toastError(errorText(err));
    }
  };

  const addDetailImages = async (files: FileList | null) => {
    if (!selectedId || !files || !files.length) return;
    images.appendFiles(Array.from(files));
    // 走统一上传拿到 upload_id，再按 op=add 落到批次。
    try {
      const ids = await images.uploadAll();
      if (!ids.uploadIds.length) return;
      await api(`/api/batches/${selectedId}/images`, {
        method: 'POST',
        body: { op: 'add', upload_ids: ids.uploadIds },
      });
      images.reset();
      toast('细节图已追加，对之后的生产生效。', 'success');
      await refreshDetail(selectedId);
    } catch (err) {
      toastError(errorText(err));
    }
  };

  const removeDetailImage = async (index: number) => {
    if (!selectedId || index < 1) return;
    try {
      await api(`/api/batches/${selectedId}/images`, {
        method: 'POST',
        body: { op: 'remove', index: index + 1 },
      });
      toast('已删除该细节图，后面的图自动前移。', 'success');
      await refreshDetail(selectedId);
    } catch (err) {
      toastError(errorText(err));
    }
  };

  const pendingCount = items.filter((item) => item.state === 'pending').length;
  const failedCount = items.filter((item) => item.state === 'failed').length;
  const counts = PRODUCE_CHOICES.filter((value) => value <= Math.max(pendingCount, PRODUCE_MIN));
  const canProduceAll = pendingCount > 0 && pendingCount < PRODUCE_MIN;

  // 批次只下发 image_urls / image_names（与 Python `public()` 同口径），没有独立的 images 数组。
  const slotFiles = useMemo(() => {
    const urls = batch?.image_urls || [];
    const names = batch?.image_names || [];
    return urls.map((url, index) => ({ name: names[index] || `图${index + 1}`, url }));
  }, [batch]);

  return (
    <div className="batch-page">
      <header className="console-head">
        <div>
          <h1>大批量生产</h1>
          <p>一个批次冻结一套定妆图与提示词，按 5/10/20/50/100 一轮多轮生产。</p>
        </div>
        <div className="console-head-actions">
          <button
            type="button"
            className="quiet"
            disabled={busy}
            onClick={() => {
              setFormOpen(true);
              setStep(1);
            }}
          >
            新建批次
          </button>
        </div>
      </header>

      <div className="batch-grid">
        <section className="panel batch-list">
          <div className="panel-head">
            <h2>我的批次</h2>
            <span className="muted">{batches.length} 个</span>
          </div>
          <div className="batch-list-body">
            {batches.length === 0 && <p className="muted">还没有批次，点「新建批次」开始。</p>}
            {batches.map((row) => (
              <button
                key={row.id}
                type="button"
                className={`batch-row${row.id === selectedId ? ' is-active' : ''}`}
                onClick={() => {
                  setSelectedId(row.id);
                  setFormOpen(false);
                  setStep(row.state === 'draft' ? 2 : row.state === 'locked' ? 3 : 4);
                }}
              >
                <span className="batch-row-top">
                  <b>{row.batch_no}</b>
                  <span className={`chip chip-${row.state === 'closed' ? 'muted' : row.state === 'draft' ? 'warn' : 'ok'}`}>
                    {STATE_TEXT[row.state] || row.state}
                  </span>
                </span>
                <span className="batch-row-meta">
                  {row.total_items} 条 · 完成 {row.done} · 失败 {row.failed} · {fmtTime(row.created_at)}
                </span>
                {row.title && <span className="batch-row-meta">{row.title}</span>}
              </button>
            ))}
          </div>
        </section>

        <section className="batch-main">
          {!batch && formOpen && (
            <div className="panel">
              <div className="panel-head">
                <h2>新建批次</h2>
              </div>
              <BatchForm
                batchNo={batchNo}
                setBatchNo={setBatchNo}
                title={title}
                setTitle={setTitle}
                promptId={promptId}
                setPromptId={(id) => {
                  setPromptId(id);
                  const hit = prompts.find((item) => item.id === id);
                  setPrompt(hit?.body || '');
                }}
                prompt={prompt}
                setPrompt={setPrompt}
                prompts={prompts}
                resolution={resolution}
                setResolution={setResolution}
                duration={duration}
                setDuration={setDuration}
                tags={tags}
                setTags={setTags}
                images={images}
                busy={busy}
                onCreate={createBatch}
              />
            </div>
          )}

          {batch && (
            <>
              <StepBar step={step} state={batch.state} onStep={setStep} />

              {(step === 1 || (step === 2 && batch.state === 'draft')) && (
                <div className="panel">
                  <div className="panel-head">
                    <h2>步骤 2 · 挑场景</h2>
                    <span className="muted">
                      命中 {candInfo.filtered} 条 · 云端就绪 {candInfo.ready} 条 · 已选 {picked.length}
                    </span>
                  </div>
                  <div className="batch-toolbar">
                    <button
                      type="button"
                      className="quiet btn-sm"
                      disabled={candLoading}
                      onClick={() => void loadCandidates()}
                    >
                      {candLoading ? '筛选…' : '按标签筛选'}
                    </button>
                    <button
                      type="button"
                      className="quiet btn-sm"
                      onClick={() => setPicked(candidates.filter((row) => row.ready).map((row) => row.id))}
                    >
                      全选就绪项
                    </button>
                    <button type="button" className="quiet btn-sm" onClick={() => setPicked([])}>
                      清空
                    </button>
                    {batch.state === 'draft' && !batch.image_urls?.length && (
                      <span className="chip chip-warn">还没绑定妆图，锁定前要先绑</span>
                    )}
                  </div>

                  <TagFilterPanel
                    categories={Object.keys(tagValues)}
                    values={tagValues}
                    value={tagFilters}
                    onChange={setTagFilters}
                    allowExclude
                  />

                  <div className="cand-grid">
                    {candidates.map((row) => {
                      const index = picked.indexOf(row.id);
                      return (
                        <button
                          key={row.id}
                          type="button"
                          className={`cand-card${index >= 0 ? ' is-picked' : ''}${row.ready ? '' : ' is-off'}`}
                          onClick={() =>
                            setPicked((prev) =>
                              prev.includes(row.id) ? prev.filter((id) => id !== row.id) : [...prev, row.id],
                            )
                          }
                          title={row.ready ? row.tags.join(' / ') : '这条场景还没上传到云端模板库，不能生成'}
                        >
                          <span className="cand-id">#{row.id}</span>
                          <span className="cand-name">{row.name}</span>
                          <span className="cand-tags">{row.tags.slice(0, 4).join(' · ')}</span>
                          {row.rating > 0 && <span className="cand-rating">{'★'.repeat(row.rating)}</span>}
                          {index >= 0 && <span className="cand-order">{index + 1}</span>}
                          {!row.ready && <span className="cand-off">未就绪</span>}
                        </button>
                      );
                    })}
                  </div>
                  {candidates.length === 0 && (
                    <p className="muted">还没拉候选。先点「按标签筛选」（不选标签=全部可用场景）。</p>
                  )}

                  <div className="batch-actions">
                    <button type="button" className="primary" disabled={busy || !picked.length} onClick={lockBatch}>
                      锁定清单（{picked.length} 条）
                    </button>
                    <span className="muted">锁定后清单不可改，但可以增删细节图。</span>
                  </div>
                </div>
              )}

              {(step === 3 || (step === 2 && batch.state !== 'draft')) && (
                <div className="panel">
                  <div className="panel-head">
                    <h2>步骤 3 · 概览</h2>
                    <span className="muted">{STATE_TEXT[batch.state]}</span>
                  </div>
                  <dl className="batch-facts">
                    <div>
                      <dt>批次号</dt>
                      <dd>{batch.batch_no}</dd>
                    </div>
                    <div>
                      <dt>画质 / 时长</dt>
                      <dd>
                        {batch.resolution} · {batch.duration > 0 ? `${batch.duration} 秒` : '跟随素材'}
                      </dd>
                    </div>
                    <div>
                      <dt>提示词</dt>
                      <dd>{batch.prompt.slice(0, 60)}{batch.prompt.length > 60 ? '…' : ''}</dd>
                    </div>
                    <div>
                      <dt>款号</dt>
                      <dd>{batch.tags?.top || batch.style_no || '未填'}</dd>
                    </div>
                  </dl>

                  <h3 className="batch-sub">定妆图 + 细节图（{slotFiles.length}/10）</h3>
                  <div className="slot-strip">
                    {slotFiles.map((slot, index) => (
                      <figure key={`${slot.name}-${index}`} className="slot-thumb">
                        <img src={slot.url} alt={slot.name} />
                        <figcaption>图{index + 1}</figcaption>
                        {index > 0 && batch.state !== 'closed' && (
                          <button
                            type="button"
                            className="slot-remove"
                            onClick={() => void removeDetailImage(index)}
                            title="删除这张细节图，后面的图自动前移"
                          >
                            ×
                          </button>
                        )}
                      </figure>
                    ))}
                    {batch.state !== 'closed' && slotFiles.length < 10 && (
                      <label className="slot-add">
                        添加细节图
                        <input
                          type="file"
                          accept="image/jpeg,image/png,image/webp,image/bmp"
                          multiple
                          onChange={(event) => {
                            void addDetailImages(event.target.files);
                            event.target.value = '';
                          }}
                        />
                      </label>
                    )}
                  </div>
                  <p className="muted">槽位 1 是定妆图，不可删除；删除中间某张时后面的图自动前移，保证「图N」不错位。</p>

                  {batch.state !== 'closed' && (
                    <>
                      <h3 className="batch-sub">提示词（只影响下一轮生产）</h3>
                      <label className="field">
                        <textarea
                          rows={4}
                          maxLength={4000}
                          value={promptDraft}
                          placeholder="跑完一批发现提示词要调，改这里再开下一轮即可，不用整批重开"
                          onChange={(event) => setPromptDraft(event.target.value)}
                        />
                      </label>
                      <div className="batch-actions">
                        <button
                          type="button"
                          className="quiet btn-sm"
                          disabled={busy || promptDraft.trim() === batch.prompt.trim()}
                          onClick={() => void savePrompt()}
                        >
                          保存提示词
                        </button>
                        <span className="muted">本轮已提交的不受影响；改完下一轮生效。</span>
                      </div>
                    </>
                  )}

                  <div className="batch-actions">
                    <button type="button" className="primary" disabled={busy} onClick={() => setStep(4)}>
                      去生产
                    </button>
                    {batch.state === 'draft' && (
                      <button type="button" className="quiet" onClick={() => void loadCandidates()}>
                        重新挑场景
                      </button>
                    )}
                  </div>
                </div>
              )}

              {(step === 4 || batch.state === 'closed') && (
                <div className="panel">
                  <div className="panel-head">
                    <h2>步骤 4 · 生产</h2>
                    <span className="muted">
                      待生产 {pendingCount} · 生产中 {items.filter((item) => item.state === 'producing').length} ·
                      完成 {batch.done} · 失败 {batch.failed}
                    </span>
                  </div>

                  {batch.state === 'locked' && (
                    <div className="produce-bar">
                      <label className="field">
                        <span>本轮条数</span>
                        <select
                          value={produceCount}
                          onChange={(event) => setProduceCount(Number(event.target.value))}
                          disabled={Boolean(batch.active_job_id)}
                        >
                          {(canProduceAll ? [pendingCount] : counts).map((value) => (
                            <option key={value} value={value}>
                              {value} 条
                            </option>
                          ))}
                        </select>
                      </label>
                      <button
                        type="button"
                        className="primary"
                        disabled={busy || Boolean(batch.active_job_id) || pendingCount === 0}
                        onClick={produce}
                      >
                        {batch.active_job_id ? '本轮生产中…' : `开始生产 ${produceCount} 条`}
                      </button>
                      <button
                        type="button"
                        className="quiet"
                        disabled={busy || Boolean(batch.active_job_id) || failedCount === 0}
                        onClick={retryFailed}
                      >
                        重试失败 {failedCount} 条
                      </button>
                      <button type="button" className="quiet" disabled={busy || rechecking} onClick={recheckBatch}>
                        {rechecking ? '复查中…' : '复查超时 / 补下载'}
                      </button>
                    </div>
                  )}

                  <div className="batch-actions">
                    <button type="button" className="quiet" onClick={downloadZip}>
                      整批下载 ZIP
                    </button>
                    {batch.state !== 'closed' && (
                      <button type="button" className="quiet" disabled={busy} onClick={closeBatch}>
                        关闭批次
                      </button>
                    )}
                    <button type="button" className="quiet btn-sm" disabled={busy} onClick={deleteBatch}>
                      删除批次记录
                    </button>
                  </div>

                  <table className="data-table batch-items">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>场景</th>
                        <th>标签</th>
                        <th>状态</th>
                        <th>结果</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((item, index) => (
                        <tr key={`${item.template_id}-${index}`}>
                          <td>{index + 1}</td>
                          <td>
                            #{item.template_id} {item.name}
                          </td>
                          <td className="muted">{item.tags.slice(0, 3).join(' · ')}</td>
                          <td>
                            <span
                              className={`chip chip-${
                                item.state === 'done'
                                  ? 'ok'
                                  : item.state === 'failed'
                                    ? 'bad'
                                    : item.state === 'producing'
                                      ? 'busy'
                                      : 'muted'
                              }`}
                            >
                              {ITEM_TEXT[item.state] || item.state}
                            </span>
                            {item.download_failed && <span className="chip chip-warn">下载失败待复查</span>}
                          </td>
                          <td>
                            {item.state === 'failed' && item.error ? (
                              <span className="error">{item.error}</span>
                            ) : item.state === 'done' ? (
                              <a href={apiUrl(`/api/jobs/${item.job_id}/download/${item.template_id}`)}>下载</a>
                            ) : (
                              <span className="muted">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {filtered.length === 0 && <p className="muted">这个批次还没有条目。</p>}
                </div>
              )}
            </>
          )}

          {!batch && !formOpen && <p className="muted">从左边选一个批次，或新建批次。</p>}
        </section>
      </div>
    </div>
  );
}

function StepBar({ step, state, onStep }: { step: Step; state: string; onStep: (step: Step) => void }) {
  const steps: Array<{ id: Step; label: string }> = [
    { id: 1, label: '1 建批次' },
    { id: 2, label: '2 挑场景' },
    { id: 3, label: '3 概览' },
    { id: 4, label: '4 生产' },
  ];
  return (
    <div className="step-bar">
      {steps.map((row) => (
        <button
          key={row.id}
          type="button"
          className={`step-pill${step === row.id ? ' is-active' : ''}`}
          onClick={() => {
            if (row.id === 1 && state !== 'draft') return;
            if (row.id === 2 && state !== 'draft') return;
            onStep(row.id);
          }}
        >
          {row.label}
        </button>
      ))}
    </div>
  );
}

interface BatchFormProps {
  batchNo: string;
  setBatchNo: (value: string) => void;
  title: string;
  setTitle: (value: string) => void;
  promptId: string;
  setPromptId: (value: string) => void;
  prompt: string;
  setPrompt: (value: string) => void;
  prompts: PromptItem[];
  resolution: string;
  setResolution: (value: string) => void;
  duration: number;
  setDuration: (value: number) => void;
  tags: Record<string, string>;
  setTags: (value: Record<string, string>) => void;
  images: ReturnType<typeof useImageSlots>;
  busy: boolean;
  onCreate: () => void;
}

const TAG_FIELDS: Array<{ key: string; label: string; required?: boolean }> = [
  { key: 'top', label: '上衣', required: true },
  { key: 'lining', label: '内衬' },
  { key: 'pants', label: '裤子' },
  { key: 'shoes', label: '鞋子' },
  { key: 'scene', label: '场景' },
  { key: 'note', label: '特殊备注' },
];

function BatchForm(props: BatchFormProps) {
  return (
    <div className="batch-form">
      <div className="field-row">
        <label className="field">
          <span>批次号</span>
          <input
            value={props.batchNo}
            maxLength={40}
            onChange={(event) => props.setBatchNo(event.target.value)}
            placeholder="例如 A-20261110-01"
          />
        </label>
        <label className="field">
          <span>备注</span>
          <input
            value={props.title}
            maxLength={60}
            onChange={(event) => props.setTitle(event.target.value)}
            placeholder="选填，最多 60 字"
          />
        </label>
      </div>

      <h3 className="batch-sub">定妆图 + 细节图（可稍后补）</h3>
      <ImageSlotGrid
        slots={props.images.slots}
        busy={props.images.busy}
        onFiles={props.images.appendFiles}
        onRemove={props.images.remove}
      />

      <h3 className="batch-sub">场景参数</h3>
      <div className="field-row">
        <label className="field">
          <span>画质</span>
          <select value={props.resolution} onChange={(event) => props.setResolution(event.target.value)}>
            <option value="480P">480P 标准</option>
            <option value="720P">720P 高清</option>
          </select>
        </label>
        <label className="field">
          <span>时长</span>
          <select value={props.duration} onChange={(event) => props.setDuration(Number(event.target.value))}>
            <option value={7}>7 秒（标配）</option>
            <option value={-1}>跟随素材</option>
            <option value={5}>5 秒</option>
            <option value={10}>10 秒</option>
          </select>
        </label>
      </div>

      <h3 className="batch-sub">提示词</h3>
      <label className="field">
        <span>提示词模板</span>
        <select value={props.promptId} onChange={(event) => props.setPromptId(event.target.value)}>
          {props.prompts.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title || item.id}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>提示词正文</span>
        <textarea
          rows={6}
          maxLength={4000}
          value={props.prompt}
          onChange={(event) => props.setPrompt(event.target.value)}
        />
      </label>

      <h3 className="batch-sub">款号标签</h3>
      <div className="tag-input-grid">
        {TAG_FIELDS.map((row) => (
          <label key={row.key} className="field">
            <span>
              {row.label}
              {row.required ? '（必填）' : ''}
            </span>
            <input
              value={props.tags[row.key] || ''}
              maxLength={row.key === 'note' ? 100 : 40}
              onChange={(event) => props.setTags({ ...props.tags, [row.key]: event.target.value })}
            />
          </label>
        ))}
      </div>

      <div className="batch-actions">
        <button type="button" className="primary" disabled={props.busy || props.images.busy} onClick={props.onCreate}>
          {props.images.busy ? '上传图片中…' : '创建批次'}
        </button>
        <span className="muted">创建后进入「挑场景」，标签筛选后勾选并锁定清单。</span>
      </div>
    </div>
  );
}
