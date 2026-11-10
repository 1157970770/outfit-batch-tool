/**
 * 标签筛选面板（三态 chip）：无 → 包含 → 排除 → 无，循环点击。
 *
 * 原生版 `tag-filter.js` 的口径：同类标签 OR、跨类标签 AND、排除优先于包含；
 * 服务端 `TemplateCatalog.filterItems` 完全按这个规则过滤，所以前端只负责把两组
 * 字符串数组原样放进请求体（`tag_filters` / `tag_excludes`）。
 */
import { useMemo } from 'react';

export interface TagFilterValue {
  filters: Record<string, string[]>;
  excludes: Record<string, string[]>;
}

interface Props {
  categories: string[];
  values: Record<string, string[]>;
  value: TagFilterValue;
  onChange: (next: TagFilterValue) => void;
  /** 折叠时不渲染，省得把页面撑得太长。 */
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  /**
   * 是否允许「排除」。建任务接口只认 `tag_filters`（包含），所以小批量页传 false：
   * 点一下选中、再点一下取消；打标页那种三态只在纯前端筛表时才用得上。
   */
  allowExclude?: boolean;
}

const EMPTY: string[] = [];

export function TagFilterPanel({
  categories,
  values,
  value,
  onChange,
  collapsed,
  onToggleCollapse,
  allowExclude = true,
}: Props) {
  const total = useMemo(
    () =>
      Object.values(value.filters).reduce((sum, list) => sum + list.length, 0) +
      Object.values(value.excludes).reduce((sum, list) => sum + list.length, 0),
    [value],
  );

  const cycle = (category: string, tag: string) => {
    const include = new Set(value.filters[category] || EMPTY);
    if (!allowExclude) {
      if (include.has(tag)) include.delete(tag);
      else include.add(tag);
      onChange({ filters: { ...value.filters, [category]: [...include] }, excludes: value.excludes });
      return;
    }
    const exclude = new Set(value.excludes[category] || EMPTY);
    if (include.has(tag)) {
      include.delete(tag);
      exclude.add(tag);
    } else if (exclude.has(tag)) {
      exclude.delete(tag);
    } else {
      include.add(tag);
    }
    onChange({
      filters: { ...value.filters, [category]: [...include] },
      excludes: { ...value.excludes, [category]: [...exclude] },
    });
  };

  const clear = () => onChange({ filters: {}, excludes: {} });

  return (
    <section className="tag-filter">
      <header className="tag-filter-head">
        <h3>
          视频场景筛选
          {total > 0 && <span className="tag-filter-count">{total} 个条件</span>}
        </h3>
        <div className="tag-filter-actions">
          {total > 0 && (
            <button type="button" className="link-button" onClick={clear}>
              清空
            </button>
          )}
          {onToggleCollapse && (
            <button type="button" className="link-button" onClick={onToggleCollapse}>
              {collapsed ? '展开' : '收起'}
            </button>
          )}
        </div>
      </header>
      {!collapsed && (
        <>
          <p className="tag-filter-hint">
            {allowExclude
              ? '点一下「包含」，再点一下「排除」，第三下取消；同类任一命中即可，跨类需同时满足。'
              : '点一下选中、再点一下取消；同类任一命中即可，跨类需同时满足。'}
          </p>
          <div className="tag-filter-body">
            {categories.map((category) => (
              <div className="tag-filter-row" key={category}>
                <span className="tag-filter-category">{category}</span>
                <div className="tag-filter-chips">
                  {(values[category] || EMPTY).map((tag) => {
                    const isInclude = (value.filters[category] || EMPTY).includes(tag);
                    const isExclude = (value.excludes[category] || EMPTY).includes(tag);
                    return (
                      <button
                        type="button"
                        key={tag}
                        className={`tag-filter-chip${isInclude ? ' is-include' : ''}${isExclude ? ' is-exclude' : ''}`}
                        onClick={() => cycle(category, tag)}
                      >
                        {tag}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
