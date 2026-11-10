/**
 * 标签筛选（前端复刻 `TemplateCatalog.filterItems` 的口径）：
 * 同一类别里任一标签命中即可，不同类别必须同时满足，排除优先于包含。
 *
 * 原生版也是拿一次全量 `/api/templates` 在浏览器里筛（`state.filteredTemplates`），
 * 服务端 `/api/templates` 不接筛选参数——这里保持一致，避免两边逻辑分叉。
 */
import type { TemplateItem } from './types';

export type TagIndex = Record<string, string>;

/** 标签 → 所属类别。一个标签只属于一个类别，所以可以扁平查找。 */
export function buildTagIndex(tagValues: Record<string, string[]>): TagIndex {
  const index: TagIndex = {};
  for (const [category, tags] of Object.entries(tagValues)) {
    for (const tag of tags) index[tag] = category;
  }
  return index;
}

export function filterTemplates(
  items: TemplateItem[],
  filters: Record<string, string[]>,
  excludes: Record<string, string[]>,
  index: TagIndex,
): TemplateItem[] {
  const includeGroups = Object.entries(filters).filter(([, tags]) => tags.length > 0);
  const excludeTags = new Set(Object.values(excludes).flat());
  if (!includeGroups.length && !excludeTags.size) return items;

  return items.filter((item) => {
    const own = new Set(item.tags || []);
    for (const tag of own) if (excludeTags.has(tag)) return false;
    for (const [category, tags] of includeGroups) {
      // 只有属于该类别的标签参与匹配；用户选的标签如果不在候选集里，也不该误伤。
      const wanted = tags.filter((tag) => (index[tag] || category) === category);
      if (!wanted.length) continue;
      if (!wanted.some((tag) => own.has(tag))) return false;
    }
    return true;
  });
}
