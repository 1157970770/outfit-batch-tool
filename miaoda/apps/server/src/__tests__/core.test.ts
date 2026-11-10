/**
 * P1 纯逻辑回归：这些口径错一点，用户就会「登录不上 / 图片传不上 / 数据找不到」。
 * 全部离线：只有 ZIP 会写系统临时目录。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterAll, describe, expect, it } from 'vitest';

import { b64url, makePassword, verifyPassword } from '../auth/password';
import { SessionSigner } from '../auth/session.signer';
import { LoginLimiter } from '../auth/login-limiter';
import { relocate } from '../common/paths';
import { inspectImageBuffer, validateImageBuffer } from '../media/image-info';
import { zipFiles } from '../jobs/zip';

describe('password', () => {
  it('同一个密码能校验通过，错误密码不通过', async () => {
    const record = await makePassword('pw-parity-123');
    expect(record.rounds).toBe(310_000);
    expect(record.hash).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(await verifyPassword('pw-parity-123', record)).toBe(true);
    expect(await verifyPassword('pw-parity-124', record)).toBe(false);
  });

  it('同样的明文两次加盐结果不同', async () => {
    const a = await makePassword('same-password');
    const b = await makePassword('same-password');
    expect(a.salt).not.toBe(b.salt);
    expect(a.hash).not.toBe(b.hash);
  });
});

describe('SessionSigner', () => {
  const signer = new SessionSigner(b64url(Buffer.from('test-secret')));
  const users = { tester: { role: 'admin', version: 1, enabled: true } };

  it('签发后能验回来，且带用户名与角色', () => {
    const token = signer.issue('tester', { role: 'admin', version: 1 });
    const payload = signer.verify(token, users);
    expect(payload).not.toBeNull();
    expect(payload?.u).toBe('tester');
    expect(payload?.r).toBe('admin');
  });

  it('改一个字符就验不过（签名覆盖整个 payload）', () => {
    const token = signer.issue('tester', { role: 'admin', version: 1 });
    const [body, sig] = token.split('.');
    // 改中间的字符：base64url 末位可能有未用位，改末位不一定改到字节
    const at = Math.floor(sig.length / 2);
    const flipped = `${sig.slice(0, at)}${sig[at] === 'A' ? 'B' : 'A'}${sig.slice(at + 1)}`;
    expect(signer.verify(`${body}.${flipped}`, users)).toBeNull();
  });

  it('换密钥、禁用账号、版本不符都验不过', () => {
    const token = signer.issue('tester', { role: 'admin', version: 1 });
    const other = new SessionSigner(b64url(Buffer.from('another-secret')));
    expect(other.verify(token, users)).toBeNull();
    expect(signer.verify(token, { tester: { role: 'admin', version: 1, enabled: false } })).toBeNull();
    expect(signer.verify(token, { tester: { role: 'admin', version: 2, enabled: true } })).toBeNull();
    expect(signer.verify(token, {})).toBeNull();
  });
});

describe('LoginLimiter', () => {
  it('连续失败到上限后拒绝，成功后清零', () => {
    const limiter = new LoginLimiter(3, 600, 100);
    expect(limiter.allowed('user|a')).toBe(true);
    limiter.failed('user|a');
    limiter.failed('user|a');
    expect(limiter.allowed('user|a')).toBe(true);
    limiter.failed('user|a');
    expect(limiter.allowed('user|a')).toBe(false);
    limiter.succeeded('user|a');
    expect(limiter.allowed('user|a')).toBe(true);
  });

  it('不同 key 互不影响（IP 桶与账号桶分开）', () => {
    const limiter = new LoginLimiter(2, 600, 100);
    limiter.failed('ip|1.2.3.4');
    limiter.failed('ip|1.2.3.4');
    expect(limiter.allowed('ip|1.2.3.4')).toBe(false);
    expect(limiter.allowed('user|a')).toBe(true);
  });
});

describe('relocate', () => {
  const dataDir = 'D:\\data\\web-data-dev';

  it('按 web-data 段重锚到当前数据目录', () => {
    const raw = 'D:\\old\\web-data-dev\\output\\WEB-x\\out-001.mp4';
    expect(relocate(raw, dataDir, false)).toBe('D:\\data\\web-data-dev\\output\\WEB-x\\out-001.mp4');
  });

  it('已经在当前目录下的原样返回', () => {
    const raw = 'D:\\data\\web-data-dev\\uploads\\ab\\x.png';
    expect(relocate(raw, dataDir, false)).toBe(raw);
  });

  it('没有 web-data 段时原样返回', () => {
    const raw = 'C:\\temp\\our-fallback.mp4';
    expect(relocate(raw, dataDir, false)).toBe(raw);
  });
});

/** 造一个最小合法 PNG（只放 IHDR，够解析器读宽高）。 */
function pngBuffer(width: number, height: number): Buffer {
  const buf = Buffer.alloc(33);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(buf, 0);
  buf.writeUInt32BE(13, 8);
  buf.write('IHDR', 12, 'latin1');
  buf.writeUInt32BE(width, 16);
  buf.writeUInt32BE(height, 20);
  buf[24] = 8;
  buf[25] = 6;
  return buf;
}

describe('image-info', () => {
  it('读出 PNG 宽高', () => {
    const info = inspectImageBuffer(pngBuffer(800, 1000));
    expect(info?.format).toBe('PNG');
    expect(info?.width).toBe(800);
    expect(info?.height).toBe(1000);
  });

  it('尺寸合法时返回元信息', () => {
    const info = validateImageBuffer(pngBuffer(800, 1000), 'a.png');
    expect(info.width).toBe(800);
    expect(info.format).toBe('PNG');
  });

  it('太小、太长、非图片都被拒绝', () => {
    expect(() => validateImageBuffer(pngBuffer(100, 100), 'a.png')).toThrow(/单边需在 240–8000px/);
    expect(() => validateImageBuffer(pngBuffer(3000, 300), 'a.png')).toThrow(/宽高比 .*超过 8:1 上限/);
    expect(() => validateImageBuffer(Buffer.from('not an image'), 'a.png')).toThrow(/格式无法识别/);
  });
});

describe('zip', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-zip-'));

  afterAll(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it('打出来的包能按文件名找回内容', () => {
    fs.writeFileSync(path.join(dir, '001.mp4'), 'video-bytes');
    fs.writeFileSync(path.join(dir, 'report.csv'), '模板编号,状态\n001,success\n');
    const zip = zipFiles([
      { name: '001.mp4', file: path.join(dir, '001.mp4') },
      { name: 'report.csv', file: path.join(dir, 'report.csv') },
    ]);
    // 本地文件头签名 + 中央目录里能看到两个文件名
    expect(zip.readUInt32LE(0)).toBe(0x04034b50);
    const text = zip.toString('latin1');
    expect(text).toContain('001.mp4');
    expect(text).toContain('report.csv');
    expect(text).toContain('video-bytes');
  });

  it('空列表也返回合法空包', () => {
    const zip = zipFiles([]);
    expect(zip.length).toBeGreaterThanOrEqual(22);
  });
});
