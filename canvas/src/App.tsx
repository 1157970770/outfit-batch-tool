import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type NodeTypes,
  type OnConnectEnd,
  SelectionMode,
} from '@xyflow/react'
import {
  IconArrowBackUp,
  IconArrowForwardUp,
  IconBookmark,
  IconFocusCentered,
  IconMessagePlus,
  IconPhotoPlus,
  IconScissors,
  IconSparkles,
} from '@tabler/icons-react'
import { errorMessage, fetchBootstrap } from './api'
import { connectionForNodes } from './connect'
import { LibraryPanel } from './LibraryPanel'
import { AssetNode } from './nodes/AssetNode'
import { GeneratorNode } from './nodes/GeneratorNode'
import { PromptNode } from './nodes/PromptNode'
import { loadLibrary, loadProject, saveLibrary, saveProject, type LibraryItem } from './persist'
import { pollGenerator } from './run'
import { useCanvasStore } from './store'
import type { CanvasNode, ImageGenOptions } from './types'

const nodeTypes = {
  prompt: PromptNode,
  asset: AssetNode,
  generator: GeneratorNode,
} as NodeTypes

type MenuState = { left: number; top: number; clientX: number; clientY: number }
type EdgeMenuState = { left: number; top: number; edgeId: string }

function applyDefaults(options: ImageGenOptions) {
  const state = useCanvasStore.getState()
  const fallback = options.models.find((item) => item.id === options.default_model) ?? options.models[0]
  if (!fallback) return
  let changed = false
  const nodes = state.nodes.map((node) => {
    if (node.type !== 'generator') return node
    const patch: Record<string, unknown> = {}
    if (!node.data.model) patch.model = fallback.id
    const model = options.models.find((item) => item.id === (patch.model ?? node.data.model)) ?? fallback
    if (!node.data.size) patch.size = model.sizes[0]?.value ?? 'auto'
    if (!node.data.quality) {
      patch.quality = model.qualities.some((item) => item.value === options.default_quality)
        ? options.default_quality
        : (model.qualities[0]?.value ?? 'auto')
    }
    if (!Object.keys(patch).length) return node
    changed = true
    return { ...node, data: { ...node.data, ...patch } }
  })
  if (changed) useCanvasStore.setState({ nodes })
}

