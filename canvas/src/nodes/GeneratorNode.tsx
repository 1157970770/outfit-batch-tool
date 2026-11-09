import { useMemo, useState } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import {
  IconBookmarkPlus,
  IconDownload,
  IconPlayerPlayFilled,
  IconRefresh,
  IconSparkles,
} from '@tabler/icons-react'
import { errorMessage, uploadFromUrl } from '../api'
import { NodeHead, StatusDot } from '../NodeHead'
import { connectedPromptText, connectedReferences, runGenerator, toast } from '../run'
import { useCanvasStore } from '../store'
import type { GeneratorNodeType, ImageResult } from '../types'

function formatElapsed(ms?: number): string {
  if (!ms) return ''
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}分${seconds % 60}秒`
}

export function GeneratorNode({ id, data, selected }: NodeProps<GeneratorNodeType>) {
  const options = useCanvasStore((state) => state.options)
  const update = useCanvasStore((state) => state.updateNodeData)
  const beginGesture = useCanvasStore((state) => state.beginGesture)
  const endGesture = useCanvasStore((state) => state.endGesture)
  const promptText = useCanvasStore((state) => connectedPromptText(state.nodes, state.edges, id))
  const refs = useCanvasStore((state) => connectedReferences(state.nodes, state.edges, id))
  const active = data.status === '排队中' || data.status === '生成中'
  const [saving, setSaving] = useState<number | null>(null)

  async function saveMaterial(image: ImageResult) {
    if (saving !== null) return
    setSaving(image.index)
    try {
      const upload = await uploadFromUrl(image.url, image.name || `result-${image.index}.png`)
      const added = useCanvasStore.getState().saveLibraryItem({
        uploadId: upload.upload_id,
        name: image.name || upload.name || '生成图',
        url: `/api/uploads/${upload.upload_id}`,
        width: upload.width ?? image.width ?? null,
        height: upload.height ?? image.height ?? null,
      })
      toast(added ? '已存入素材库' : '这张图已经在素材库里了')
    } catch (error) {
      toast(errorMessage(error))
    } finally {
      setSaving(null)
    }
  }

  const model = useMemo(
    () => options?.models.find((item) => item.id === data.model) ?? options?.models[0],
    [options, data.model],
  )
  const percent = data.count > 0 ? Math.min(100, Math.round((data.progress / data.count) * 100)) : 0

  function changeModel(modelId: string) {
    const next = options?.models.find((item) => item.id === modelId)
    if (!next) return
    const quality = next.qualities.some((item) => item.value === data.quality)
      ? data.quality
      : (next.qualities[0]?.value ?? 'auto')
    update(id, { model: modelId, size: next.sizes[0]?.value ?? 'auto', quality })
  }

  return (
    <div className={`cv-node cv-node--generator${selected ? ' is-selected' : ''}`}>
      <NodeHead
        nodeId={id}
        icon={<IconSparkles size={15} stroke={1.8} />}
        title='生图'
        kind='Image'
        status={<StatusDot status={data.status} />}
      />

      <div className='cv-node-body'>
        {promptText ? (
          <div className='cv-connected-prompt' title={promptText}>
            <span className='cv-chip'>Prompt</span>
            <span className='cv-ellipsis'>{promptText}</span>
          </div>
        ) : (
          <textarea
            className='cv-textarea nodrag'
            rows={3}
            value={data.prompt}
            maxLength={options?.prompt_limit ?? 4000}
            placeholder='提示词，或把 Prompt 节点连进来'
            onFocus={beginGesture}
            onBlur={endGesture}
            onChange={(event) => update(id, { prompt: event.target.value })}
          />
        )}

        <div className='cv-refs'>
          {refs.length ? (
            refs.map((ref) => (
              <img key={ref.id} src={ref.url} alt={ref.name} title={ref.name} loading='lazy' />
            ))
          ) : (
            <span className='cv-muted'>把图片节点连进来作为参考图（可不传）</span>
          )}
        </div>

        {options && !options.configured ? (
          <p className='cv-note is-error'>{options.reason || '生图功能未配置。'}</p>
        ) : null}

        <label className='cv-field'>
          <span>模型</span>
          <select
            className='nodrag'
            value={data.model}
            onChange={(event) => changeModel(event.target.value)}
            disabled={!options || active}
          >
            {(options?.models ?? []).map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
          </select>
        </label>

        <div className='cv-field-row'>
          <label className='cv-field'>
            <span>尺寸</span>
            <select
              className='nodrag'
              value={data.size}
              onChange={(event) => update(id, { size: event.target.value })}
              disabled={!model || active}
            >
              {(model?.sizes ?? []).map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className='cv-field'>
            <span>质量</span>
            <select
              className='nodrag'
              value={data.quality}
              onChange={(event) => update(id, { quality: event.target.value })}
              disabled={!model || active}
            >
              {(model?.qualities ?? []).map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
        </div>

        <div className='cv-run-row'>
          <label className='cv-field cv-count'>
            <span>张数</span>
            <select
              className='nodrag'
              value={data.count}
              onChange={(event) => update(id, { count: Number(event.target.value) })}
              disabled={active}
            >
              {Array.from({ length: options?.max_count ?? 1 }, (_, index) => index + 1).map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </label>
          <button
            type='button'
            className='cv-run nodrag'
            disabled={active || !options?.configured}
            onClick={() => void runGenerator(id)}
          >
            {active ? <IconRefresh size={15} className='cv-spin' /> : <IconPlayerPlayFilled size={14} />}
            {active ? '生成中…' : data.status === '生成失败' ? '重试' : '生成'}
          </button>
        </div>

        {data.status !== 'idle' ? (
          <div className={`cv-run-status is-${data.status === '生成失败' ? 'failed' : active ? 'running' : 'done'}`}>
            <div className='cv-run-line'>
              <StatusDot status={data.status} />
              <span>{data.status}</span>
              {active ? <span className='cv-muted'>{percent}%</span> : null}
              {data.elapsedMs ? <span className='cv-muted'>{formatElapsed(data.elapsedMs)}</span> : null}
            </div>
            {active ? <div className='cv-progress'><i style={{ width: `${percent}%` }} /></div> : null}
            {data.error ? <p className='cv-note is-error'>{data.error}</p> : null}
          </div>
        ) : null}

        {data.images.length ? (
          <div className={`cv-results${data.images.length > 1 ? ' is-grid' : ''}`}>
            {data.images.map((image) => (
              <figure key={image.index} className='cv-result nodrag'>
                <button
                  type='button'
                  title='点击查看原图'
                  onClick={() => window.open(image.url, '_blank', 'noopener')}
                >
                  <img src={image.url} alt={image.name || `结果 ${image.index}`} loading='lazy' />
                </button>
                <figcaption>
                  <span>#{image.index}</span>
                  <button
                    type='button'
                    className='cv-result-action'
                    title='存为素材'
                    disabled={saving !== null}
                    onClick={() => void saveMaterial(image)}
                  >
                    {saving === image.index
                      ? <IconRefresh size={13} className='cv-spin' />
                      : <IconBookmarkPlus size={13} />}
                  </button>
                  <a href={image.downloadUrl} download title='下载原图'>
                    <IconDownload size={13} />
                  </a>
                </figcaption>
              </figure>
            ))}
          </div>
        ) : null}
      </div>

      <div className='cv-node-foot'>
        <span className='cv-muted'>{refs.length ? `${refs.length} 张参考图` : '无参考图'}</span>
        <span className='cv-muted'>{model?.price ? `$${model.price}/张` : ''}</span>
      </div>

      <Handle type='target' position={Position.Left} className='cv-handle' />
    </div>
  )
}
