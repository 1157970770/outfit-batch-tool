import { pbkdf2, randomBytes, timingSafeEqual } from 'node:crypto';
import { promisify } from 'node:util';

import { PBKDF2_ROUNDS } from '@outfit/shared';

const pbkdf2Async = promisify(pbkdf2) as (
  password: Buffer,
  salt: Buffer,
  iterations: number,
  keylen: number,
  digest: string,
) => Promise<Buffer>;

export interface PasswordHash {
  salt: string;
  hash: string;
  rounds: number;
}

/** Python `_b64`：base64url，去尾部 `=`。 */
export function b64url(data: Buffer | string): string {
  const buf = typeof data === 'string' ? Buffer.from(data, 'utf8') : data;
  return buf.toString('base64url');
}

/** Python `_unb64`：补回 padding 再解。 */
export function unb64url(text: string): Buffer {
  const padded = text + '='.repeat((4 - (text.length % 4)) % 4);
  return Buffer.from(padded, 'base64url');
}

/**
 * 与 `auth.make_password` 完全一致：PBKDF2-SHA256 / 310000 轮 / 16 字节盐。
 * 必须走异步版：310k 轮同步计算会卡住整个事件循环（调度线程也在里面）。
 */
export async function makePassword(password: string): Promise<PasswordHash> {
  const salt = randomBytes(16);
  const digest = await pbkdf2Async(Buffer.from(password, 'utf8'), salt, PBKDF2_ROUNDS, 32, 'sha256');
  return { salt: b64url(salt), hash: b64url(digest), rounds: PBKDF2_ROUNDS };
}

/** 与 `auth.verify_password` 一致；任何字段异常都返回 false，不抛。 */
export async function verifyPassword(
  password: string,
  record: { salt?: string; hash?: string; rounds?: number } | null | undefined,
): Promise<boolean> {
  if (!record || typeof record.salt !== 'string' || typeof record.hash !== 'string') return false;
  try {
    const expected = unb64url(record.hash);
    const salt = unb64url(record.salt);
    const rounds = Number(record.rounds ?? PBKDF2_ROUNDS);
    if (!Number.isFinite(rounds) || rounds <= 0) return false;
    const actual = await pbkdf2Async(Buffer.from(password, 'utf8'), salt, rounds, expected.length || 32, 'sha256');
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  } catch {
    return false;
  }
}

/** 未知账号也跑一次 PBKDF2，抹平账号枚举的时序差异（与 Python 版同口径）。 */
export async function burnPasswordTime(password: string): Promise<void> {
  await pbkdf2Async(Buffer.from(password, 'utf8'), Buffer.from('unknown-user-salt'), PBKDF2_ROUNDS, 32, 'sha256');
}