function Canvas() {
  const nodes = useCanvasStore((state) => state.nodes)
  const edges = useCanvasStore((state) => state.edges)
  const ready = useCanvasStore((state) => state.ready)
  const hydrated = useCanvasStore((state) => state.hydrated)
  const onNodesChange = useCanvasStore((state) => state.onNodesChange)
  const onEdgesChange = useCanvasStore((state) => state.onEdgesChange)
  const onConnect = useCanvasStore((state) => state.onConnect)
  const beginGesture = useCanvasStore((state) => state.beginGesture)
  const endGesture = useCanvasStore((state) => state.endGesture)
  const undo = useCanvasStore((state) => state.undo)
  const redo = useCanvasStore((state) => state.redo)
  const canUndo = useCanvasStore((state) => state.past.length > 0)
  const canRedo = useCanvasStore((state) => state.future.length > 0)
  const libraryCount = useCanvasStore((state) => state.library.length)

  const { screenToFlowPosition, setViewport, fitView, getNodes } = useReactFlow()
  const wrapperRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  const [menu, setMenu] = useState<MenuState | null>(null)
  const [edgeMenu, setEdgeMenu] = useState<EdgeMenuState | null>(null)
  const [libraryOpen, setLibraryOpen] = useState(() => {
    try {
      return localStorage.getItem('outfit.canvas.libraryOpen') !== '0'
    } catch {
      return true
    }
  })

  const toggleLibrary = useCallback(() => {
    setLibraryOpen((open) => {
      const next = !open
      try {
        localStorage.setItem('outfit.canvas.libraryOpen', next ? '1' : '0')
      } catch {
        /* 存不了开关状态不影响使用 */
      }
      return next
    })
  }, [])

  const tick = useCallback(async () => {
    const view = document.getElementById('canvas-view')
    if (!view?.classList.contains('active-view')) return
    const state = useCanvasStore.getState()
    const pending = state.nodes.filter(
      (node) =>
        node.type === 'generator' &&
        (node.data.status === '排队中' || node.data.status === '生成中') &&
        node.data.taskId,
    )
    for (const node of pending) await pollGenerator(node.id)
  }, [])

  const enterCanvas = useCallback(async () => {
    try {
      const data = await fetchBootstrap()
      const state = useCanvasStore.getState()
      if (state.username !== data.user.username) {
        const project = loadProject(data.user.username)
        state.reset()
        state.setUsername(data.user.username)
        state.hydrateLibrary(loadLibrary(data.user.username))
        if (project) state.hydrate(project.nodes, project.edges, project.viewport)
      }
      useCanvasStore.getState().setOptions(data.service.imagegen)
      applyDefaults(data.service.imagegen)
      useCanvasStore.getState().setReady(true)
      setError('')
      void tick()
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [tick])

  useEffect(() => {
    void enterCanvas()
    const onEnter = () => void enterCanvas()
    window.addEventListener('canvas:enter', onEnter)
    return () => window.removeEventListener('canvas:enter', onEnter)
  }, [enterCanvas])

  useEffect(() => {
    const timer = window.setInterval(() => void tick(), 2500)
    return () => window.clearInterval(timer)
  }, [tick])

  useEffect(() => {
    if (!ready) return
    const state = useCanvasStore.getState()
    if (state.hasSavedViewport) {
      void setViewport(state.viewport)
    } else if (state.nodes.length) {
      const timer = window.setTimeout(() => void fitView({ padding: 0.22, duration: 260 }), 80)
      return () => window.clearTimeout(timer)
    }
  }, [ready, hydrated, setViewport, fitView])

  useEffect(() => {
    let timer: number | undefined
    const unsubscribe = useCanvasStore.subscribe((state, previous) => {
      if (!state.ready || !state.username) return
      if (
        state.nodes === previous.nodes &&
        state.edges === previous.edges &&
        state.viewport === previous.viewport
      ) {
        return
      }
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        const current = useCanvasStore.getState()
        saveProject(current.username, {
          version: 1,
          nodes: current.nodes,
          edges: current.edges,
          viewport: current.viewport,
        })
      }, 800)
    })
    return () => {
      unsubscribe()
      window.clearTimeout(timer)
    }
  }, [])

  useEffect(() => {
    let timer: number | undefined
    const unsubscribe = useCanvasStore.subscribe((state, previous) => {
      if (state.library === previous.library || !state.username) return
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        const current = useCanvasStore.getState()
        saveLibrary(current.username, current.library)
      }, 400)
    })
    return () => {
      unsubscribe()
      window.clearTimeout(timer)
    }
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)
      ) {
        return
      }
      if (!(event.ctrlKey || event.metaKey)) return
      const key = event.key.toLowerCase()
      if (key === 'z') {
        event.preventDefault()
        if (event.shiftKey) redo()
        else undo()
      } else if (key === 'y') {
        event.preventDefault()
        redo()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo])

  const addAtCenter = useCallback(
    (kind: CanvasNode['type']) => {
      const rect = wrapperRef.current?.getBoundingClientRect()
      const center = screenToFlowPosition({
        x: (rect?.left ?? 0) + (rect?.width ?? 900) / 2,
        y: (rect?.top ?? 0) + (rect?.height ?? 600) / 2,
      })
      const offset = (useCanvasStore.getState().nodes.length % 6) * 26
      useCanvasStore.getState().addNode(kind, {
        x: center.x - 170 + offset,
        y: center.y - 170 + offset,
      })
    },
    [screenToFlowPosition],
  )

  const placeLibraryItem = useCallback(
    (item: LibraryItem) => {
      const rect = wrapperRef.current?.getBoundingClientRect()
      const center = screenToFlowPosition({
        x: (rect?.left ?? 0) + (rect?.width ?? 900) / 2,
        y: (rect?.top ?? 0) + (rect?.height ?? 600) / 2,
      })
      const offset = (useCanvasStore.getState().nodes.length % 6) * 26
      useCanvasStore.getState().addNode(
        'asset',
        { x: center.x - 150 + offset, y: center.y - 150 + offset },
        {
          uploadId: item.uploadId,
          name: item.name,
          url: item.url,
          width: item.width ?? null,
          height: item.height ?? null,
        },
      )
    },
    [screenToFlowPosition],
  )

  const openMenu = useCallback((event: ReactMouseEvent | MouseEvent) => {
    event.preventDefault()
    setEdgeMenu(null)
    const rect = wrapperRef.current?.getBoundingClientRect()
    if (!rect) return
    const width = 196
    const height = 128
    setMenu({
      left: Math.max(8, Math.min(event.clientX - rect.left, rect.width - width - 8)),
      top: Math.max(8, Math.min(event.clientY - rect.top, rect.height - height - 8)),
      clientX: event.clientX,
      clientY: event.clientY,
    })
  }, [])

  const openEdgeMenu = useCallback((event: ReactMouseEvent, edge: Edge) => {
    event.preventDefault()
    setMenu(null)
    const rect = wrapperRef.current?.getBoundingClientRect()
    if (!rect) return
    setEdgeMenu({
      edgeId: edge.id,
      left: Math.max(8, Math.min(event.clientX - rect.left, rect.width - 196 - 8)),
      top: Math.max(8, Math.min(event.clientY - rect.top, rect.height - 52 - 8)),
    })
  }, [])

  const createFromMenu = useCallback(
    (kind: CanvasNode['type']) => {
      if (!menu) return
      const position = screenToFlowPosition({ x: menu.clientX, y: menu.clientY })
      const width = kind === 'generator' ? 340 : kind === 'asset' ? 300 : 320
      useCanvasStore.getState().addNode(
        kind,
        { x: position.x - width / 2, y: position.y - 36 },
        kind === 'asset' ? { autoPick: true } : undefined,
      )
      setMenu(null)
    },
    [menu, screenToFlowPosition],
  )

  useEffect(() => {
    if (!menu) return
    const onPointerDown = (event: Event) => {
      const target = event.target as Element | null
      if (target?.closest?.('.cv-menu')) return
      setMenu(null)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(null)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [menu])

  useEffect(() => {
    if (!edgeMenu) return
    const onPointerDown = (event: Event) => {
      const target = event.target as Element | null
      if (target?.closest?.('.cv-menu')) return
      setEdgeMenu(null)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setEdgeMenu(null)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [edgeMenu])

  // 松手落在节点身上（而不是 14px 的圆点）也算连上：看松手位置下的节点，
  // 再按两端类型决定方向，交给 store.onConnect 统一校验。
  const handleConnectEnd = useCallback<OnConnectEnd>((event, connectionState) => {
    const from = connectionState.fromNode as CanvasNode | null
    if (connectionState.toNode || !from) return
    const point = 'changedTouches' in event ? event.changedTouches[0] : event
    if (!point) return
    const nodeEl = (document.elementFromPoint(point.clientX, point.clientY) as Element | null)
      ?.closest('.react-flow__node')
    const hitId = nodeEl?.getAttribute('data-id') || ''
    const hit = hitId
      ? ((getNodes() as CanvasNode[]).find((node) => node.id === hitId) ?? null)
      : null
    const connection = connectionForNodes(from, hit)
    if (connection) {
      useCanvasStore.getState().onConnect({
        ...connection,
        sourceHandle: null,
        targetHandle: null,
      })
    }
  }, [getNodes])

  const isValidConnection = useCallback((connection: Connection | Edge) => {
    const state = useCanvasStore.getState()
    const source = state.nodes.find((node) => node.id === connection.source)
    const target = state.nodes.find((node) => node.id === connection.target)
    if (!source || !target || target.type !== 'generator') return false
    if (source.type !== 'prompt' && source.type !== 'asset') return false
    if (source.type === 'asset' && !source.data.uploadId) return false
    if (source.id === target.id) return false
    if (state.edges.some((edge) => edge.source === source.id && edge.target === target.id)) return false
    if (source.type === 'asset') {
      const max = state.options?.max_references ?? 10
      const count = state.edges.filter(
        (edge) => edge.target === target.id && (edge.data?.kind ?? 'reference') === 'reference',
      ).length
      if (count >= max) return false
    }
    return true
  }, [])

  return (
    <div
      className='cv-canvas'
      ref={wrapperRef}
      onDoubleClick={(event) => {
        const target = event.target as HTMLElement
        if (!target.classList.contains('react-flow__pane')) return
        const position = screenToFlowPosition({ x: event.clientX, y: event.clientY })
        useCanvasStore.getState().addNode('generator', {
          x: position.x - 170,
          y: position.y - 170,
        })
      }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onConnectEnd={handleConnectEnd}
        onEdgeContextMenu={openEdgeMenu}
        onNodeDragStart={beginGesture}
        onNodeDragStop={endGesture}
        onMoveStart={() => {
          setMenu(null)
          setEdgeMenu(null)
        }}
        onPaneContextMenu={openMenu}
        onMoveEnd={(_, viewport) => useCanvasStore.getState().setViewport(viewport)}
        isValidConnection={isValidConnection}
        selectionKeyCode='Shift'
        selectionOnDrag
        selectionMode={SelectionMode.Partial}
        panOnDrag={[1]}
        deleteKeyCode={['Backspace', 'Delete']}
        minZoom={0.15}
        maxZoom={2.5}
        zoomOnDoubleClick={false}
        fitView={false}
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1.4} color='#d7d5cf' />
        <Controls position='bottom-left' showInteractive={false} />
        <Panel position='top-left' className='cv-toolbar'>
          <button type='button' onClick={() => addAtCenter('prompt')} title='添加提示词节点'>
            <IconMessagePlus size={16} stroke={1.8} />
            <span>提示词</span>
          </button>
          <button type='button' onClick={() => addAtCenter('generator')} title='添加生图节点'>
            <IconSparkles size={16} stroke={1.8} />
            <span>生图</span>
          </button>
          <button type='button' onClick={() => addAtCenter('asset')} title='添加图片素材节点'>
            <IconPhotoPlus size={16} stroke={1.8} />
            <span>图片</span>
          </button>
          <span className='cv-toolbar-sep' />
          <button
            type='button'
            className='cv-tool-btn'
            disabled={!canUndo}
            onClick={undo}
            title='撤销（Ctrl+Z）'
          >
            <IconArrowBackUp size={16} stroke={1.8} />
          </button>
          <button
            type='button'
            className='cv-tool-btn'
            disabled={!canRedo}
            onClick={redo}
            title='重做（Ctrl+Shift+Z）'
          >
            <IconArrowForwardUp size={16} stroke={1.8} />
          </button>
          <button
            type='button'
            className='cv-tool-btn'
            onClick={() => void fitView({ padding: 0.22, duration: 240 })}
            title='适应画布'
          >
            <IconFocusCentered size={16} stroke={1.8} />
          </button>
          <span className='cv-toolbar-sep' />
          <button
            type='button'
            className='cv-tool-btn'
            aria-pressed={libraryOpen}
            onClick={toggleLibrary}
            title='素材库：保存可复用的图片'
          >
            <IconBookmark size={16} stroke={1.8} />
            {libraryCount ? <span className='cv-tool-badge'>{libraryCount}</span> : null}
          </button>
        </Panel>
      </ReactFlow>

      {!nodes.length && ready ? (
        <div className='cv-empty'>
          <div className='cv-empty-card'>
            <strong>从一张图或一句提示词开始</strong>
            <p>双击空白处新建生图节点；拖拽空白处框选多个节点，按住空格或鼠标中键拖动画面。</p>
            <div className='cv-empty-actions'>
              <button type='button' onClick={() => addAtCenter('generator')}>
                <IconSparkles size={15} /> 新建生图节点
              </button>
              <button type='button' className='is-quiet' onClick={() => addAtCenter('asset')}>
                <IconPhotoPlus size={15} /> 上传参考图
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {error ? (
        <div className='cv-empty'>
          <div className='cv-empty-card'>
            <strong>画布加载失败</strong>
            <p>{error}</p>
            <div className='cv-empty-actions'>
              <button type='button' onClick={() => void enterCanvas()}>重试</button>
            </div>
          </div>
        </div>
      ) : null}

      {menu ? (
        <div
          className='cv-menu'
          style={{ left: menu.left, top: menu.top }}
          role='menu'
          onContextMenu={(event) => event.preventDefault()}
        >
          <button type='button' role='menuitem' onClick={() => createFromMenu('prompt')}>
            <IconMessagePlus size={15} stroke={1.8} />
            <span>提示词</span>
            <em>Prompt</em>
          </button>
          <button type='button' role='menuitem' onClick={() => createFromMenu('generator')}>
            <IconSparkles size={15} stroke={1.8} />
            <span>生图节点</span>
            <em>Generate</em>
          </button>
          <button type='button' role='menuitem' onClick={() => createFromMenu('asset')}>
            <IconPhotoPlus size={15} stroke={1.8} />
            <span>上传图片</span>
            <em>Image</em>
          </button>
        </div>
      ) : null}

      {edgeMenu ? (
        <div
          className='cv-menu'
          style={{ left: edgeMenu.left, top: edgeMenu.top }}
          role='menu'
          onContextMenu={(event) => event.preventDefault()}
        >
          <button
            type='button'
            role='menuitem'
            onClick={() => {
              useCanvasStore.getState().removeEdge(edgeMenu.edgeId)
              setEdgeMenu(null)
            }}
          >
            <IconScissors size={15} stroke={1.8} />
            <span>剪断连线</span>
            <em>Delete</em>
          </button>
        </div>
      ) : null}

      <LibraryPanel open={libraryOpen} onClose={toggleLibrary} onPlace={placeLibraryItem} />
    </div>
  )
}

export function App() {
  return (
    <ReactFlowProvider>
      <Canvas />
    </ReactFlowProvider>
  )
}
