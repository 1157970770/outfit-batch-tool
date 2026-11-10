import type { NextFunction, Request, Response } from 'express';

import { AppConfigService } from '../config/app-config.service';

/**
 * 与 Python `server.py` 的请求前检查 + 统一响应头对齐：
 * - Host 不在白名单 → 421「访问地址无效。」
 * - POST/DELETE 的 Origin 与 Host 不一致 → 403「请求来源无效，请刷新页面后重试。」
 * - `Transfer-Encoding: chunked` → 411（现有客户端全部带 Content-Length）
 * - 每个响应都带 no-store / nosniff / DENY / no-referrer / CSP
 */
export function createSecurityMiddleware(config: AppConfigService) {
  const csp = [
    "default-src 'self'",
    "img-src 'self' data: blob:",
    "media-src 'self'",
    "connect-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "script-src 'self'",
    `frame-src ${config.materialUrl}`,
    "frame-ancestors 'none'",
  ].join('; ');

  return function securityMiddleware(req: Request, res: Response, next: NextFunction): void {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', csp);

    const host = String(req.headers.host || '').split(':')[0].toLowerCase();
    if (!config.allowedHosts.has(host)) {
      res.status(421).json({ ok: false, error: '访问地址无效。' });
      return;
    }

    if (req.method !== 'GET' && req.method !== 'HEAD') {
      const origin = req.headers.origin;
      if (origin) {
        try {
          const parsed = new URL(origin);
          const hostHeader = String(req.headers.host || '').toLowerCase();
          const okHost = config.allowedHosts.has(parsed.hostname.toLowerCase());
          if (!(parsed.protocol === 'http:' || parsed.protocol === 'https:') || !okHost || parsed.host.toLowerCase() !== hostHeader) {
            res.status(403).json({ ok: false, error: '请求来源无效，请刷新页面后重试。' });
            return;
          }
        } catch {
          res.status(403).json({ ok: false, error: '请求来源无效，请刷新页面后重试。' });
          return;
        }
      }
    }

    if (String(req.headers['transfer-encoding'] || '').toLowerCase().includes('chunked')) {
      res.status(411).json({ ok: false, error: '不支持分块传输（Transfer-Encoding: chunked），请带 Content-Length。' });
      return;
    }

    next();
  };
}
