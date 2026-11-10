import { Controller, HttpCode, Post, Req, Res } from '@nestjs/common';
import type { Request, Response } from 'express';

import { AuthService } from '../auth/auth.service';
import { Public } from '../auth/decorators';

@Controller('api')
export class AuthController {
  constructor(private readonly auth: AuthService) {}

  /** POST /api/login：与 Python 的 `_login()` 同口径（双桶限流 + 时序抹平）。 */
  @Public()
  @Post('login')
  @HttpCode(200) // Python 版登录成功是 200；Nest 的 POST 默认 201，必须显式对齐
  async login(@Req() req: Request, @Res({ passthrough: true }) res: Response) {
    const body = (req.body || {}) as { username?: unknown; password?: unknown };
    const username = String(body.username ?? '').trim();
    const password = String(body.password ?? '');
    const ip = req.ip || req.socket?.remoteAddress || '';
    const result = await this.auth.login(username, password, ip);
    res.setHeader('Set-Cookie', result.cookie);
    return { ok: true, user: { username: result.user.username, role: result.user.role } };
  }

  /** POST /api/logout：无鉴权要求，直接清 cookie（与 Python 一致）。 */
  @Public()
  @Post('logout')
  @HttpCode(200)
  logout(@Res({ passthrough: true }) res: Response) {
    res.setHeader('Set-Cookie', AuthService.clearCookie());
    return { ok: true };
  }
}
