/**
 * 去紫（Purple cast removal）：1:1 移植 `app/depurple.py`。
 *
 * 口径（与 PS 插件 / Python 版逐条一致）：
 *  1) 转 Lab，只动 A/B，L 完全不动；
 *  2) 两个锚点：暗部 `Lg < 60`、背景（低饱和像素最多的亮部区间，32 档直方图取 `Lg >= 120` 的峰）；
 *  3) 偏色量按 Lg 在「暗部 → 背景」之间线性插值，背景以上取背景值；
 *  4) 高光端 taper 收 25%（白料高光自带偏色小于背景，全量扣会发青）；
 *  5) 暖色保护：A>0 且 B>0（肤色/棕色/米色）只改 10%；
 *  6) 应用后 1/3 抽样量一次残留，`|A|` 或 `|B| > 1.0` 就把锚点推一次（自校准）。
 *
 * 与 Python 的唯一实现差异：这里用 `Float32Array` 手写循环（无 numpy），
 * 数值路径（矩阵、gamma、cbrt、clip、直方图、中位数）逐位对齐。
 */
export const DEFAULT_STRENGTH = 100;
export const MAX_STRENGTH = 100;

/** sRGB → XYZ（D65），与插件逐位一致；别换成别的矩阵。 */
const M: readonly number[][] = [
  [0.4124, 0.3576, 0.1805],
  [0.2126, 0.7152, 0.0722],
  [0.0193, 0.1192, 0.9505],
];
const XN = 0.95047;
const YN = 1.0;
const ZN = 1.08883;
const NEUTRAL_SAT = 30;
const MIN_BAND_PX = 300;
const RESIDUAL_LIMIT = 1.0;

export class DepurpleError extends Error {
  status = 500;
}

export class DepurpleBadImage extends DepurpleError {
  override status = 422;
}

/** 强度统一在这里校验：0–100 的整数，越界/非整数抛错（→ 400）。 */
export function parseStrength(value: unknown): number {
  if (value === null || value === undefined || value === '') return DEFAULT_STRENGTH;
  if (typeof value === 'boolean') throw new RangeError('强度必须是 0–100 的整数。');
  if (typeof value === 'number' && !Number.isInteger(value)) {
    throw new RangeError('强度必须是 0–100 的整数。');
  }
  const strength = Number(value);
  if (!Number.isFinite(strength) || !Number.isInteger(strength)) {
    throw new RangeError('强度必须是 0–100 的整数。');
  }
  if (strength < 0 || strength > MAX_STRENGTH) {
    throw new RangeError(`强度必须在 0–${MAX_STRENGTH} 之间。`);
  }
  return strength;
}

interface LabPlanes {
  L: Float32Array;
  A: Float32Array;
  B: Float32Array;
}

/** `(H,W,3)` 的 8 位 sRGB → L/A/B（float32）。 */
export function rgbToLab(rgb: Uint8Array | Uint8ClampedArray, pixels: number): LabPlanes {
  const L = new Float32Array(pixels);
  const A = new Float32Array(pixels);
  const B = new Float32Array(pixels);
  const srgbToLinear = (v: number): number => {
    const x = v / 255;
    return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
  };
  const f = (t: number): number =>
    t > 0.008856 ? Math.cbrt(Math.max(t, 1e-9)) : 7.787 * t + 16 / 116;

  for (let i = 0; i < pixels; i += 1) {
    const r = srgbToLinear(rgb[i * 3]);
    const g = srgbToLinear(rgb[i * 3 + 1]);
    const b = srgbToLinear(rgb[i * 3 + 2]);
    const X = M[0][0] * r + M[0][1] * g + M[0][2] * b;
    const Y = M[1][0] * r + M[1][1] * g + M[1][2] * b;
    const Z = M[2][0] * r + M[2][1] * g + M[2][2] * b;
    const fx = f(X / XN);
    const fy = f(Y / YN);
    const fz = f(Z / ZN);
    L[i] = 116 * fy - 16;
    A[i] = 500 * (fx - fy);
    B[i] = 200 * (fy - fz);
  }
  return { L, A, B };
}

