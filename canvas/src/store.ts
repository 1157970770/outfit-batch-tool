import { addEdge, applyEdgeChanges, applyNodeChanges } from '@xyflow/react'
import type { Connection, EdgeChange, NodeChange, Viewport } from '@xyflow/react'
import { create } from 'zustand'
import type { LibraryItem } from './persist'
import type {
  CanvasEdge,
  CanvasNode,
  GenStatus,
  GeneratorData,
  ImageGenOptions,
} from './types'

type Snapshot = { nodes: CanvasNode[]; edges: CanvasEdge[] }

type CanvasState = {
  nodes: CanvasNode[]
  edges: CanvasEdge[]
  viewport: Viewport
  hasSavedViewport: boolean
  options: ImageGenOptions | null
  username: string
  ready: boolean
  hydrated: number
  library: LibraryItem[]
  past: Snapshot[]
  future: Snapshot[]
  gesture: Snapshot | null
  setUsername: (username: string) => void
  setOptions: (options: ImageGenOptions | null) => void
  setReady: (ready: boolean) => void
  hydrateLibrary: (items: LibraryItem[]) => void
  saveLibraryItem: (item: Omit<LibraryItem, 'id' | 'addedAt'>) => boolean
  renameLibraryItem: (id: string, name: string) => void
  removeLibraryItem: (id: string) => void
  reset: () => void
  hydrate: (nodes: CanvasNode[], edges: CanvasEdge[], viewport: Viewport) => void
  onNodesChange: (changes: NodeChange<CanvasNode>[]) => void
  onEdgesChange: (changes: EdgeChange<CanvasEdge>[]) => void
  onConnect: (connection: Connection) => void
  removeEdge: (id: string) => void
  addNode: (
    kind: CanvasNode['type'],
    position: { x: number; y: number },
    data?: Record<string, unknown>,
  ) => void
  updateNodeData: (id: string, patch: Record<string, unknown>) => void
  removeNodes: (ids: string[]) => void
  beginGesture: () => void
  endGesture: () => void
  commit: () => void
  undo: () => void
  redo: () => void
  setViewport: (viewport: Viewport) => void
}

const EMPTY_VIEWPORT: Viewport = { x: 0, y: 0, zoom: 1 }

function newId(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.().slice(0, 8) ?? Date.now().toString(36)
  return `${prefix}_${random}`
}

function defaultGeneratorData(options: ImageGenOptions | null): GeneratorData {
  const model = options?.models.find((item) => item.id === options.default_model) ?? options?.models[0]
  const quality = model?.qualities.some((item) => item.value === options?.default_quality)
    ? String(options?.default_quality)
    : (model?.qualities[0]?.value ?? 'auto')
  return {
    prompt: '',
    model: model?.id ?? '',
    size: model?.sizes[0]?.value ?? 'auto',
    quality,
    count: 1,
    status: 'idle',
    progress: 0,
    error: '',
    images: [],
  }
}

