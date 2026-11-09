import type { CanvasNode } from './types'

export type NodeRef = { id: string; type: CanvasNode['type'] }

// 松手落在节点身上时决定连线方向；类型不匹配的返回 null，最终仍由 store.onConnect 校验。
export function connectionForNodes(from: NodeRef, hit: NodeRef | null): { source: string; target: string } | null {
  if (!hit || hit.id === from.id) return null
  if (from.type === 'generator') {
    if (hit.type !== 'prompt' && hit.type !== 'asset') return null
    return { source: hit.id, target: from.id }
  }
  if (from.type !== 'prompt' && from.type !== 'asset') return null
  if (hit.type !== 'generator') return null
  return { source: from.id, target: hit.id }
}