/** L/A/B → 8 位 sRGB（写入 `out`，长度 = pixels*3）。 */
function labToRgbInto(
  L: Float32Array,
  A: Float32Array,
  B: Float32Array,
  pixels: number,
  out: Uint8Array,
): void {
  // 3×3 常量矩阵求逆（float32 精度足够：误差 < 0.01 个灰阶）
  const inv = invert3(M);
  const invF = (t: number): number => (t ** 3 > 0.008856 ? t ** 3 : (t - 16 / 116) / 7.787);

  for (let i = 0; i < pixels; i += 1) {
    const fy = (L[i] + 16) / 116;
    const fx = fy + A[i] / 500;
    const fz = fy - B[i] / 200;
    const X = invF(fx) * XN;
    const Y = invF(fy) * YN;
    const Z = invF(fz) * ZN;
    let lr = inv[0][0] * X + inv[0][1] * Y + inv[0][2] * Z;
    let lg = inv[1][0] * X + inv[1][1] * Y + inv[1][2] * Z;
    let lb = inv[2][0] * X + inv[2][1] * Y + inv[2][2] * Z;
    lr = lr < 0 ? 0 : lr > 1 ? 1 : lr;
    lg = lg < 0 ? 0 : lg > 1 ? 1 : lg;
    lb = lb < 0 ? 0 : lb > 1 ? 1 : lb;
    // numpy 的 `astype(uint8)` 是截断取整（不是四舍五入），这里必须一致，
    // 否则整幅图的平均亮度会系统性偏高约 0.7。
    const enc = (x: number): number => {
      const s = x <= 0.0031308 ? x * 12.92 : 1.055 * x ** (1 / 2.4) - 0.055;
      const v = s * 255;
      return v < 0 ? 0 : v > 255 ? 255 : Math.trunc(v);
    };
    out[i * 3] = Math.round(enc(lr));
    out[i * 3 + 1] = Math.round(enc(lg));
    out[i * 3 + 2] = Math.round(enc(lb));
  }
}

