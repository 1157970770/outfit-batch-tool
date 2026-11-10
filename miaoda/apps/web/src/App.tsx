import { useEffect, type ComponentType } from 'react';

import { isAdmin, logout, useSession } from '@/session';
import { BootView, LoginView } from '@/pages/Entry';
import { CreatePage } from '@/pages/CreatePage';
import {
  AdminPlaceholder,
  BatchPlaceholder,
  CanvasPlaceholder,
  HistoryPlaceholder,
  ImageGenPlaceholder,
  MaterialPlaceholder,
  PsPlaceholder,
  TagPlaceholder,
  VIEWS,
  ViewSection,
  useView,
} from '@/pages/page-registry';
import { ToastHost } from '@/ui/ToastHost';

const MATERIAL_URL = 'http://192.168.10.179:8700';

const PAGES: Record<string, ComponentType> = {
  create: CreatePage,
  batch: BatchPlaceholder,
  history: HistoryPlaceholder,
  admin: AdminPlaceholder,
  tag: TagPlaceholder,
  material: MaterialPlaceholder,
  ps: PsPlaceholder,
  imagegen: ImageGenPlaceholder,
  canvas: CanvasPlaceholder,
};

export function App() {
  const session = useSession();
  const [view, setView] = useView('create');
  const admin = isAdmin(session);

  useEffect(() => {
    // 原版：管理员页普通账号不可见，切走后回落到试跑页
    if (!admin && view === 'admin') setView('create');
  }, [admin, view, setView]);

  if (session.status === 'boot') return <BootView />;
  if (session.status === 'anon') return <LoginView hint={session.error} />;

  const service = session.service;

  return (
    <div id="app-view" className="app-view">
      <header className="topbar">
        <div className="brand">
          换装批量生产器
          {service?.dev_mode ? <span className="dev-chip">开发版</span> : null}
        </div>
        <div id="service-status" className="service-status">
          {service ? `${service.templates_count} 个可用模板 · 并发 ${service.max_concurrency}` : '加载中…'}
        </div>
        <div className="topbar-right">
          <a className="link" href="http://www.runninghub.cn" target="_blank" rel="noreferrer">
            RunningHub
          </a>
          <span className="who">{session.user?.username}</span>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              void logout();
            }}
          >
            退出
          </button>
        </div>
      </header>

      <nav className="tabs" aria-label="主导航">
        {VIEWS.filter((item) => !item.admin || admin).map((item) => (
          <button
            key={item.key}
            type="button"
            className={`tab${view === item.key ? ' active' : ''}`}
            onClick={() => setView(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <main className="views">
        {VIEWS.map((item) => {
          const Page = PAGES[item.key];
          if (!Page) return null;
          if (item.admin && !admin) return null;
          return (
            <ViewSection key={item.key} id={item.key} active={view === item.key}>
              <Page />
            </ViewSection>
          );
        })}
      </main>

      {service?.notice ? <aside className="notice-bar">{service.notice}</aside> : null}
      <footer className="foot">
        <span>妙搭版 · 与 Python 版共用同一份 server/*.json 与 web-data*（只读兼容）</span>
        <span className="muted">{MATERIAL_URL}</span>
      </footer>
      <ToastHost />
    </div>
  );
}
