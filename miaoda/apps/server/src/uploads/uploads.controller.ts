/**
 * 图片上传与读取。
 *
 * - `POST /api/uploads`：JSON `{filename, data}`，`data` 为 base64（可带 data: 前缀），返回 201；
 * - `GET /api/uploads/<id>`：带 owner 校验，返回图片字节。
 */
import * as path from 'node:path';
import { Body, Controller, Get, HttpCode, HttpException, Param, Post, Res } from '@nestjs/common';
import type { Response } from 'express';
import { CurrentUser, type AuthedUser } from '../auth/decorators';
import { UploadsService } from './uploads.service';

const MIME: Record<string, string> = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
};

@Controller('api/uploads')
export class UploadsController {
  constructor(private readonly uploads: UploadsService) {}

  @Post()
  @HttpCode(201)
  create(@Body() body: { filename?: string; data?: string }, @CurrentUser() user: AuthedUser) {
    try {
      const { meta } = this.uploads.create(String(body?.filename ?? ''), String(body?.data ?? ''), user.username);
      const info = meta.info ?? {};
      return {
        ok: true,
        upload_id: meta.id,
        name: meta.name,
        width: info.width,
        height: info.height,
        size_mb: info.sizeMb,
      };
    } catch (err) {
      // 图片格式/尺寸/体积不合规是用户可修复的输入问题：与 Python 一致按 status 返回，文案原样下发。
      const status = Number((err as { status?: number }).status || 400);
      const message = (err as Error).message || '图片无法上传。';
      throw new HttpException({ ok: false, error: message }, status);
    }
  }

  @Get(':id')
  serve(@Param('id') id: string, @CurrentUser() user: AuthedUser, @Res() res: Response): void {
    const peek = this.uploads.peek(id);
    if (!peek) {
      res.status(404).json({ ok: false, error: '找不到这张图片。' });
      return;
    }
    if (user.role !== 'admin' && peek.meta.owner !== user.username) {
      res.status(403).json({ ok: false, error: '无权查看这张图片。' });
      return;
    }
    const mime = MIME[path.extname(peek.file).toLowerCase()] ?? 'application/octet-stream';
    res.setHeader('Content-Type', mime);
    res.setHeader('Cache-Control', 'private, max-age=300');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader(
      'Content-Disposition',
      `inline; filename*=UTF-8''${encodeURIComponent(String(peek.meta.name || path.basename(peek.file)))}`,
    );
    res.sendFile(peek.file, (err) => {
      if (err && !res.headersSent) res.status(404).end();
    });
  }
}