function invert3(m: readonly number[][]): number[][] {
  const [a, b, c] = m[0];
  const [d, e, f] = m[1];
  const [g, h, i] = m[2];
  const det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
  return [
    [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
    [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
    [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
  ];
}

function luma(r: number, g: number, b: number): number {
  return 0.299 * r + 0.587 * g + 0.114 * b;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = values.slice().sort((x, y) => x - y);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export interface DepurpleStats {
  width: number;
  height: number;
  strength: number;
  anchors: { dark: [number, number]; bg: [number, number] };
  cast_before: number;
  cast_after: number;
}

/** 对一段 `(H,W,3)` 的 8 位 RGB 做去紫，返回新 buffer + 统计。 */
export function depurpleRgb(
  rgb: Uint8Array | Uint8ClampedArray,
  width: number,
  height: number,
  strengthInput: unknown = DEFAULT_STRENGTH,
): { rgb: Uint8Array; stats: DepurpleStats } {
  const strength = parseStrength(strengthInput);
  const pixels = width * height;
  const { L, A, B } = rgbToLab(rgb, pixels);

  const Lg = new Float32Array(pixels);
  const neutral = new Uint8Array(pixels);
  for (let i = 0; i < pixels; i += 1) {
    const r = rgb[i * 3];
    const g = rgb[i * 3 + 1];
    const b = rgb[i * 3 + 2];
    Lg[i] = luma(r, g, b);
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    neutral[i] = max - min < NEUTRAL_SAT ? 1 : 0;
  }

  const castOf = (Ax: Float32Array, Bx: Float32Array): number => {
    let sum = 0;
    let count = 0;
    for (let i = 0; i < pixels; i += 1) {
      const c = Math.sqrt(Ax[i] * Ax[i] + Bx[i] * Bx[i]);
      const w = 1 - c / 60;
      if ((w < 0 ? 0 : w > 1 ? 1 : w) > 0.5) {
        sum += Math.abs(Bx[i] - Ax[i]);
        count += 1;
      }
    }
    return count ? sum / count : 0;
  };

  const castBefore = castOf(A, B);

  // 32 档直方图（0–256），只在低饱和像素上取峰
  const bins = 32;
  const hist = new Float64Array(bins);
  const edges: number[] = [];
  for (let k = 0; k <= bins; k += 1) edges.push((k * 256) / bins);
  for (let i = 0; i < pixels; i += 1) {
    if (!neutral[i]) continue;
    let bin = Math.floor(Lg[i] / 8);
    if (bin < 0) bin = 0;
    if (bin >= bins) bin = bins - 1;
    hist[bin] += 1;
  }
  let best = -1;
  let bestCount = -1;
  for (let k = 0; k < bins; k += 1) {
    const center = (edges[k] + edges[k + 1]) / 2;
    if (center < 120) continue;
    if (hist[k] > bestCount) {
      bestCount = hist[k];
      best = k;
    }
  }
  if (best < 0) {
    for (let k = 0; k < bins; k += 1) {
      if (hist[k] > bestCount) {
        bestCount = hist[k];
        best = k;
      }
    }
  }
  const loB = edges[best];
  const hiB = edges[best + 1];

  const warm = new Float32Array(pixels);
  const wApp = new Float32Array(pixels);
  for (let i = 0; i < pixels; i += 1) {
    const wa = clamp01((A[i] - 2) / 10);
    const wb = clamp01((B[i] + 2) / 10);
    warm[i] = wa * wb;
    wApp[i] = (1 - 0.9 * warm[i]) * (strength / 100);
  }

  const bandMed = (
    Ax: Float32Array,
    Bx: Float32Array,
    lumaValues: Float32Array,
    lo: number,
    hi: number,
    neutralMask: (i: number) => boolean,
  ): [number, number] | null => {
    const as: number[] = [];
    const bs: number[] = [];
    for (let i = 0; i < Ax.length; i += 1) {
      const value = lumaValues[i];
      if (value < lo || value >= hi) continue;
      if (!neutralMask(i)) continue;
      as.push(Ax[i]);
      bs.push(Bx[i]);
    }
    if (as.length < MIN_BAND_PX) return null;
    return [median(as), median(bs)];
  };

  const taper = new Float32Array(pixels);
  for (let i = 0; i < pixels; i += 1) taper[i] = 1 - 0.25 * clamp01((Lg[i] - 215) / 40);

  const out = new Uint8Array(pixels * 3);
  const off = (x: number, loV: number, hiV: number): number => {
    const t = clamp01((x - 50) / Math.max(1, hiB - 50));
    return loV + (hiV - loV) * t;
  };

  const apply = (
    dark: [number, number],
    bg: [number, number],
    outBuffer: Uint8Array,
  ): { A: Float32Array; B: Float32Array } => {
    const aNew = new Float32Array(pixels);
    const bNew = new Float32Array(pixels);
    for (let i = 0; i < pixels; i += 1) {
      const ca = off(Lg[i], dark[0], bg[0]) * taper[i];
      const cb = off(Lg[i], dark[1], bg[1]) * taper[i];
      aNew[i] = A[i] - ca * wApp[i];
      bNew[i] = B[i] - cb * wApp[i];
    }
    labToRgbInto(L, aNew, bNew, pixels, outBuffer);
    return { A: aNew, B: bNew };
  };

  const darkBand = bandMed(A, B, Lg, -Infinity, 60, (i) => neutral[i] === 1);
  const bgBand = bandMed(A, B, Lg, loB, hiB, (i) => neutral[i] === 1);
  const bg: [number, number] = bgBand ?? darkBand ?? [0, 0];
  const dark: [number, number] = darkBand ?? bg;

  let applied = apply(dark, bg, out);

  // 自校准：1/3 抽样看残留，明显才推一次锚点
  const samplePixels = Math.ceil(width / 3) * Math.ceil(height / 3);
  const small = new Uint8Array(samplePixels * 3);
  const sw = Math.ceil(width / 3);
  const sh = Math.ceil(height / 3);
  for (let y = 0; y < sh; y += 1) {
    for (let x = 0; x < sw; x += 1) {
      const src = (y * 3 * width + x * 3) * 3;
      const dst = (y * sw + x) * 3;
      small[dst] = out[src];
      small[dst + 1] = out[src + 1];
      small[dst + 2] = out[src + 2];
    }
  }
  const smallLab = rgbToLab(small, sw * sh);
  const smallLg = new Float32Array(sw * sh);
  const smallNeutral = new Uint8Array(sw * sh);
  for (let i = 0; i < sw * sh; i += 1) {
    const r = small[i * 3];
    const g = small[i * 3 + 1];
    const b = small[i * 3 + 2];
    smallLg[i] = luma(r, g, b);
    smallNeutral[i] = Math.max(r, g, b) - Math.min(r, g, b) < NEUTRAL_SAT ? 1 : 0;
  }
  const rb = bandMed(smallLab.A, smallLab.B, smallLg, loB, hiB, (i) => smallNeutral[i] === 1);
  const rd = bandMed(smallLab.A, smallLab.B, smallLg, -Infinity, 60, (i) => smallNeutral[i] === 1);
  if (rb && (Math.abs(rb[0]) > RESIDUAL_LIMIT || Math.abs(rb[1]) > RESIDUAL_LIMIT)) {
    const bg2: [number, number] = [bg[0] + rb[0], bg[1] + rb[1]];
    const dark2: [number, number] = [dark[0] + (rd ? rd[0] : 0), dark[1] + (rd ? rd[1] : 0)];
    applied = apply(dark2, bg2, out);
  }

  const castAfter = castOf(applied.A, applied.B);

  return {
    rgb: out,
    stats: {
      width,
      height,
      strength,
      anchors: {
        dark: [round2(dark[0]), round2(dark[1])],
        bg: [round2(bg[0]), round2(bg[1])],
      },
      cast_before: round2(castBefore),
      cast_after: round2(castAfter),
    },
  };
}

function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}
