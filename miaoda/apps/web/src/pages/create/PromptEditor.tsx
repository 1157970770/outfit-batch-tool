/**
 * 提示词：选模板 → 正文可改（只影响本次任务）。
 *
 * 原生版行为：换模板会把正文重置成模板 body，但用户改过之后再切模板会直接覆盖——
 * 这里保持一致，不做「未保存改动」的额外确认。
 */
import { useEffect, useState } from 'react';
import type { PromptItem } from '../../lib/types';

const MAX_PROMPT = 4000;

interface Props {
  prompts: PromptItem[];
  promptId: string;
  body: string;
  onPick: (id: string) => void;
  onBody: (body: string) => void;
}

export function PromptEditor({ prompts, promptId, body, onPick, onBody }: Props) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(body);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="prompt-block">
      <label className="field">
        提示词模板
        <select value={promptId} onChange={(event) => onPick(event.target.value)}>
          {prompts.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
            </option>
          ))}
          {!prompts.length && <option value="">（提示词库为空）</option>}
        </select>
      </label>
      <label className="field prompt-field">
        本次提示词
        <textarea
          value={body}
          maxLength={MAX_PROMPT}
          spellCheck={false}
          onChange={(event) => onBody(event.target.value)}
        />
      </label>
      <div className="prompt-meta">
        <span>修改只影响本次任务</span>
        <span>
          {body.length} / {MAX_PROMPT}
        </span>
        <button type="button" className="link-button" onClick={copy} disabled={!body}>
          {copied ? '已复制' : '复制提示词'}
        </button>
      </div>
    </div>
  );
}
