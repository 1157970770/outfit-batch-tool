import { Global, Module } from '@nestjs/common';

import { AuthService } from './auth.service';
import { SessionGuard } from './session.guard';

@Global()
@Module({
  providers: [AuthService, SessionGuard],
  exports: [AuthService, SessionGuard],
})
export class AuthModule {}
