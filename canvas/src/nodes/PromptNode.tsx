import { Handle, Position, type NodeProps } from '@xyflow/react'
import { IconMessage2 } from '@tabler/icons-react'
import { NodeHead } from '../NodeHead'
import { useCanvasStore } from '../store'
import type { PromptNodeType } from '../types'

export function PromptNode({ id, data, selected }: NodeProps<PromptNodeType>) {
  const update = useCanvasStore((state) => state.updateNodeData)
  const beginGesture = useCanvasStore((state) => state.beginGesture)
  const endGesture = useCanvasStore((state) => state.endGesture)
  const limit = useCanvasStore((state) => state.options?.prompt_limit ?? 4000)

  return (
    <div className={`cv-node cv-node--prompt${selected ? ' is-selected' : ''}`}>
      <NodeHead nodeId={id} icon={<IconMessage2 size={15} stroke={1.8} />} title='提示词' kind='Prompt' />
      <div className='cv-node-body'>
        <textarea
          className='cv-textarea nodrag'
          rows={5}
          value={data.text}
          maxLength={limit}
          placeholder='描述你想生成的画面，例如：白色棉质短袖衬衫，正面站立，自然光，纯色背景'
          onFocus={beginGesture}
          onBlur={endGesture}
          onChange={(event) => update(id, { text: event.target.value })}
        />
      </div>
      <div className='cv-node-foot'>
        <span>{data.text.length} / {limit}</span>
        <span className='cv-muted'>连到「生图」节点</span>
      </div>
      <Handle type='source' position={Position.Right} className='cv-handle' />
    </div>
  )
}
