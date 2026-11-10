import { createParamDecorator, ExecutionContext, SetMetadata } from '@nestjs/common';

import type { UserRecord } from '../config/app-config.service';

export const IS_PUBLIC_KEY = 'outfit:public';
export const ADMIN_ONLY_KEY = 'outfit:admin';

/** 免鉴权路由（登录页、/healthz、静态资源）。 */
export const Public = () => SetMetadata(IS_PUBLIC_KEY, true);

/** 需要 admin 角色（与 `_user(admin=True)` 同义）。 */
export const AdminOnly = () => SetMetadata(ADMIN_ONLY_KEY, true);

export interface AuthedUser {
  username: string;
  role: string;
  record: UserRecord;
}

export const CurrentUser = createParamDecorator((_data: unknown, ctx: ExecutionContext): AuthedUser | undefined => {
  const request = ctx.switchToHttp().getRequest();
  return request.user as AuthedUser | undefined;
});