export const useCanvasStore = create<CanvasState>((set, get) => ({
  nodes: [],
  edges: [],
  viewport: EMPTY_VIEWPORT,
  hasSavedViewport: false,
  options: null,
  username: '',
  ready: false,
  hydrated: 0,
  library: [],
  past: [],
  future: [],
  gesture: null,

  setUsername: (username) => set({ username }),
  setOptions: (options) => set({ options }),
  setReady: (ready) => set({ ready }),

  hydrateLibrary: (items) => set({ library: items }),

  saveLibraryItem: (item) => {
    const state = get()
    if (!item.uploadId || state.library.some((entry) => entry.uploadId === item.uploadId)) {
      return false
    }
    const entry: LibraryItem = { ...item, id: newId('mat'), addedAt: Date.now() }
    set({ library: [entry, ...state.library].slice(0, 200) })
    return true
  },

  renameLibraryItem: (id, name) => {
    const value = name.trim().slice(0, 60)
    if (!value) return
    set((state) => ({
      library: state.library.map((item) => (item.id === id ? { ...item, name: value } : item)),
    }))
  },

  removeLibraryItem: (id) =>
    set((state) => ({ library: state.library.filter((item) => item.id !== id) })),

  reset: () =>
    set({
      nodes: [],
      edges: [],
      viewport: EMPTY_VIEWPORT,
      hasSavedViewport: false,
      ready: false,
      hydrated: 0,
      library: [],
      past: [],
      future: [],
      gesture: null,
    }),

  hydrate: (nodes, edges, viewport) =>
    set((state) => ({
      nodes,
      edges,
      viewport,
      hasSavedViewport: true,
      hydrated: state.hydrated + 1,
      past: [],
      future: [],
      gesture: null,
    })),

  onNodesChange: (changes) => {
    const removed = changes.filter((change) => change.type === 'remove').map((change) => change.id)
    if (removed.length) {
      get().removeNodes(removed)
      return
    }
    set({ nodes: applyNodeChanges(changes, get().nodes) as CanvasNode[] })
  },

  onEdgesChange: (changes) => {
    if (changes.some((change) => change.type === 'remove')) get().commit()
    set({ edges: applyEdgeChanges(changes, get().edges) as CanvasEdge[] })
  },

  onConnect: (connection) => {
    if (!connection.source || !connection.target) return
    const state = get()
    const source = state.nodes.find((node) => node.id === connection.source)
    const target = state.nodes.find((node) => node.id === connection.target)
    if (!source || !target || target.type !== 'generator') return
    if (source.type !== 'prompt' && source.type !== 'asset') return
    if (source.type === 'asset' && !source.data.uploadId) return
    if (state.edges.some((edge) => edge.source === source.id && edge.target === target.id)) return

    const kind = source.type === 'prompt' ? 'prompt' : 'reference'
    if (kind === 'reference') {
      const max = state.options?.max_references ?? 10
      const count = state.edges.filter(
        (edge) => edge.target === target.id && (edge.data?.kind ?? 'reference') === 'reference',
      ).length
      if (count >= max) return
    }

    state.commit()
    set({
      edges: addEdge({ ...connection, data: { kind } }, state.edges) as CanvasEdge[],
    })
  },

  removeEdge: (id) => {
    const state = get()
    if (!state.edges.some((edge) => edge.id === id)) return
    state.commit()
    set({ edges: state.edges.filter((edge) => edge.id !== id) })
  },

  addNode: (kind, position, data) => {
    const state = get()
    state.commit()
    const id = newId(kind)
    let node: CanvasNode
    if (kind === 'prompt') {
      node = { id, type: 'prompt', position, data: { text: '', ...data } }
    } else if (kind === 'asset') {
      node = { id, type: 'asset', position, data: { ...data } }
    } else {
      node = { id, type: 'generator', position, data: { ...defaultGeneratorData(state.options), ...data } }
    }
    set({ nodes: [...state.nodes, node] })
  },

  updateNodeData: (id, patch) =>
    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.id === id ? ({ ...node, data: { ...node.data, ...patch } } as CanvasNode) : node,
      ),
    })),

  removeNodes: (ids) => {
    const state = get()
    const keep = state.nodes.filter((node) => !ids.includes(node.id))
    if (keep.length === state.nodes.length) return
    state.commit()
    set({
      nodes: keep,
      edges: state.edges.filter(
        (edge) => !ids.includes(edge.source) && !ids.includes(edge.target),
      ),
    })
  },

  beginGesture: () =>
    set((state) =>
      state.gesture ? {} : { gesture: { nodes: state.nodes, edges: state.edges } },
    ),

  endGesture: () => {
    const state = get()
    const gesture = state.gesture
    if (!gesture) return
    if (gesture.nodes === state.nodes && gesture.edges === state.edges) {
      set({ gesture: null })
      return
    }
    set({
      past: [...state.past, gesture].slice(-50),
      future: [],
      gesture: null,
    })
  },

  commit: () =>
    set((state) => ({
      past: [...state.past, { nodes: state.nodes, edges: state.edges }].slice(-50),
      future: [],
    })),

  undo: () => {
    const state = get()
    if (!state.past.length) return
    const previous = state.past[state.past.length - 1]
    set({
      nodes: previous.nodes,
      edges: previous.edges,
      past: state.past.slice(0, -1),
      future: [{ nodes: state.nodes, edges: state.edges }, ...state.future].slice(0, 50),
      gesture: null,
    })
  },

  redo: () => {
    const state = get()
    if (!state.future.length) return
    const next = state.future[0]
    set({
      nodes: next.nodes,
      edges: next.edges,
      past: [...state.past, { nodes: state.nodes, edges: state.edges }].slice(-50),
      future: state.future.slice(1),
      gesture: null,
    })
  },

  setViewport: (viewport) => set({ viewport }),
}))

const KNOWN_STATUS: GenStatus[] = ['排队中', '生成中', '已完成', '部分完成', '生成失败']

export function toGenStatus(value: unknown): GenStatus {
  return KNOWN_STATUS.includes(value as GenStatus) ? (value as GenStatus) : '生成失败'
}
