import { describe, expect, it } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { acquireInstanceLock } from '../common/instance-lock';

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'outfit-lock-'));
}

describe('数据目录单实例锁', () => {
  it('第一次能拿到，第二次拿不到', () => {
    const dir = tmpDir();
    const first = acquireInstanceLock(dir);
    expect(first).not.toBeNull();
    expect(fs.existsSync(path.join(dir, 'server.lock.d', 'owner.json'))).toBe(true);
    expect(acquireInstanceLock(dir)).toBeNull();
    first!.release();
  });

  it('释放后可以再次获取，目录被清掉', () => {
    const dir = tmpDir();
    const first = acquireInstanceLock(dir)!;
    first.release();
    expect(fs.existsSync(path.join(dir, 'server.lock.d'))).toBe(false);
    const second = acquireInstanceLock(dir);
    expect(second).not.toBeNull();
    second!.release();
  });

  it('重复 release 不报错', () => {
    const dir = tmpDir();
    const lock = acquireInstanceLock(dir)!;
    lock.release();
    expect(() => lock.release()).not.toThrow();
  });

  it('持有者进程已不存在时按陈旧锁接管', () => {
    const dir = tmpDir();
    const lockDir = path.join(dir, 'server.lock.d');
    fs.mkdirSync(lockDir, { recursive: true });
    // 999999 在本机不可能存在；心跳也写得很旧。
    fs.writeFileSync(
      path.join(lockDir, 'owner.json'),
      JSON.stringify({ pid: 999999, started_at: 0, heartbeat: 1 }),
      'utf8',
    );
    const lock = acquireInstanceLock(dir);
    expect(lock).not.toBeNull();
    const owner = JSON.parse(fs.readFileSync(path.join(lockDir, 'owner.json'), 'utf8'));
    expect(owner.pid).toBe(process.pid);
    lock!.release();
  });

  it('owner.json 坏掉也能接管（读不出来就当陈旧）', () => {
    const dir = tmpDir();
    const lockDir = path.join(dir, 'server.lock.d');
    fs.mkdirSync(lockDir, { recursive: true });
    fs.writeFileSync(path.join(lockDir, 'owner.json'), 'not json', 'utf8');
    const lock = acquireInstanceLock(dir);
    expect(lock).not.toBeNull();
    lock!.release();
  });
});
