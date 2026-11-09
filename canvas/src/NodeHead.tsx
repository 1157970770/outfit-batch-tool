import type { ReactNode } from 'react'
import { IconX } from '@tabler/icons-react'
import { useCanvasStore } from './store'

export function NodeHead({
  nodeId,
  icon,
  title,
  kind,
  status,
}: {
  nodeId: string
  icon: ReactNode
  title: string
  kind: string
  status?: ReactNode
}) {
  return (
    <div className='cv-node-head'>
      <span className='cv-node-icon'>{icon}</span>
      <span className='cv-node-title'>{title}</span>
      <span className='cv-node-kind'>{kind}</span>
      {status ? <span className='cv-node-head-status'>{status}</span> : null}
      <button
        type='button'
        className='cv-icon-btn cv-node-remove nodrag'
        title='删除节点'
        onClick={() => useCanvasStore.getState().removeNodes([nodeId])}
      >
        <IconX size={14} stroke={1.8} />
      </button>
    </div>
  )
}

export function StatusDot({ status }: { status: string }) {
  const tone =
    status === '已完成'
      ? 'done'
      : status === '部分完成'
        ? 'partial'
        : status === '生成失败'
          ? 'failed'
          : status === '排队中'
            ? 'queued'
            : status === '生成中'
              ? 'running'
              : 'idle'
  return <span className={`cv-dot is-${tone}`} title={status} />
}
