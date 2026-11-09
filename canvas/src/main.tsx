import { createRoot, type Root } from 'react-dom/client'
import '@xyflow/react/dist/style.css'
import './canvas.css'
import { App } from './App'

let root: Root | null = null

function enter(): void {
  const container = document.getElementById('canvas-root')
  if (!container) return
  if (!root) {
    root = createRoot(container)
    root.render(<App />)
  }
  window.dispatchEvent(new CustomEvent('canvas:enter'))
}

function leave(): void {
  window.dispatchEvent(new CustomEvent('canvas:leave'))
}

;(window as unknown as { CanvasUI: { enter: () => void; leave: () => void } }).CanvasUI = {
  enter,
  leave,
}

// app.js 的 bootstrap 可能在 canvas.js 执行前就完成并恢复「画布」tab；
// 那种情况下没人再调 enter()，画布会一直是空的。
if (document.getElementById('canvas-view')?.classList.contains('active-view')) enter()
