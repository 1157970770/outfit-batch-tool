/**
 * 生图 tab 的 HTTP 接口。路由与状态码与 `app/web/server.py` 一一对应：
 *
 * - `GET    /api/imagegen/tasks`                              → `{ok, items, configured, reason}`
 * - `POST   /api/imagegen/tasks`                              → 201 `{ok, task}`
 * - `GET    /api/imagegen/tasks/<id>`                         → `{ok, task}`
 * - `DELETE /api/imagegen/tasks/<id>`                         → `{ok}`（生成中 409）
 * - `GET    /api/imagegen/tasks/<id>/images/<n>[?download=1]` → 图片字节
 * - `GET    /api/imagegen/tasks/<id>/references/<n>`          → 参考图字节
 *
 * 参考图复用 `/api/uploads` 的 upload_id（前端先逐张上传再建任务），
 * `upload_ids[]` 的顺序就是模型看到的「图1、图2…」，这里不做任何重排。
 */
import { Body, Controller, Delete, Get, HttpCode, Param, Post, Query, Res } from '@nestjs/common';
import type { Response } from 'express';

import { CurrentUser, type AuthedUser } from '../auth/decorators';
import { UPLOAD_ID_RE, UploadsService } from '../uploads/uploads.service';
import { mime_for_name } from './imagegen-api';
import {
  ImagegenManagerService,
  imagegen_error,
  type ImageReferenceSlot,
} from './imagegen-manager.service';

const MAX_IMAGE_SLOTS = 10;
const TASK_ID_RE = /^[a-f0-9]{32}$/;

@Controller('api/imagegen')
export class ImagegenController {
  constructor(
    private readonly manager: ImagegenManagerService,
    private readonly uploads: UploadsService,
  ) {}

  @Get('tasks')
  list(@Query('limit') limit: string | undefined, @CurrentUser() user: AuthedUser) {
    const text = String(limit ?? '').trim();
    const parsed = /^[+-]?\d+$/.test(text) ? Number(text) : 40;
    return {
      ok: true,
      items: this.manager.list_tasks(user, { limit: parsed }),
      configured: this.manager.configured,
      reason: this.manager.unconfigured_reason(),
    };
  }

  @Post('tasks')
  @HttpCode(201)
  create(@Body() body: Record<string, unknown>, @CurrentUser() user: AuthedUser) {
    const slots = this.resolve_slots(body ?? {}, user);
    return { ok: true, task: this.manager.create_task(user, body ?? {}, slots) };
  }

  @Get('tasks/:id')
  detail(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    return { ok: true, task: this.manager.get_record(id, user) };
  }

  @Delete('tasks/:id')
  remove(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    this.manager.delete_task(id, user);
    return { ok: true };
  }

  @Get('tasks/:id/images/:index')
  image(
    @Param('id') id: string,
    @Param('index') raw_index: string,
    @Query('download') download: string | undefined,
    @CurrentUser() user: AuthedUser,
    @Res() res: Response,
  ): void {
    this.send_slot(res, id, raw_index, user, false, download);
  }

  @Get('tasks/:id/references/:index')
  reference(
    @Param('id') id: string,
    @Param('index') raw_index: string,
    @Query('download') download: string | undefined,
    @CurrentUser() user: AuthedUser,
    @Res() res: Response,
  ): void {
    this.send_slot(res, id, raw_index, user, true, download);
  }

  private send_slot(
    res: Response,
    id: string,
    raw_index: string,
    user: AuthedUser,
    reference: boolean,
    download: string | undefined,
  ): void {
    if (!TASK_ID_RE.test(String(id ?? ''))) throw imagegen_error('找不到这张图片。', 404);
    const text = String(raw_index ?? '').trim();
    if (!/^[+-]?\d+$/.test(text)) throw imagegen_error('找不到这张图片。', 404);
    const index = Number(text);

    const { file, name } = this.manager.image_file(id, index, user, { reference });
    const wants_download = ['1', 'true', 'yes'].includes(String(download ?? '0').toLowerCase());

    const mime = mime_for_name(file);
    res.setHeader('Content-Type', mime === 'application/octet-stream' ? 'image/png' : mime);
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    const disposition = wants_download ? 'attachment' : 'inline';
    res.setHeader('Content-Disposition', `${disposition}; filename*=UTF-8''${encodeURIComponent(name)}`);
    // express 的 sendFile 自带 Range / Accept-Ranges，与 Python `_send_file` 行为一致。
    res.sendFile(file, (err) => {
      if (err && !res.headersSent) res.status(404).json({ ok: false, error: '找不到这张图片。' });
    });
  }

  /** 等价 Python `_resolve_image_slots(..., allow_empty=True)`。 */
  private resolve_slots(body: Record<string, unknown>, user: AuthedUser): ImageReferenceSlot[] {
    const raw = body.upload_ids;
    let items: unknown[];
    if (raw === undefined || raw === null) {
      const single = String(body.upload_id ?? '').trim();
      items = single ? [single] : [];
    } else if (Array.isArray(raw)) {
      items = raw;
    } else {
      throw imagegen_error('图片参数无效，请重新上传。');
    }

    const ids = items.map((item) => String(item ?? '').trim()).filter((item) => item !== '');
    if (ids.length === 0) return [];
    if (ids.length > MAX_IMAGE_SLOTS) {
      throw imagegen_error(
        `一次最多 ${MAX_IMAGE_SLOTS} 张图片（1 张定妆图 + 9 张细节图），当前 ${ids.length} 张。`,
      );
    }
    if (new Set(ids).size !== ids.length) throw imagegen_error('同一张图片不能重复提交。');

    const slots: ImageReferenceSlot[] = [];
    ids.forEach((upload_id, offset) => {
      const index = offset + 1;
      if (!UPLOAD_ID_RE.test(upload_id)) {
        throw imagegen_error(`第 ${index} 张图片已失效，请重新上传。`, 404);
      }
      const resolved = this.uploads.resolve(upload_id, String(user?.username ?? ''));
      if (resolved === null) throw imagegen_error(`第 ${index} 张图片已失效，请重新上传。`, 404);
      slots.push({ upload_id, name: resolved.meta.name, path: resolved.file });
    });
    return slots;
  }
}
