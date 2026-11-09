import { createImageTask, errorMessage, getImageTask } from './api'
import { toGenStatus, useCanvasStore } from './store'
import type { CanvasEdge, CanvasNode, GenStatus, ImageResult } from './types'

const ACTIVE: GenStatus[] = ['排队中', '生成中']
const inFlight = new Set<string>()

function edgeKind(edge: CanvasEdge, nodes: CanvasNode[]): 'prompt' | 'reference' {
  if (edge.data?.kind) return edge.data.kind
  const source = nodes.find((node) => node.id === edge.source)
  return source?.type === 'prompt' ? 'prompt' : 'reference'
}

export function connectedPromptText(nodes: CanvasNode[], edges: CanvasEdge[], generatorId: string): string {
  return edges
    .filter((edge) => edge.target === generatorId && edgeKind(edge, nodes) === 'prompt')
    .map((edge) => {
      const source = nodes.find((node) => node.id === edge.source)
      return source?.type === 'prompt' ? source.data.text.trim() : ''
    })
    .filter(Boolean)
    .join('\n\n')
}

export type ConnectedReference = {
  id: string
  uploadId: string
  name: string
  url: string
}

export function connectedReferences(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  generatorId: string,
): ConnectedReference[] {
  return edges
    .filter((edge) => edge.target === generatorId && edgeKind(edge, nodes) === 'reference')
    .map((edge) => {
      const source = nodes.find((node) => node.id === edge.source)
      if (!source || source.type !== 'asset' || !source.data.uploadId) return null
      return {
        id: source.id,
        uploadId: source.data.uploadId,
        name: source.data.name || '参考图',
        url: source.data.url || `/api/uploads/${source.data.uploadId}`,
      }
    })
    .filter((item): item is ConnectedReference => item !== null)
}

export function toast(text: string): void {
  const app = (window as unknown as { OutfitApp?: { toast?: (value: string) => void } }).OutfitApp
  app?.toast?.(text)
}

export async function runGenerator(generatorId: string): Promise<void> {
  const state = useCanvasStore.getState()
  const node = state.nodes.find((item) => item.id === generatorId)
  if (!node || node.type !== 'generator') return
  if (ACTIVE.includes(node.data.status)) return

  const prompt = connectedPromptText(state.nodes, state.edges, generatorId) || node.data.prompt.trim()
  if (!prompt) {
    state.updateNodeData(generatorId, { status: '生成失败', error: '请先填写提示词。' })
    return
  }
  if (!state.options?.configured) {
    state.updateNodeData(generatorId, {
      status: '生成失败',
      error: state.options?.reason || '生图功能未配置。',
    })
    return
  }

  const references = connectedReferences(state.nodes, state.edges, generatorId)
  state.updateNodeData(generatorId, {
    status: '排队中',
    progress: 0,
    error: '',
    images: [],
    taskId: undefined,
  })

  try {
    const task = await createImageTask({
      prompt,
      model: node.data.model,
      size: node.data.size,
      quality: node.data.quality,
      count: node.data.count,
      upload_ids: references.map((item) => item.uploadId),
    })
    state.updateNodeData(generatorId, {
      status: toGenStatus(task.status),
      taskId: task.id,
      progress: task.progress || 0,
    })
  } catch (error) {
    state.updateNodeData(generatorId, { status: '生成失败', error: errorMessage(error) })
  }
}

export async function pollGenerator(generatorId: string): Promise<void> {
  const state = useCanvasStore.getState()
  const node = state.nodes.find((item) => item.id === generatorId)
  if (!node || node.type !== 'generator') return
  const taskId = node.data.taskId
  if (!taskId || !ACTIVE.includes(node.data.status)) return
  if (inFlight.has(taskId)) return

  inFlight.add(taskId)
  try {
    const task = await getImageTask(taskId)
    const status = toGenStatus(task.status)
    const images: ImageResult[] = (task.images || []).map((image) => ({
      index: image.index,
      name: image.name,
      width: image.width,
      height: image.height,
      url: image.url,
      downloadUrl: image.download_url,
    }))
    state.updateNodeData(generatorId, {
      status,
      progress: task.progress || 0,
      error: task.error || '',
      images,
      elapsedMs: task.elapsed_ms || 0,
    })
    if (!ACTIVE.includes(status)) toast(`生图${status}`)
  } catch (error) {
    state.updateNodeData(generatorId, { status: '生成失败', error: errorMessage(error) })
  } finally {
    inFlight.delete(taskId)
  }
}
