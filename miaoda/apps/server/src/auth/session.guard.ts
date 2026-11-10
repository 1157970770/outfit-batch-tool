import { CanActivate, ExecutionContext, HttpStatus, Injectable } from '@nestjs/common';
import { Reflector } from '@nestjs/core';

import { AppConfigService } from '../config/app-config.service';
import { AuthService, apiError } from './auth.service';
import { ADMIN_ONLY_KEY, AuthedUser, IS_PUBLIC_KEY } from './decorators';
import { parseCookies } from '../security/cookies';
import { SESSION_COOKIE } from '@outfit/shared';

/**
 * 全局会话守卫，行为对齐 `server.py::Handler._user()`：
 * 未登录 401「登录已过期，请重新登录。」；`@AdminOnly()` 非管理员 403「此操作需要管理员权限。」。
 */
@Injectable()
export class SessionGuard implements CanActivate {
  constructor(
    private readonly reflector: Reflector,
    private readonly auth: AuthService,
    private readonly config: AppConfigService,
  ) {}

  canActivate(context: ExecutionContext): boolean {
    if (context.getType() !== 'http') return true;
    const handler = context.getHandler();
    const controller = context.getClass();
    const isPublic = this.reflector.getAllAndOverride<boolean>(IS_PUBLIC_KEY, [handler, controller]);
    if (isPublic) return true;

    const request = context.switchToHttp().getRequest();
    const token = String(parseCookies(request.headers?.cookie)[SESSION_COOKIE] || '');
    const user = token ? this.auth.verifyToken(token) : null;
    if (!user) throw apiError('登录已过期，请重新登录。', HttpStatus.UNAUTHORIZED);

    (request as { user?: AuthedUser }).user = user;
    void this.config;

    const adminOnly = this.reflector.getAllAndOverride<boolean>(ADMIN_ONLY_KEY, [handler, controller]);
    if (adminOnly && user.role !== 'admin') {
      throw apiError('此操作需要管理员权限。', HttpStatus.FORBIDDEN);
    }
    return true;
  }
}
