/**
 * 图片槽位网格：10 个格子（1 定妆 + 9 细节），点空格选图、点 × 删除并左移、支持拖拽与多选。
 *
 * 每个格子自带一个隐藏 input，用 `<label htmlFor>` 原生打开文件选择器——这是原生版
 * `image-slots.js` 特意保留的做法（某些浏览器会吞掉隐藏 input 上的 .click()）。
 */
import type { DragEvent, ChangeEvent } from 'react';
import { SLOT_MAX, type SlotItem } from '../../lib/image-slots';

interface Props {
  slots: SlotItem[];
  busy?: boolean;
  readonly?: boolean;
  onFiles: (files: FileList | File[] | null, replaceIndex?: number) => void;
  onRemove: (index: number) => void;
}

export function ImageSlotGrid({ slots, busy, readonly, onFiles, onRemove }: Props) {
  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.classList.remove('is-drop');
    if (readonly) return;
    onFiles(event.dataTransfer?.files ?? null, 0);
  };

  return (
    <div
      className="slot-grid"
      onDragOver={(event) => {
        if (readonly) return;
        event.preventDefault();
        event.currentTarget.classList.add('is-drop');
      }}
      onDragLeave={(event) => event.currentTarget.classList.remove('is-drop')}
      onDrop={handleDrop}
    >
      {Array.from({ length: SLOT_MAX }, (_, offset) => {
        const index = offset + 1;
        const item = slots[offset];
        const primary = index === 1;
        const inputId = `slot-input-${index}`;
        return (
          <div
            key={index}
            className={`slot-cell ${item ? 'is-filled' : 'is-empty'}${primary ? ' is-primary' : ''}`}
          >
            {item ? (
              <>
                <img className="slot-thumb" src={item.previewUrl} alt={item.name} />
                <span className="slot-index">图{index}</span>
                {!readonly && (
                  <button
                    type="button"
                    className="slot-remove"
                    title="移除这张图"
                    onClick={() => onRemove(index)}
                  >
                    ×
                  </button>
                )}
                {item.status === 'uploading' && (
                  <span className="slot-progress">
                    <span className="slot-progress-bar" style={{ width: `${item.progress || 5}%` }} />
                    <span className="slot-progress-text">{item.progress}%</span>
                  </span>
                )}
                {item.status === 'error' && <span className="slot-error" title={item.error}>上传失败</span>}
              </>
            ) : (
              <label className="slot-empty" htmlFor={readonly ? undefined : inputId}>
                <span className="slot-add">+</span>
                <span className="slot-caption">{primary ? '定妆图（必填）' : `图${index} · 细节`}</span>
              </label>
            )}
            {!readonly && (
              <input
                id={inputId}
                className="slot-input"
                type="file"
                accept="image/jpeg,image/png,image/webp,image/bmp"
                multiple={!item}
                onChange={(event: ChangeEvent<HTMLInputElement>) => {
                  onFiles(event.target.files, item ? index : 0);
                  event.target.value = '';
                }}
              />
            )}
          </div>
        );
      })}
      <p className="slot-hint">
        共 {slots.length}/{SLOT_MAX} 张{busy ? ' · 上传中…' : ''}：第 1 张是定妆图（必填），第 2 张起是细节图；
        提示词里的「图2…图10」按这里的顺序对应，删除中间某张时后面的图会自动前移。
      </p>
    </div>
  );
}
