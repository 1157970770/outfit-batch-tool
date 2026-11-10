/**
 * 「标签」页控制器，路由与 `app/web/server.py` 一一对应：
 * - `GET  /api/tags/snapshot?rev=`      快照（rev 命中时只回 unchanged）
 * - `GET  /api/tags/versions?limit=`    版本列表
 * - `POST /api/tags/tags`               改标签（replace / add / remove）
 * - `POST /api/tags/deprecate`          弃用 / 恢复
 * - `POST /api/tags/rating`             打分
 * - `POST /api/tags/library`            标签库与大类维护
 * - `POST /api/tags/impact`             影响预览
 * - `POST /api/tags/versions`           手动打版本
 * - `POST /api/tags/restore`            回滚整库
 * - `POST /api/tags/videos/upload`      原始字节流上传新视频
 *
 * 全部需要登录（全局 SessionGuard 兜底），但**不要求 admin**：所有登录用户都能写。
 */
import { Body, Controller, Get, HttpCode, Post, Query, Req } from '@nestjs/common';
import type { Request } from 'express';

import { AuthedUser, CurrentUser } from '../auth/decorators';
import { TagsService } from './tags.service';

/** 请求头可能是数组（Node 允许多值），取第一个再解 URL 编码。 */
function header(value: unknown): string {
  const text = Array.isArray(value) ? String(value[0] ?? '') : String(value ?? '');
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

@Controller('api/tags')
export class TagsController {
  constructor(private readonly tags: TagsService) {}

  /** 标签页快照；`rev` 与当前 revision 相同就只回 `{ok,unchanged,revision}`。 */
  @Get('snapshot')
  snapshot(@Query('rev') rev?: string): Promise<Record<string, unknown>> {
    return this.tags.snapshot(rev);
  }

  /** 版本列表（自动 50 份 + 手动 50 份）。 */
  @Get('versions')
  versions(@Query('limit') limit?: string): Record<string, unknown> {
    return this.tags.versions(limit);
  }

  /** 改标签：`{ids, tags, mode, revision}`。 */
  @Post('tags')
  @HttpCode(200)
  setTags(@Body() body: Record<string, unknown>, @CurrentUser() user: AuthedUser): Promise<Record<string, unknown>> {
    return this.tags.setTags(body ?? {}, user);
  }

  /** 弃用 / 恢复：`{ids, deprecated, reason, revision}`。 */
  @Post('deprecate')
  @HttpCode(200)
  deprecate(@Body() body: Record<string, unknown>, @CurrentUser() user: AuthedUser): Promise<Record<string, unknown>> {
    return this.tags.deprecate(body ?? {}, user);
  }

  /** 打分：`{ids, rating(0-5), revision}`。 */
  @Post('rating')
  @HttpCode(200)
  setRating(@Body() body: Record<string, unknown>, @CurrentUser() user: AuthedUser): Promise<Record<string, unknown>> {
    return this.tags.setRating(body ?? {}, user);
  }

  /** 标签库维护：`{action, ...params, revision}`。 */
  @Post('library')
  @HttpCode(200)
  library(@Body() body: Record<string, unknown>, @CurrentUser() user: AuthedUser): Promise<Record<string, unknown>> {
    return this.tags.library(body ?? {}, user);
  }

  /** 影响预览（只读）：`{action, ...params}`；不需要 revision。 */
  @Post('impact')
  @HttpCode(200)
  impact(@Body() body: Record<string, unknown>, @CurrentUser() user: AuthedUser): Record<string, unknown> {
    return this.tags.impact(body ?? {}, user);
  }

  /** 手动打版本：`{note}`。 */
  @Post('versions')
  @HttpCode(200)
  createVersion(
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    return this.tags.createVersion(body ?? {}, user);
  }

  /** 回滚整库：`{name, revision}`。 */
  @Post('restore')
  @HttpCode(200)
  restoreVersion(
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    return this.tags.restoreVersion(body ?? {}, user);
  }

  /**
   * 上传新视频：**原始字节流**（不是 multipart/base64，50MB 走 base64 会多占 1/3 内存）。
   *
   * 请求头：`X-File-Name`（URL 编码）、`X-Tags`（URL 编码的 JSON 数组）、
   * `X-Auto-Number`（'1' 时按清单顺延成 0001 形式）。body 由 `main.ts` 里的
   * `express.raw` 提前解析成 Buffer；成功返回 201（与 Python 一致）。
   */
  @Post('videos/upload')
  @HttpCode(201)
  upload(@Req() req: Request, @CurrentUser() user: AuthedUser): Promise<Record<string, unknown>> {
    return this.tags.uploadVideo(
      {
        data: Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0),
        fileName: header(req.headers['x-file-name']),
        tags: header(req.headers['x-tags']),
        autoNumber: header(req.headers['x-auto-number']).trim() === '1',
      },
      user,
    );
  }
}
