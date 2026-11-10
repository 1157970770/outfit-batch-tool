import type { ReactNode } from 'react';

export interface PlaceholderProps {
  title: string;
  phase: string;
  summary: string;
  bullets: string[];
  children?: ReactNode;
}

/**
 * P0 的占位页：写清这一页在哪个阶段迁移、要满足哪些交互，
 * 让「迁移进度」在界面上可见，而不是只躺在文档里。
 */
export function Placeholder({ title, phase, summary, bullets, children }: PlaceholderProps) {
  return (
    <div className="placeholder">
      <header className="placeholder-head">
        <h2>{title}</h2>
        <span className="phase-chip">{phase}</span>
      </header>
      <p className="placeholder-summary">{summary}</p>
      <ul className="placeholder-list">
        {bullets.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      {children}
    </div>
  );
}
