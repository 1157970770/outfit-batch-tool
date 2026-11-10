import { HttpException, HttpStatus, Injectable, Logger } from '@nestjs/common';

import { AppConfigService, UserRecord } from '../config/app-config.service';
import { AuthUser } from './auth.types';
import { AuthedUser } from './decorators';
import { LoginLimiter } from './login-limiter';
import { burnPasswordTime, verifyPassword } from './password';
import { SessionSigner, todayString } from './session.signer';

/** 统一错误体：与 Python `_error()` 一样是 `{ok:false,error}`。 */
export function apiError(message: string, status: number = HttpStatus.BAD_REQUEST): HttpException {
  return new HttpException({ ok: false, error: String(message).slice(0, 600) }, status);
}

const LOGIN_IP_LIMIT = 30;
/** 与 Python `_LOGIN_SLOTS = BoundedSemaphore(4)` 等价：同时最多 4 个登录在算哈希。 */
const LOGIN_SLOTS = 4;

@Injectable()
export class AuthService {
  private readonly logger = new Logger('Auth');
  readonly signer: SessionSigner;
  readonly limiter = new LoginLimiter(8, 600, 2000);
  private busyLogins = 0;

  constructor(private readonly config: AppConfigService) {
    this.signer = new SessionSigner(config.sessionSecret, config.sessionHours);
  }

  get users() {
    return this.config.users;
  }

  findUser(username: string): AuthUser | undefined {
    const record = this.users[username];
    if (!record) return undefined;
    return { username, role: String(record.role || 'user'), record };
  }

  /** 真实数据是扁平 `{salt,hash,rounds}`；老数据可能嵌在 `password` 里，两种都认。 */
  private passwordOf(record: UserRecord | undefined): { salt?: string; hash?: string; rounds?: number } | undefined {
    if (!record) return undefined;
    if (typeof record.hash === 'string') return { salt: record.salt, hash: record.hash, rounds: record.rounds };
    return record.password;
  }

  /** 登录：双桶限流 → 并发闸门 → 校验 → 失败计数 / 成功只清账号桶。 */
  async login(username: string, password: string, ip: string): Promise<{ user: AuthUser; cookie: string }> {
    const ipKey = `ip|${ip}`;
    const userKey = `user|${username}`;
    if (!this.limiter.allowed(ipKey, LOGIN_IP_LIMIT) || !this.limiter.allowed(userKey)) {
      throw apiError('尝试次数过多，请 10 分钟后再试。', HttpStatus.TOO_MANY_REQUESTS);
    }
    if (this.busyLogins >= LOGIN_SLOTS) {
      throw apiError('登录请求过多，请稍后再试。', HttpStatus.TOO_MANY_REQUESTS);
    }
    this.busyLogins += 1;
    try {
      const record = this.users[username];
      let ok = Boolean(record && record.enabled !== false && (await verifyPassword(password, this.passwordOf(record))));
      if (record?.expires && String(record.expires) < todayString()) ok = false;
      if (!ok) {
        this.limiter.failed(ipKey);
        this.limiter.failed(userKey);
        if (!record) await burnPasswordTime(password);
        throw apiError('账号或密码不正确。', HttpStatus.UNAUTHORIZED);
      }
      // 只清账号桶：IP 桶留给窗口自然衰减，否则用合法账号登一次就能清零失败计数。
      this.limiter.succeeded(userKey);
      const token = this.signer.issue(username, record);
      return { user: { username, role: String(record?.role || 'user'), record }, cookie: this.sessionCookie(token) };
    } finally {
      this.busyLogins -= 1;
    }
  }

  verifyToken(token: string): AuthUser | null {
    const payload = this.signer.verify(token, this.users);
    if (!payload) return null;
    const record = this.users[payload.u];
    if (!record) return null;
    return { username: payload.u, role: String(record.role || 'user'), record };
  }

  sessionCookie(token: string): string {
    const maxAge = this.signer.hours * 3600;
    return `outfit_sid=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}`;
  }

  static clearCookie(): string {
    return 'outfit_sid=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0';
  }
}
