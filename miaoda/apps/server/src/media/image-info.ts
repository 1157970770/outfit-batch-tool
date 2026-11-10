/**
 * 图片元信息（不依赖 Pillow）：格式、宽高、EXIF 方向。
 *
 * 与 Python 版 `app/media.py::inspect_image` 的口径对齐：
 * - 宽高按 EXIF 转正后的方向返回（竖拍图 orientation=6 时交换宽高）；
 * - 只认 JPEG / PNG / BMP / WEBP，其余返回 format=''，由调用方报「格式不支持」。
 */
export interface ImageInfo {
  format: string;
  width: number;
  height: number;
  sizeMb: number;
  orientation: number;
  hasAlpha: boolean;
  bytes: number;
}

export const IMAGE_FORMATS = ['JPEG', 'PNG', 'BMP', 'WEBP'];
export const MAX_IMAGE_MB = 20;
export const MAX_IMAGE_PIXELS = 64_000_000;
export const MIN_SIDE = 240;
export const MAX_SIDE = 8000;
export const MAX_RATIO = 8;

function readJpeg(buf: Buffer): { width: number; height: number; orientation: number } | null {
  let offset = 2;
  let orientation = 1;
  while (offset + 4 <= buf.length) {
    if (buf[offset] !== 0xff) {
      offset += 1;
      continue;
    }
    const marker = buf[offset + 1];
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) {
      offset += 2;
      continue;
    }
    const length = buf.readUInt16BE(offset + 2);
    if (length < 2) return null;
    if (marker === 0xe1 && buf.toString('latin1', offset + 4, offset + 8) === 'Exif') {
      orientation = readExifOrientation(buf, offset + 10, length - 8) || 1;
    }
    // SOF0..SOF15，排除 DHT(C4)/JPG(C8)/DAC(CC)
    if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
      return { height: buf.readUInt16BE(offset + 5), width: buf.readUInt16BE(offset + 7), orientation };
    }
    offset += 2 + length;
  }
  return null;
}

function readExifOrientation(buf: Buffer, start: number, size: number): number {
  if (start + 8 > buf.length) return 0;
  const le = buf.toString('latin1', start, start + 2) === 'II';
  const u16 = (at: number) => (le ? buf.readUInt16LE(at) : buf.readUInt16BE(at));
  const u32 = (at: number) => (le ? buf.readUInt32LE(at) : buf.readUInt32BE(at));
  if (u16(start + 2) !== 0x002a) return 0;
  const ifd = start + u32(start + 4);
  if (ifd + 2 > buf.length) return 0;
  const count = u16(ifd);
  for (let i = 0; i < count; i += 1) {
    const entry = ifd + 2 + i * 12;
    if (entry + 12 > buf.length) return 0;
    if (u16(entry) === 0x0112) return u16(entry + 8);
  }
  return 0;
}

export function inspectImageBuffer(buf: Buffer): ImageInfo | null {
  const base: ImageInfo = {
    format: '',
    width: 0,
    height: 0,
    sizeMb: Math.round((buf.length / 1024 / 1024) * 1000) / 1000,
    orientation: 1,
    hasAlpha: false,
    bytes: buf.length,
  };
  if (buf.length >= 8 && buf.readUInt32BE(0) === 0x89504e47) {
    if (buf.length < 26) return null;
    const width = buf.readUInt32BE(16);
    const height = buf.readUInt32BE(20);
    const colorType = buf[25];
    return { ...base, format: 'PNG', width, height, hasAlpha: colorType === 4 || colorType === 6 };
  }
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8) {
    const jpeg = readJpeg(buf);
    if (!jpeg) return null;
    const swap = jpeg.orientation >= 5 && jpeg.orientation <= 8;
    return {
      ...base,
      format: 'JPEG',
      width: swap ? jpeg.height : jpeg.width,
      height: swap ? jpeg.width : jpeg.height,
      orientation: jpeg.orientation,
    };
  }
  if (buf.length >= 12 && buf.toString('latin1', 0, 4) === 'RIFF' && buf.toString('latin1', 8, 12) === 'WEBP') {
    const kind = buf.toString('latin1', 12, 16);
    if (kind === 'VP8 ' && buf.length >= 30) {
      return { ...base, format: 'WEBP', width: buf.readUInt16LE(26) & 0x3fff, height: buf.readUInt16LE(28) & 0x3fff };
    }
    if (kind === 'VP8L' && buf.length >= 25) {
      const bits = buf.readUInt32LE(21);
      return { ...base, format: 'WEBP', width: (bits & 0x3fff) + 1, height: ((bits >> 14) & 0x3fff) + 1, hasAlpha: true };
    }
    if (kind === 'VP8X' && buf.length >= 30) {
      return {
        ...base,
        format: 'WEBP',
        width: buf.readUIntLE(24, 3) + 1,
        height: buf.readUIntLE(27, 3) + 1,
        hasAlpha: (buf[20] & 0x10) !== 0,
      };
    }
    return null;
  }
  if (buf.length >= 26 && buf[0] === 0x42 && buf[1] === 0x4d) {
    return { ...base, format: 'BMP', width: buf.readInt32LE(18), height: Math.abs(buf.readInt32LE(22)) };
  }
  return null;
}

/** 按官方图片限制校验；不合规抛 Error（消息直接用给用户看）。 */
export function validateImageBuffer(buf: Buffer, name: string): ImageInfo {
  const info = inspectImageBuffer(buf);
  if (!info || !info.format) throw new Error(`图片打不开：${name}（格式无法识别）`);
  if (info.sizeMb > MAX_IMAGE_MB) {
    throw new Error(`图片 ${name} 有 ${info.sizeMb.toFixed(1)}MB，超过官方 20MB 上限`);
  }
  if (!IMAGE_FORMATS.includes(info.format)) {
    throw new Error(`图片 ${name} 格式 ${info.format} 不支持（只收 JPEG/PNG/BMP/WEBP）`);
  }
  const { width, height } = info;
  if (width && height) {
    if (width * height > MAX_IMAGE_PIXELS) {
      throw new Error(`图片 ${name} 像素总量超过 ${MAX_IMAGE_PIXELS / 1_000_000}MP 解压上限，已拒绝处理`);
    }
    if (!(width >= MIN_SIDE && width <= MAX_SIDE && height >= MIN_SIDE && height <= MAX_SIDE)) {
      throw new Error(`图片 ${name} 尺寸 ${width}×${height}，单边需在 240–8000px`);
    }
    const ratio = Math.max(width / height, height / width);
    if (ratio > MAX_RATIO) throw new Error(`图片 ${name} 宽高比 ${ratio.toFixed(1)}:1，超过 8:1 上限`);
  }
  return info;
}

/** 按文件头判断真实扩展名（Python 侧用于把 .jpg 纠正成 .png 这类错名）。 */
export function sniffExtension(buf: Buffer): string {
  const info = inspectImageBuffer(buf);
  if (!info || !info.format) return '';
  return { JPEG: '.jpg', PNG: '.png', BMP: '.bmp', WEBP: '.webp' }[info.format] ?? '';
}
