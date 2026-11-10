import { useEffect, useRef, useState } from 'react';

/**
 * 画布：复用 `canvas/` 工程已经打好的 IIFE 包（`public/canvas/canvas.js`）。
 *
 * 为什么不把 canvas/src 直接编进这个 app：那个包是**独立 root**、自带 React 与
 * react-flow 的全局样式，官方产物就是单文件 + 独立 css（`canvas/vite.config.ts` 用
 * lib/IIFE 输出）。这里维持同样的边界：
 *
 * · 首次切进本页才注入 `<link>` + `<script>`（原版 `<script defer>` 是首屏就加载）；
 * · 包自己会 `getElementById('canvas-root')` 建 root，并在 `#canvas-view.active-view`
 *   时自动 enter；这里在脚本 load 后补一次 `CanvasUI.enter()`，保证晚加载也不空白；
 * · 切走不卸载（与原版一致），只是 `CanvasUI.leave()`（它会停掉 2.5s 轮询）。
 */
const ASSET_VERSION = '20261109t';

export function CanvasPage() {
  const [mounted, setMounted] = useState(false);
  const seenRef = useRef(false);

  useEffect(() => {
    const section = document.getElementById('canvas-view');
    if (!section) return;

    const check = () => {
      if (seenRef.current) return;
      if (!section.hidden) {
        seenRef.current = true;
        setMounted(true);
      }
    };
    check();
    const observer = new MutationObserver(check);
    observer.observe(section, { attributes: true, attributeFilter: ['hidden', 'class'] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!mounted) return;
    if (document.getElementById('canvas-bundle')) return;

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = `/canvas/canvas.css?v=${ASSET_VERSION}`;
    document.head.appendChild(link);

    const script = document.createElement('script');
    script.id = 'canvas-bundle';
    script.src = `/canvas/canvas.js?v=${ASSET_VERSION}`;
    script.defer = true;
    script.addEventListener('load', () => {
      // 包加载完若当前就在画布页，补一次 enter（包的自动 enter 只覆盖「加载时已激活」的情况）
      const section = document.getElementById('canvas-view');
      if (section && !section.hidden) {
        (window as unknown as { CanvasUI?: { enter?: () => void } }).CanvasUI?.enter?.();
      }
    });
    document.body.appendChild(script);
  }, [mounted]);

  // 离开本页时通知画布停轮询（原版 app.js 的 switchView 也是这么做的）
  useEffect(() => {
    const section = document.getElementById('canvas-view');
    if (!section) return;
    const observer = new MutationObserver(() => {
      if (section.hidden) {
        (window as unknown as { CanvasUI?: { leave?: () => void } }).CanvasUI?.leave?.();
      }
    });
    observer.observe(section, { attributes: true, attributeFilter: ['hidden'] });
    return () => observer.disconnect();
  }, []);

  return (
    <div className="canvas-page">
      <div id="canvas-root" className="canvas-root" />
      {!mounted && <p className="tag-empty">画布在首次切到本页时加载。</p>}
    </div>
  );
}
