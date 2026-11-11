import { Body, Controller, Get, HttpCode, Param, Post, Query, Res } from '@nestjs/common';
import type { Response } from 'express';

import { CurrentUser, type AuthedUser } from '../auth/decorators';
import { PsService } from './ps.service';

/** PS 助手（去紫）：`/api/ps/depurple` + `/api/ps/result/<id>`（与原版同名同参）。 */
@Controller('api/ps')
export class PsController {
  constructor(private readonly ps: PsService) {}

  @Post('depurple')
  @HttpCode(200)
  async depurple(
    @Body() body: { upload_id?: unknown; strength?: unknown },
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    const uploadId = String(body?.upload_id ?? '').trim();
    const result = await this.ps.depurple(uploadId, body?.strength, user.username, user.role === 'admin');
    return {
      ok: true,
      upload_id: result.upload_id,
      strength: result.strength,
      width: result.width,
      height: result.height,
      name: result.name,
      url: `/api/ps/result/${result.upload_id}?strength=${result.strength}&v=${Date.now()}`,
      download_url: `/api/ps/result/${result.upload_id}?strength=${result.strength}&download=1`,
      cast_before: result.cast_before,
      cast_after: result.cast_after,
      anchors: result.anchors,
      elapsed_ms: result.elapsed_ms,
      cached: result.cached,
    };
  }

  /** 结果图：`inline` 给 `<img>`，`?download=1` 走附件（图片预览必须能内联）。 */
  @Get('result/:uploadId')
  result(
    @Param('uploadId') uploadId: string,
    @Query('strength') strength: string | undefined,
    @Query('download') download: string | undefined,
    @CurrentUser() user: AuthedUser,
    @Res() res: Response,
  ): void {
    const { file, name } = this.ps.resultFile(uploadId, strength, user.username, user.role === 'admin');
    const disposition = download ? 'attachment' : 'inline';
    res.setHeader('Content-Type', 'image/png');
    res.setHeader('Cache-Control', 'private, max-age=300');
    res.setHeader('Content-Disposition', `${disposition}; filename*=UTF-8''${encodeURIComponent(name)}`);
    res.sendFile(file);
  }
}
