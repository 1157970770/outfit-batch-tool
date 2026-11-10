/**
 * 视频场景选择：自动推荐（服务端随机抽）或手动挑选（最多 3 个，与原生版 `PICK_MAX=3` 一致）。
 *
 * 只把候选集里 `ready` 的场景算作「可生成」；未就绪的仍显示，但会说明为什么点了也用不了。
 */
import type { TemplateItem } from '../../lib/types';

export const PICK_MAX = 3;

interface Props {
  templates: TemplateItem[];
  mode: 'random' | 'selected';
  onMode: (mode: 'random' | 'selected') => void;
  picked: string[];
  onPicked: (ids: string[]) => void;
  count: number;
  maxCount: number;
}

export function TemplatePicker({ templates, mode, onMode, picked, onPicked, count, maxCount }: Props) {
  const toggle = (item: TemplateItem) => {
    if (picked.includes(item.id)) {
      onPicked(picked.filter((id) => id !== item.id));
      return;
    }
    if (picked.length >= PICK_MAX) {
      // 原生版就是直接拒绝第 4 个，而不是静默顶掉已选的
      return;
    }
    onPicked([...picked, item.id]);
  };

  return (
    <section className="create-block">
      <header className="create-block-head">
        <h3>视频场景</h3>
        <span className="create-block-hint">
          {mode === 'selected' ? `已选 ${picked.length}/${PICK_MAX} 个` : `自动推荐 ${Math.min(count, maxCount)} 个`}
        </span>
      </header>

      <div className="create-mode">
        <label className={`mode-chip${mode === 'random' ? ' is-on' : ''}`}>
          <input type="radio" name="scene-mode" checked={mode === 'random'} onChange={() => onMode('random')} />
          自动推荐
        </label>
        <label className={`mode-chip${mode === 'selected' ? ' is-on' : ''}`}>
          <input type="radio" name="scene-mode" checked={mode === 'selected'} onChange={() => onMode('selected')} />
          我来挑选
        </label>
      </div>

      {mode === 'random' ? (
        <p className="create-note">
          按下面的「生成条数」从当前筛选结果里随机抽取场景；筛选条件越窄，可用场景越少。
        </p>
      ) : (
        <>
          <p className="create-note">
            点选 1–{PICK_MAX} 个场景，按当前标签筛选的结果里任选；未就绪的场景不会出现在候选集里。
          </p>
          <div className="pick-grid">
            {templates.map((item) => {
              const index = picked.indexOf(item.id);
              return (
                <button
                  type="button"
                  key={item.id}
                  className={`pick-card${index >= 0 ? ' is-on' : ''}`}
                  onClick={() => toggle(item)}
                  title={item.tags.join(' · ')}
                >
                  <span className="pick-id">#{item.id}</span>
                  <span className="pick-name">{item.name}</span>
                  <span className="pick-meta">
                    {item.duration ? `${item.duration}s` : '时长未知'}
                    {item.size_mb ? ` · ${item.size_mb}MB` : ''}
                  </span>
                  {index >= 0 && <span className="pick-badge">{index + 1}</span>}
                </button>
              );
            })}
            {!templates.length && <p className="create-note">当前筛选条件下没有视频场景，先放宽标签筛选。</p>}
          </div>
        </>
      )}
    </section>
  );
}
