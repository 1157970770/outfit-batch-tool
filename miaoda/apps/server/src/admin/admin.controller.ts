/**
 * 管理端路由：`/api/admin/*`，与 Python `app/web/server.py` 的路由逐条对应。
 *
 * 全部挂 `@AdminOnly()`（全局 SessionGuard 先校验登录，再做管理员角色判断），
 * 未登录 401「登录已过期，请重新登录。」；非管理员 403「此操作需要管理员权限。」。
 *
 * | 方法 | 路径 | 状态码 |
 * | --- | --- | --- |
 * | GET    | /api/admin/status             | 200 |
 * | GET    | /api/admin/template-health    | 200 |
 * | GET    | /api/admin/usage              | 200 |
 * | GET    | /api/admin/archive-search     | 200 / 400 / 503 |
 * | POST   | /api/admin/archive-sign       | 200 / 400 / 503 |
 * | POST   | /api/admin/reload-templates   | 200 / 503 |
 * | GET    | /api/admin/users              | 200 |
 * | POST   | /api/admin/users              | 201 |
 * | POST   | /api/admin/users/:name/reset  | 200 / 400 / 404 |
 * | POST   | /api/admin/users/:name/quota  | 200 / 400 / 404 |
 * | POST   | /api/admin/users/:name/enable | 200 / 404 |
 * | POST   | /api/admin/users/:name/disable| 200 / 404 / 409 |
 * | DELETE | /api/admin/users/:name        | 200 / 404 / 409 |
 * | GET    | /api/admin/accounts/:owner/work      | 200 |
 * | GET    | /api/admin/accounts/:owner/work.csv  | 200 text/csv |
 */
import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  HttpStatus,
  Param,
  Post,
  Query,
  Res,
} from '@nestjs/common';
import type { Response } from 'express';

import { AdminOnly, CurrentUser, type AuthedUser } from '../auth/decorators';
import { AdminService, type AdminQuery, type CsvPayload } from './admin.service';

@AdminOnly()
@Controller('api/admin')
export class AdminController {
  constructor(private readonly admin: AdminService) {}

  // ---------------------------------------------------------------- 状态 / 模板

  @Get('status')
  status(): Record<string, unknown> {
    return this.admin.status();
  }

  @Get('template-health')
  templateHealth(): Record<string, unknown> {
    return this.admin.templateHealth();
  }

  @Post('reload-templates')
  @HttpCode(HttpStatus.OK)
  reloadTemplates(): Promise<Record<string, unknown>> {
    return this.admin.reloadTemplates();
  }

  // ---------------------------------------------------------------- 用量 / 归档

  @Get('usage')
  usage(@Query() query: AdminQuery): Record<string, unknown> {
    return this.admin.usage(query);
  }

  @Get('archive-search')
  archiveSearch(@Query() query: AdminQuery): Promise<Record<string, unknown>> {
    return this.admin.archiveSearch(query);
  }

  @Post('archive-sign')
  @HttpCode(HttpStatus.OK)
  archiveSign(@Body() body: Record<string, unknown>): Record<string, unknown> {
    return this.admin.archiveSign(body ?? {});
  }

  // ---------------------------------------------------------------- 账户

  @Get('users')
  listUsers(): Record<string, unknown> {
    return this.admin.listUsers();
  }

  @Post('users')
  @HttpCode(HttpStatus.CREATED)
  addUser(@Body() body: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.admin.addUser(body ?? {});
  }

  @Post('users/:username/reset')
  @HttpCode(HttpStatus.OK)
  resetPassword(
    @Param('username') username: string,
    @Body() body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return this.admin.resetPassword(username, body ?? {});
  }

  @Post('users/:username/quota')
  @HttpCode(HttpStatus.OK)
  setQuota(@Param('username') username: string, @Body() body: Record<string, unknown>): Record<string, unknown> {
    return this.admin.setQuota(username, body ?? {});
  }

  @Post('users/:username/enable')
  @HttpCode(HttpStatus.OK)
  enable(@Param('username') username: string): Record<string, unknown> {
    return this.admin.enableUser(username);
  }

  @Post('users/:username/disable')
  @HttpCode(HttpStatus.OK)
  disable(@Param('username') username: string, @CurrentUser() user: AuthedUser): Record<string, unknown> {
    return this.admin.disableUser(username, user);
  }

  @Delete('users/:username')
  remove(@Param('username') username: string, @CurrentUser() user: AuthedUser): Record<string, unknown> {
    return this.admin.deleteUser(username, user);
  }

  // ---------------------------------------------------------------- 账户明细 / CSV

  @Get('accounts/:owner/work')
  async work(
    @Param('owner') owner: string,
    @Query() query: AdminQuery,
    @Res({ passthrough: true }) res: Response,
  ): Promise<Record<string, unknown> | string> {
    if (String(query?.format ?? '') === 'csv') {
      return this.sendCsv(res, await this.admin.accountCsv(owner, query));
    }
    return this.admin.accountWork(owner, query);
  }

  @Get('accounts/:owner/work.csv')
  async workCsv(
    @Param('owner') owner: string,
    @Query() query: AdminQuery,
    @Res({ passthrough: true }) res: Response,
  ): Promise<string> {
    return this.sendCsv(res, await this.admin.accountCsv(owner, query));
  }

  /** 与 Python `_send_bytes` 同口径：只给 `filename*=UTF-8''...`，内容带 BOM。 */
  private sendCsv(res: Response, payload: CsvPayload): string {
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename*=UTF-8''${encodeURIComponent(payload.filename)}`);
    return payload.body;
  }
}
