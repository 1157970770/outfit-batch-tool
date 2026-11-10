/**
 * 小批量试跑（≤3 条，走快速通道）。对应原生 `#create-view` + `app.js`。
 *
 * 三步：1) 定妆图 + 细节图 → 2) 场景与参数（标签筛选 / 选择方式 / 数量画质 / 款号标签）→
 * 3) 提示词与提交；另一个页签是本账户的试跑记录。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { api, errorText } from '@/api';
import { toast, toastError } from '@/ui/toast';
import { useSession } from '@/session';
import { useVisiblePolling } from '@/lib/useVisiblePolling';
import { useImageSlots } from '@/lib/image-slots';
import { buildTagIndex, filterTemplates } from '@/lib/tag-filter';
import type {
  JobView,
  JobsResponse,
  ServiceInfo,
  TagFilterValue,
  TemplateItem,
  TemplatesResponse,
} from '@/lib/types';
import { TagFilterPanel } from '@/components/TagFilterPanel';
import { ImageSlotGrid } from './create/ImageSlotGrid';
import { TemplatePicker, PICK_MAX } from './create/TemplatePicker';
import { StyleTags } from './create/StyleTags';
import { PromptEditor } from './create/PromptEditor';
import { JobBoard } from './create/JobBoard';

const CREATE_MAX = 3;
const JOB_POLL_MS = 3500;

const EMPTY_FILTER: TagFilterValue = { filters: {}, excludes: {} };

export function CreatePage() {
  const session = useSession();
  const service: Partial<ServiceInfo> = session.service || {};

  const [tab, setTab] = useState<'make' | 'history'>('make');
  const [templates, setTemplates] = useState<TemplateItem[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [tagValues, setTagValues] = useState<Record<string, string[]>>({});
  const [templatesError, setTemplatesError] = useState('');
  const [loadingTemplates, setLoadingTemplates] = useState(true);

  const [tagFilter, setTagFilter] = useState<TagFilterValue>(EMPTY_FILTER);
  const [mode, setMode] = useState<'random' | 'selected'>('random');
  const [picked, setPicked] = useState<string[]>([]);
  const [count, setCount] = useState(1);
  const [resolution, setResolution] = useState('480P');
  const [duration, setDuration] = useState(5);
  const [styleTags, setStyleTags] = useState<Record<string, string>>({ top: '' });

  const [promptId, setPromptId] = useState('');
  const [promptBody, setPromptBody] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const [jobs, setJobs] = useState<JobView[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [selected, setSelected] = useState<JobView | null>(null);
  const [jobBusy, setJobBusy] = useState(false);

  const images = useImageSlots();

  // 与原生一致：拿一次全量候选集，标签筛选在浏览器里做（服务端 /api/templates 不接筛选参数）。
  const loadTemplates = useCallback(async () => {
    setLoadingTemplates(true);
    try {
      const data = await api<TemplatesResponse>('/api/templates');
      setTemplates((data.items || []).filter((item) => item.ready));
      setCategories(data.categories || []);
      setTagValues(data.tag_values || {});
      setTemplatesError('');
    } catch (err) {
      setTemplatesError(errorText(err));
    } finally {
      setLoadingTemplates(false);
    }
  }, []);

  useEffect(() => {
    void loadTemplates();
  }, [loadTemplates]);

  const tagIndex = useMemo(() => buildTagIndex(tagValues), [tagValues]);
  const filtered = useMemo(
    () => filterTemplates(templates, tagFilter.filters, tagFilter.excludes, tagIndex),
    [templates, tagFilter, tagIndex],
  );

  // 筛选变窄后，已选场景若掉出候选集要清掉，否则提交会拿到「不在当前筛选结果里」。
  useEffect(() => {
    const valid = new Set(filtered.map((item) => item.id));
    setPicked((prev) => {
      const kept = prev.filter((id) => valid.has(id));
      return kept.length === prev.length ? prev : kept;
    });
  }, [filtered]);

  // 提示词默认取第一条（P01「服装定妆」服务端已过滤）。
  useEffect(() => {
    if (promptId || !session.prompts.length) return;
    const first = session.prompts[0];
    setPromptId(first.id);
    setPromptBody(first.body);
  }, [promptId, session.prompts]);

  const refreshJobs = useCallback(async () => {
    try {
      const data = await api<JobsResponse>('/api/jobs');
      setJobs(data.jobs || []);
      return data.jobs || [];
    } catch {
      return [];
    }
  }, []);

  const refreshSelected = useCallback(async (id: string) => {
    if (!id) return;
    try {
      const data = await api<{ job: JobView }>(`/api/jobs/${id}`);
      setSelected(data.job);
    } catch {
      // 任务可能刚被清理；列表下一次刷新会把它带走
    }
  }, []);

  useEffect(() => {
    void refreshJobs().then((list) => {
      if (!selectedId && list.length) setSelectedId(list[0].id);
    });
    // 只在挂载时挑一次默认选中项
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshJobs]);

  useEffect(() => {
    void refreshSelected(selectedId);
  }, [selectedId, refreshSelected]);

  // 复查进行中时 job.status 已经是终态，但进度还在变，必须继续轮询。
  const hasActive = useMemo(
    () =>
      jobs.some(
        (job) => job.status === 'queued' || job.status === 'running' || Boolean(job.recheck?.running),
      ),
    [jobs],
  );

  useVisiblePolling(
    () => {
      void refreshJobs();
      if (selectedId) void refreshSelected(selectedId);
    },
    JOB_POLL_MS,
    { enabled: hasActive },
  );

  const availableCount = filtered.length ? Math.min(CREATE_MAX, filtered.length) : CREATE_MAX;
  useEffect(() => {
    if (count > availableCount) setCount(availableCount);
  }, [count, availableCount]);

  const submit = async () => {
    setError('');
    if (!images.slots.length) {
      setError('请先上传服装图片。');
      return;
    }
    if (!(styleTags.top || '').trim()) {
      setError('请填写「上衣」款号（云端归档路径按它命名）。');
      return;
    }
    if (mode === 'selected' && !picked.length) {
      setError('请先在「视频场景」里点选 1–3 个场景。');
      return;
    }
    setSubmitting(true);
    try {
      const uploaded = await images.uploadAll();
      if (!uploaded.ok) {
        setError(
          uploaded.failures.length === 1
            ? `第 ${uploaded.failures[0].index} 张上传失败：${uploaded.failures[0].message}`
            : `${uploaded.failures.length} 张图片上传失败：${uploaded.failures.map((f) => `图${f.index}`).join('、')}`,
        );
        return;
      }
      const ids = uploaded.uploadIds;
      if (!ids.length) {
        setError('图片还没有上传完成，请重试。');
        return;
      }
      const data = await api<{ job: JobView }>('/api/jobs', {
        method: 'POST',
        body: {
          upload_ids: ids,
          upload_id: ids[0],
          selection: mode,
          count,
          template_id: mode === 'selected' ? picked.join(',') : '',
          prompt_id: promptId,
          prompt: promptBody,
          resolution,
          duration,
          tag_filters: tagFilter.filters,
          tags: styleTags,
        },
      });
      toast('任务已提交，排队中…', 'success');
      setSelectedId(data.job.id);
      setSelected(data.job);
      setTab('history');
      images.reset();
      void refreshJobs();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  };

  const cancelJob = async (id: string) => {
    setJobBusy(true);
    try {
      await api(`/api/jobs/${id}/cancel`, { method: 'POST' });
      toast('已请求取消。', 'success');
      await refreshJobs();
      await refreshSelected(id);
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setJobBusy(false);
    }
  };

  const retryJob = async (id: string) => {
    setJobBusy(true);
    try {
      const data = await api<{ job: JobView }>(`/api/jobs/${id}/retry`, { method: 'POST' });
      setSelected(data.job);
      toast('已重新排队失败条目。', 'success');
      await refreshJobs();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setJobBusy(false);
    }
  };

  /** 复查超时/下载失败条目：只查云端、不重新提交（重提会二次计费）。 */
  const recheckJob = async (id: string) => {
    setJobBusy(true);
    try {
      const data = await api<{ job: JobView }>(`/api/jobs/${id}/recheck`, { method: 'POST' });
      setSelected(data.job);
      toast('已开始向云端复查，进度会显示在任务详情里。', 'info');
      await refreshJobs();
    } catch (err) {
      toastError(errorText(err));
    } finally {
      setJobBusy(false);
    }
  };

  const summaryDetail = `${resolution === '720P' ? '720P 高清' : '480P 标准'} · ${
    duration > 0 ? `${duration} 秒` : '跟随场景'
  }${mode === 'selected' ? ` · 挑选 ${picked.length || 0} 个场景` : ''}`;

  return (
    <div className="create-page">
      <header className="console-head">
        <div>
          <h1>小批量试跑</h1>
          <p className="muted">一次最多 {CREATE_MAX} 条，走快速通道，不与大批量排队。</p>
        </div>
        <div className="segmented segmented-sm" role="group" aria-label="小批量页面">
          <button type="button" className={tab === 'make' ? 'active' : ''} onClick={() => setTab('make')}>
            制作
          </button>
          <button
            type="button"
            className={tab === 'history' ? 'active' : ''}
            onClick={() => setTab('history')}
            title="在「历史任务 → 小批量」里查看全部试跑记录"
          >
            历史
          </button>
        </div>
      </header>

      {templatesError && <p className="error-banner">{templatesError}</p>}
      {service.templates_error ? <p className="error-banner">{service.templates_error}</p> : null}
      {service.notice ? <p className="notice-banner">{service.notice}</p> : null}

      {tab === 'make' ? (
        <div className="create-grid">
          <article className="panel panel-media">
            <div className="panel-head">
              <span className="step">1</span>
              <div>
                <b>定妆图 + 细节图</b>
                <small>定妆图必填，可再加 9 张细节图（单张 ≤ 20MB）</small>
              </div>
            </div>
            <ImageSlotGrid
              slots={images.slots}
              busy={images.busy}
              onFiles={(files, replaceIndex) => {
                const result = images.appendFiles(files, replaceIndex);
                if (result.errors.length) toastError(result.errors[0]);
              }}
              onRemove={images.remove}
            />
          </article>

          <article className="panel panel-scene">
            <div className="panel-head">
              <span className="step">2</span>
              <div>
                <b>场景与参数</b>
                <small>标签筛选 → 选择方式 → 数量画质</small>
              </div>
            </div>

            <TagFilterPanel
              categories={categories}
              values={tagValues}
              value={tagFilter}
              allowExclude={false}
              onChange={setTagFilter}
            />

            <TemplatePicker
              templates={filtered}
              mode={mode}
              onMode={setMode}
              picked={picked}
              onPicked={(ids) => {
                if (ids.length > PICK_MAX) {
                  toastError(`一次最多挑 ${PICK_MAX} 个视频场景，先取消一个再选。`);
                  return;
                }
                setPicked(ids);
              }}
              count={count}
              maxCount={availableCount}
            />

            <div className="field">
              <span>生成数量（上限 {availableCount} 条）</span>
              <div className="segmented segmented-count" role="group" aria-label="生成数量">
                {Array.from({ length: availableCount }, (_, index) => index + 1).map((value) => (
                  <button
                    type="button"
                    key={value}
                    className={count === value ? 'active' : ''}
                    onClick={() => setCount(value)}
                  >
                    {value} 条
                  </button>
                ))}
              </div>
            </div>

            <div className="field">
              <span>款号标签（上衣必填，云端归档路径按上衣命名）</span>
              <StyleTags value={styleTags} onChange={setStyleTags} />
            </div>

            <div className="field-row">
              <label className="field">
                画质
                <select value={resolution} onChange={(event) => setResolution(event.target.value)}>
                  <option value="480P">480P 标准</option>
                  <option value="720P">720P 高清</option>
                </select>
              </label>
              <label className="field">
                时长
                <select value={duration} onChange={(event) => setDuration(Number(event.target.value))}>
                  <option value={5}>5 秒</option>
                  <option value={-1}>跟随场景</option>
                </select>
              </label>
            </div>
            <p className="panel-note">
              ≤{service.small_lane_items || 3} 条走快速通道：独立在飞名额，不会排在大批量后面。
            </p>
          </article>

          <article className="panel panel-prompt">
            <div className="panel-head">
              <span className="step">3</span>
              <div>
                <b>提示词</b>
                <small>选模板，可按需要微调</small>
              </div>
            </div>
            <PromptEditor
              prompts={session.prompts}
              promptId={promptId}
              body={promptBody}
              onPick={(id) => {
                setPromptId(id);
                const item = session.prompts.find((prompt) => prompt.id === id);
                if (item) setPromptBody(item.body);
              }}
              onBody={setPromptBody}
            />
            <div className="submit-block">
              <div className="submit-summary">
                <strong>准备生成 {mode === 'selected' ? picked.length || 1 : count} 条视频</strong>
                <p className="muted">{summaryDetail}</p>
              </div>
              <button type="button" className="primary" disabled={submitting || loadingTemplates} onClick={submit}>
                {submitting ? '提交中…' : '确认提交'}
              </button>
            </div>
            {error && <p className="error">{error}</p>}
          </article>
        </div>
      ) : (
        <section className="panel create-history">
          <div className="panel-head">
            <span className="step">试</span>
            <div>
              <b>试跑历史</b>
              <small>只列出本账户的任务，点任意一条看详情与下载</small>
            </div>
          </div>
          <JobBoard
            jobs={jobs}
            selected={selected}
            onSelect={setSelectedId}
            onCancel={(id) => void cancelJob(id)}
            onRetry={(id) => void retryJob(id)}
            onRecheck={(id) => void recheckJob(id)}
            busy={jobBusy}
          />
        </section>
      )}
    </div>
  );
}
