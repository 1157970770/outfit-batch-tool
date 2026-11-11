import { describe, expect, it } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { CosClient, encodeKeyPath, pathSegment, slug } from '../storage/cos-client';

const cfg = {
  enabled: true,
  bucket: 'demo-1250000000',
  region: 'ap-guangzhou',
  secretId: 'AKIDEXAMPLE',
  secretKey: 'secret-example',
};

describe('COS Key 规则（与 Python app/storage.py 同口径）', () => {
  it('pathSegment 保留中文，只替换禁用字符', () => {
    expect(pathSegment('第 7 套/服装')).toBe('第 7 套-服装');
    expect(pathSegment('  A001  ')).toBe('A001');
  });

  it('slug 丢掉中文退化成默认值，保留 ascii', () => {
    expect(slug('换装 视频.mp4'.replace('.mp4', ''))).toBe('file');
    expect(slug('Batch-2026_11.10')).toBe('batch-2026_11.10');
  });

  it('encodeKeyPath 逐段编码、保留斜杠', () => {
    expect(encodeKeyPath('prod/output/2026-11-11/第 7 套/a.mp4')).toBe(
      '/prod/output/2026-11-11/%E7%AC%AC%207%20%E5%A5%97/a.mp4',
    );
  });
});

describe('COS v5 签名', () => {
  const client = new CosClient(cfg);

  it('预签名 GET 带齐 q-sign-* 参数且 host 正确', () => {
    const url = new URL(client.presignGet('prod/output/x.mp4', 3600));
    expect(url.host).toBe('demo-1250000000.cos.ap-guangzhou.myqcloud.com');
    expect(url.pathname).toBe('/prod/output/x.mp4');
    for (const name of ['q-sign-algorithm', 'q-ak', 'q-sign-time', 'q-key-time', 'q-header-list', 'q-signature']) {
      expect(url.searchParams.get(name)).toBeTruthy();
    }
    expect(url.searchParams.get('q-sign-algorithm')).toBe('sha1');
    expect(url.searchParams.get('q-ak')).toBe('AKIDEXAMPLE');
    expect(url.searchParams.get('q-signature')).toMatch(/^[0-9a-f]{40}$/);
  });

  it('同 key 不同过期时间签名不同（keyTime 参与计算）', () => {
    const a = new URL(client.presignGet('k.mp4', 60)).searchParams.get('q-signature');
    const b = new URL(client.presignGet('k.mp4', 7200)).searchParams.get('q-signature');
    expect(a).not.toBe(b);
  });

  it('未配齐密钥时 enabled=false，操作直接报错', async () => {
    const off = new CosClient({ ...cfg, secretKey: '' });
    expect(off.enabled).toBe(false);
    await expect(off.listPrefix('x')).rejects.toThrow(/未启用/);
  });
});

describe('文件指纹', () => {
  it('同内容稳定、不同内容不同', async () => {
    const { StorageService } = await import('../storage/storage.service');
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-fp-'));
    const a = path.join(dir, 'a.mp4');
    const b = path.join(dir, 'b.mp4');
    fs.writeFileSync(a, Buffer.alloc(4096, 7));
    fs.writeFileSync(b, Buffer.alloc(4096, 8));
    const service = Object.create(StorageService.prototype) as InstanceType<typeof StorageService>;
    const first = service.fingerprint(a);
    expect(first).toMatch(/^[0-9a-f]{16}$/);
    expect(service.fingerprint(a)).toBe(first);
    expect(service.fingerprint(b)).not.toBe(first);
  });
});
