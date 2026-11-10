/**
 * 标签筛选口径：同类 OR、跨类 AND、排除优先。
 * 这三条如果错了，用户会「筛着筛着就没了场景」，所以单独锁住。
 */
import { describe, expect, it } from 'vitest';

import { buildTagIndex, filterTemplates } from './tag-filter';
import type { TemplateItem } from './types';

const values = {
  景别: ['全景', '中景', '特写'],
  姿势: ['站姿', '坐姿'],
  场景: ['室内', '室外'],
};

const index = buildTagIndex(values);

function tpl(id: string, tags: string[]): TemplateItem {
  return {
    id,
    name: `${id}.mp4`,
    shot: '',
    duration: 7,
    size_mb: 8,
    tags,
    tag_categories: {},
    ready: true,
    upload_state: '待上传',
  };
}

const items = [
  tpl('001', ['全景', '站姿', '室内']),
  tpl('002', ['中景', '坐姿', '室外']),
  tpl('003', ['中景', '站姿', '室外']),
];

describe('filterTemplates', () => {
  it('没有任何条件时原样返回', () => {
    expect(filterTemplates(items, {}, {}, index)).toHaveLength(3);
  });

  it('同一类别多个标签是「任一命中」', () => {
    const result = filterTemplates(items, { 景别: ['全景', '中景'] }, {}, index);
    expect(result.map((item) => item.id)).toEqual(['001', '002', '003']);
  });

  it('不同类别是「同时满足」', () => {
    const result = filterTemplates(items, { 景别: ['中景'], 姿势: ['站姿'] }, {}, index);
    expect(result.map((item) => item.id)).toEqual(['003']);
  });

  it('排除优先于包含', () => {
    const result = filterTemplates(items, { 景别: ['中景'] }, { 场景: ['室外'] }, index);
    expect(result.map((item) => item.id)).toEqual([]);
  });

  it('只排除时也能用', () => {
    const result = filterTemplates(items, {}, { 姿势: ['站姿'] }, index);
    expect(result.map((item) => item.id)).toEqual(['002']);
  });
});
