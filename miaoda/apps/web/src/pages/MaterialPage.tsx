import { useEffect, useRef, useState } from 'react';

const MATERIAL_URL = 'http://192.168.10.179:8700';

/**
 * 素材检索：整页嵌外部工具（原版同样是 iframe）。
 *
 * · **首次切进这一页才挂载**（原版 app.js 的懒挂载），之后切走也不卸载，
 *   免得每次回来都重新加载一遍。
 * · 该地址必须同时出现在后端 CSP 的 `frame-src` 里，否则浏览器直接拦掉。
 */
export function MaterialPage() {
  const [mounted, setMounted] = useState(false);
  const seenRef = useRef(false);

  useEffect(() => {
    const section = document.getElementById('material-view');
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

  return (
    <div className="material-page">
      {mounted ? (
        <iframe
          id="material-frame"
          className="material-frame"
          src={MATERIAL_URL}
          title="素材检索"
          referrerPolicy="no-referrer"
        />
      ) : (
        <p className="tag-empty">首次切到本页时才加载素材检索工具（{MATERIAL_URL}）。</p>
      )}
    </div>
  );
}
