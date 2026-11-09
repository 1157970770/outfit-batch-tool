import type { Edge, Node, Viewport } from '@xyflow/react'

export type ImageResult = {
  index: number
  name: string
  width?: number | null
  height?: number | null
  url: string
  downloadUrl: string
}

export type GenStatus = 'idle' | '排队中' | '生成中' | '已完成' | '部分完成' | '生成失败'

export type PromptData = {
  text: string
}

export type AssetData = {
  uploadId?: string
  name?: string
  url?: string
  width?: number | null
  height?: number | null
  error?: string
  autoPick?: boolean
}

export type GeneratorData = {
  prompt: string
  model: string
  size: string
  quality: string
  count: number
  status: GenStatus
  progress: number
  error: string
  taskId?: string
  images: ImageResult[]
  elapsedMs?: number
}

export type PromptNodeType = Node<PromptData, 'prompt'>
export type AssetNodeType = Node<AssetData, 'asset'>
export type GeneratorNodeType = Node<GeneratorData, 'generator'>

export type CanvasNode = PromptNodeType | AssetNodeType | GeneratorNodeType
export type CanvasEdge = Edge<{ kind: 'prompt' | 'reference' }>
export type CanvasViewport = Viewport

export type ImageGenOption = {
  value: string
  label: string
}

export type ImageGenModel = {
  id: string
  label: string
  provider: string
  price: string
  sizes: ImageGenOption[]
  qualities: ImageGenOption[]
}

export type ImageGenOptions = {
  configured: boolean
  reason: string
  models: ImageGenModel[]
  default_model: string
  default_quality: string
  max_count: number
  max_references: number
  price: string
  prompt_limit: number
}
