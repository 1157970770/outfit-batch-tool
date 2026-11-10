import { createHmac, timingSafeEqual } from 'node:crypto';

import { b64url, unb64url } from './password';

export interface SessionPayload {
  u: string;
  r: string;
  v: number;
  exp: number;
}

/**
 * 会话签名，逐字复刻 `app/web/auth.py::SessionSigner`：
 * payload = {"u","r","v","exp"} → base64url(JSON) + "." + base64url(HMAC-SHA256(secret, raw))。
 * 现有 `web.json` 里的 `session_secret` 是 base64url 无填充，必须按同一口径解。
 */
export class SessionSigner {
  private readonly secret: Buffer;
  readonly hours: number;

  constructor(secret: string, hours = 12) {
    this.secret = unb64url(secret);
    this.hours = Math.max(1, Math.min(168, Math.trunc(hours) || 12));
  }

  issue(username: string, user: { role?: string; version?: number }): string {
    const payload: SessionPayload = {
      u: username,
      r: String(user.role || 'user'),
      v: Number(user.version ?? 1),
      exp: Math.floor(Date.now() / 1000) + this.hours * 3600,
    };
    const raw = b64url(Buffer.from(JSON.stringify(payload), 'utf8'));
    const sig = b64url(createHmac('sha256', this.secret).update(raw, 'ascii').digest());
    return `${raw}.${sig}`;
  }

  verify(token: string, users: Record<string, { role?: string; version?: number; enabled?: boolean; expires?: string }>): SessionPayload | null {
    try {
      const index = token.indexOf('.');
      if (index <= 0) return null;
      const raw = token.slice(0, index);
      const signature = token.slice(index + 1);
      const expected = createHmac('sha256', this.secret).update(raw, 'ascii').digest();
      const provided = unb64url(signature);
      if (provided.length !== expected.length || !timingSafeEqual(expected, provided)) return null;
      const payload = JSON.parse(unb64url(raw).toString('utf8')) as SessionPayload;
      const user = users[String(payload.u)];
      if (!user || user.enabled === false) return null;
      const now = Math.floor(Date.now() / 1000);
      if (Number(payload.exp) < now) return null;
      if (Number(payload.v ?? 1) !== Number(user.version ?? 1)) return null;
      if (user.expires && String(user.expires) < todayString()) return null;
      return { ...payload, r: String(user.role || 'user') };
    } catch {
      return null;
    }
  }
}

/** 与 Python `time.strftime("%Y-%m-%d")` 同一本地时区口径。 */
export function todayString(date = new Date()): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}
