import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from '@/App';
import { bootstrap } from '@/session';
import '@/styles/shell.css';
import '@/styles/components.css';
import '@/styles/create.css';
import '@/styles/batch.css';
import '@/styles/history.css';

const host = document.getElementById('root');
if (!host) throw new Error('缺少 #root 挂载点');

createRoot(host).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// 先探活再渲染：status=boot 时 App 显示启动页，避免闪一下登录框。
void bootstrap();
