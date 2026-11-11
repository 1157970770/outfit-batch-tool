/**
 * 数据目录级单实例锁（对应 Python `app/web/server.py::_acquire_instance_lock`）。
 *
 * 为什么需要：Windows 上 `SO_REUSEADDR` 允许两个进程同时 bind 同一端口，
 * 后起的实例会抢到带 Cookie 的请求，任务/批次文件也会被两边同时写。
 *
 * 实现：在 `<dataDir>/server.lock.d` 上做原子 `mkdir`。目录里放 `owner.json`
 * （pid + 启动时间），每 15s 写一次心跳；拿不到锁时先判断持有者是否还活着，
 * 进程已退（或心跳超过 90s）就当陈旧锁清掉再抢一次。
 *
 * **与 Python 版不互通**：Python 用 `msvcrt`/`flock` 锁 `server.lock` 文件，
 * 这里用目录锁，两者互不可见——同一个数据目录不要混跑两种实现（部署文档已写明）。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

export interface InstanceLock {
  readonly dir: string;
  release(): void;
}

interface Owner {
  pid: number;
  started_at: number;
  heartbeat: number;
}

const HEARTBEAT_MS = 15_000;
const STALE_MS = 90_000;

function alive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    // 0 号信号只探活，不真的发信号；EPERM 说明进程存在但属别的用户。
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === 'EPERM';
  }
}

function readOwner(dir: string): Owner | null {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, 'owner.json'), 'utf8')) as Owner;
  } catch {
    return null;
  }
}

export function acquireInstanceLock(dataDir: string): InstanceLock | null {
  const dir = path.join(dataDir, 'server.lock.d');
  const owner: Owner = { pid: process.pid, started_at: Date.now(), heartbeat: Date.now() };

  const take = (): boolean => {
    try {
      fs.mkdirSync(dir);
      return true;
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'EEXIST') throw err;
      return false;
    }
  };

  if (!take()) {
    const held = readOwner(dir);
    const staleByPid = !held || !alive(held.pid);
    const staleByTime = Boolean(held) && Date.now() - Number(held!.heartbeat || 0) > STALE_MS;
    if (!staleByPid && !staleByTime) return null;
    // 陈旧锁：清掉再抢一次（并发下另一个进程也可能抢赢，赢了就返回 null）。
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {
      return null;
    }
    if (!take()) return null;
  }

  fs.writeFileSync(path.join(dir, 'owner.json'), JSON.stringify(owner), 'utf8');

  const timer = setInterval(() => {
    try {
      owner.heartbeat = Date.now();
      fs.writeFileSync(path.join(dir, 'owner.json'), JSON.stringify(owner), 'utf8');
    } catch {
      /* 心跳写不进去不影响主流程，下一轮再试 */
    }
  }, HEARTBEAT_MS);
  timer.unref?.();

  let released = false;
  return {
    dir,
    release(): void {
      if (released) return;
      released = true;
      clearInterval(timer);
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {
        /* 删不掉就留给下次的陈旧锁判定 */
      }
    },
  };
}
