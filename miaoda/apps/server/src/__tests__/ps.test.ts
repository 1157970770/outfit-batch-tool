import { describe, expect, it } from 'vitest';

import { depurpleRgb, parseStrength, rgbToLab } from '../ps/depurple';

/** 造一张「淡紫背景 + 暗部块 + 暖色块」的图，和 Python 对拍用的构图一致。 */
function sampleImage(width = 96, height = 72): Uint8Array {
  const data = new Uint8Array(width * height * 3);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const i = (y * width + x) * 3;
      let rgb: [number, number, number];
      const inDark = x > 8 && x < 40 && y > 8 && y < height - 8;
      const inWarm = x > 56 && x < 88 && y > 40 && y < height - 8;
      if (inDark) rgb = [30, 26, 38];
      else if (inWarm) rgb = [214, 168, 140];
      else rgb = [238, 230, 246];
      data[i] = rgb[0];
      data[i + 1] = rgb[1];
      data[i + 2] = rgb[2];
    }
  }
  return data;
}

describe('parseStrength', () => {
  it('缺省给 100', () => {
    expect(parseStrength(undefined)).toBe(100);
    expect(parseStrength('')).toBe(100);
  });

  it('接受 0–100 的整数（含字符串）', () => {
    expect(parseStrength(0)).toBe(0);
    expect(parseStrength('45')).toBe(45);
    expect(parseStrength(100)).toBe(100);
  });

  it('拒绝布尔、小数、越界、非数字', () => {
    expect(() => parseStrength(true)).toThrow('强度必须是 0–100 的整数。');
    expect(() => parseStrength(12.5)).toThrow('强度必须是 0–100 的整数。');
    expect(() => parseStrength('abc')).toThrow('强度必须是 0–100 的整数。');
    expect(() => parseStrength(101)).toThrow('强度必须在 0–100 之间。');
    expect(() => parseStrength(-1)).toThrow('强度必须在 0–100 之间。');
  });
});

describe('depurpleRgb', () => {
  it('strength=0 时像素基本不变（Lab 往返有 ±2 的取整误差，与 Python 一致）', () => {
    const src = sampleImage();
    const { rgb, stats } = depurpleRgb(src, 96, 72, 0);
    expect(stats.strength).toBe(0);
    let maxDiff = 0;
    for (let i = 0; i < src.length; i += 1) {
      maxDiff = Math.max(maxDiff, Math.abs(rgb[i] - src[i]));
    }
    expect(maxDiff).toBeLessThanOrEqual(2);
  });

  it('去紫后色偏明显下降，且只动 A/B（亮度基本不变）', () => {
    const src = sampleImage();
    const { stats } = depurpleRgb(src, 96, 72, 100);
    expect(stats.cast_before).toBeGreaterThan(0);
    expect(stats.cast_after).toBeLessThan(stats.cast_before);
    expect(stats.width).toBe(96);
    expect(stats.height).toBe(72);
    // 锚点是「暗部偏紫、背景偏紫」：A 为正、B 为负
    expect(stats.anchors.dark[0]).toBeGreaterThan(0);
    expect(stats.anchors.bg[1]).toBeLessThan(0);
  });

  it('同一输入两次结果完全一致（无随机性）', () => {
    const src = sampleImage();
    const first = depurpleRgb(src, 96, 72, 60);
    const second = depurpleRgb(src, 96, 72, 60);
    expect(Buffer.from(first.rgb).equals(Buffer.from(second.rgb))).toBe(true);
  });

  it('rgbToLab 对纯白给出的 L 接近 100', () => {
    const white = new Uint8Array([255, 255, 255, 255, 255, 255]);
    const { L } = rgbToLab(white, 2);
    expect(L[0]).toBeGreaterThan(99);
    expect(L[0]).toBeLessThan(100.5);
  });
});
