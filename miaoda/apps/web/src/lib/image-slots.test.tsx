/**
 * 图片槽位的顺序语义——这是用户明确要求「保留」的交互，必须有测试兜住：
 * 追加从第一个空槽开始、删除后后面的图左移、最多 10 张、校验文案与原生版一致。
 */
import { describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import { SLOT_MAX, useImageSlots, validateImageFile } from './image-slots';

function png(name: string, bytes = 1024): File {
  return new File([new Uint8Array(bytes)], name, { type: 'image/png' });
}

describe('validateImageFile', () => {
  it('接受 jpg / png / webp / bmp', () => {
    expect(validateImageFile(png('a.png'))).toBe('');
    expect(validateImageFile(png('b.JPG'))).toBe('');
    expect(validateImageFile(png('c.webp'))).toBe('');
    expect(validateImageFile(png('d.bmp'))).toBe('');
  });

  it('HEIC 给出可照做的转换提示', () => {
    const message = validateImageFile(png('IMG_0001.HEIC'));
    expect(message).toContain('HEIC');
    expect(message).toContain('JPG');
  });

  it('别的格式与超过 20MB 都拒绝', () => {
    expect(validateImageFile(png('a.gif'))).toContain('格式不支持');
    const big = new File([new Uint8Array(21 * 1024 * 1024)], 'big.png', { type: 'image/png' });
    expect(validateImageFile(big)).toContain('超过单张 20MB 上限');
  });
});

describe('useImageSlots', () => {
  it('追加时按顺序填槽，删除后序号左移', () => {
    const { result } = renderHook(() => useImageSlots());

    act(() => {
      result.current.appendFiles([png('front.png'), png('detail-1.png'), png('detail-2.png')]);
    });
    expect(result.current.slots.map((slot) => slot.name)).toEqual(['front.png', 'detail-1.png', 'detail-2.png']);

    act(() => result.current.remove(2));
    expect(result.current.slots.map((slot) => slot.name)).toEqual(['front.png', 'detail-2.png']);
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it(`最多 ${SLOT_MAX} 张，超出部分丢弃并提示`, () => {
    const { result } = renderHook(() => useImageSlots());
    const many = Array.from({ length: SLOT_MAX + 2 }, (_, index) => png(`img-${index}.png`));

    let appended: { added: number; errors: string[] } | undefined;
    act(() => {
      appended = result.current.appendFiles(many);
    });

    expect(result.current.slots).toHaveLength(SLOT_MAX);
    expect(appended?.added).toBe(SLOT_MAX);
    expect(appended?.errors.join()).toContain(`最多 ${SLOT_MAX} 张`);
  });

  it('原位替换不改变其它槽位顺序', () => {
    const { result } = renderHook(() => useImageSlots());
    act(() => result.current.appendFiles([png('a.png'), png('b.png')]));
    act(() => result.current.appendFiles([png('b2.png')], 2));
    expect(result.current.slots.map((slot) => slot.name)).toEqual(['a.png', 'b2.png']);
  });

  it('reset 清空并释放缩略图', () => {
    const { result } = renderHook(() => useImageSlots());
    act(() => result.current.appendFiles([png('a.png')]));
    act(() => result.current.reset());
    expect(result.current.slots).toHaveLength(0);
  });
});
