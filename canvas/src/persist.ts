import type { CanvasEdge, CanvasNode, CanvasViewport } from './types'

const VERSION = 1

export type StoredProject = {
  version: number
  nodes: CanvasNode[]
  edges: CanvasEdge[]
  viewport: CanvasViewport
}

function key(username: string): string {
  return `outfit.canvas.v1.${username}`
}

export function loadProject(username: string): StoredProject | null {
  try {
    const raw = localStorage.getItem(key(username))
    if (!raw) return null
    const data = JSON.parse(raw) as StoredProject
    if (!data || data.version !== VERSION || !Array.isArray(data.nodes) || !Array.isArray(data.edges)) {
      return null
    }
    return {
      ...data,
      nodes: data.nodes.map((node) =>
        node.type === 'asset' ? ({ ...node, data: { ...node.data } } as CanvasNode) : node,
      ),
      viewport: data.viewport || { x: 0, y: 0, zoom: 1 },
    }
  } catch {
    return null
  }
}

export function saveProject(username: string, project: StoredProject): void {
  try {
    localStorage.setItem(key(username), JSON.stringify(project))
  } catch {
    /* 隐私模式或配额满时忽略，画布仍可继续使用 */
  }
}

// ------------------------------------------------------------ 素材库
// 只存 upload 引用（url 始终是 /api/uploads/<id>），不复制图片本体。

export type LibraryItem = {
  id: string
  uploadId: string
  name: string
  url: string
  width?: number | null
  height?: number | null
  addedAt: number
}

function libraryKey(username: string): string {
  return `outfit.canvas.assets.v1.${username}`
}

export function loadLibrary(username: string): LibraryItem[] {
  try {
    const raw = localStorage.getItem(libraryKey(username))
    if (!raw) return []
    const data = JSON.parse(raw) as { version?: number; items?: LibraryItem[] }
    if (!data || data.version !== 1 || !Array.isArray(data.items)) return []
    return data.items.filter(
      (item) =>
        item && typeof item.id === 'string' && typeof item.uploadId === 'string' &&
        typeof item.url === 'string' && item.url.startsWith('/api/uploads/'),
    )
  } catch {
    return []
  }
}

export function saveLibrary(username: string, items: LibraryItem[]): void {
  try {
    localStorage.setItem(libraryKey(username), JSON.stringify({ version: 1, items }))
  } catch {
    /* 忽略：素材库存不下不影响画布 */
  }
}
