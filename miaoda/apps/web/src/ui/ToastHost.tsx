import { useEffect } from 'react';

import { useToast } from '@/ui/toast';

export function ToastHost() {
  const message = useToast();
  useEffect(() => {
    // 保持与原版一致的定位行为：消息变化时把节点重新挂到 body 末尾之上（这里只做滚动重置）
    if (message) window.scrollTo({ top: window.scrollY });
  }, [message?.seq]);

  return (
    <div id="toast" className={`toast${message ? ' is-visible' : ''}${message ? ` toast-${message.tone}` : ''}`} role="status" aria-live="polite">
      {message?.text || ''}
    </div>
  );
}
