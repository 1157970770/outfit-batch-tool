import { Controller, Get } from '@nestjs/common';

import { CurrentUser, AuthedUser } from '../auth/decorators';

@Controller('api')
export class MeController {
  /** GET /api/me：未登录由 SessionGuard 统一返回 401。 */
  @Get('me')
  me(@CurrentUser() user: AuthedUser) {
    return { ok: true, user: { username: user.username, role: user.role } };
  }
}
