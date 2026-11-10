/**
 * 任务批次接口（P2）。
 *
 * 与 Python `app/web/server.py` 的批次路由一一对应（状态码逐条对齐）：
 * - `GET  /api/batches`                     200 `{ok, batches}`
 * - `POST /api/batches`                     201 `{ok, batch}`
 * - `GET  /api/batches/<id>`                200 `{ok, batch, items}`
 * - `GET  /api/batches/<id>/files.zip`      200 zip / `?probe=1` → 200 `{ok, count, ...}`
 * - `POST /api/batches/<id>/image`          200 `{ok, image_name, batch}`
 * - `POST /api/batches/<id>/images`         200 `{ok, batch, slot_count}`（op=add|remove）
 * - `POST /api/batches/<id>/candidates`     200 `{ok, items, ..., cost_estimate}`
 * - `POST /api/batches/<id>/lock`           200 `{ok, batch}`
 * - `POST /api/batches/<id>/produce`        201 `{ok, job, batch, ...}`
 * - `POST /api/batches/<id>/retry-failed`   201 `{ok, job, batch, ...}`
 * - `POST /api/batches/<id>/recheck`        202 `{ok, jobs, skipped, total_jobs}`
 * - `POST /api/batches/<id>/close`          200 `{ok, batch}`
 * - `POST /api/batches/<id>/prompt`         200 `{ok, batch}`
 * - `DELETE /api/batches/<id>`              200 `{ok, id}`
 *
 * 未定义的 action（例如 `POST .../files.zip`）自然落空 → 404「页面或接口不存在。」，
 * 与 Python 的「POST 分发落空」同结果。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  HttpException,
  Param,
  Post,
  Query,
  Res,
} from '@nestjs/common';
import type { Response } from 'express';
import { CurrentUser, type AuthedUser } from '../auth/decorators';
import { BatchManagerService, BatchError } from './batch-manager.service';

/** BatchError 自带 HTTP 码；其它异常原样抛出（交给全局过滤器）。 */
export function toHttpException(err: unknown): HttpException {
  if (err instanceof BatchError) {
    return new HttpException({ ok: false, error: err.message }, err.status);
  }
  return err instanceof HttpException ? err : new HttpException({ ok: false, error: String((err as Error)?.message || '操作失败') }, 500);
}

@Controller('api/batches')
export class BatchesController {
  constructor(private readonly batches: BatchManagerService) {}

  // ---------------------------------------------------------------- 列表 / 创建

  @Get()
  async list(
    @CurrentUser() user: AuthedUser,
    @Query('scope') scope?: string,
    @Query('owner') owner?: string,
  ): Promise<Record<string, unknown>> {
    const all = String(scope ?? 'mine') === 'all';
    if (all && user.role !== 'admin') throw new HttpException({ ok: false, error: '此操作需要管理员权限。' }, 403);
    try {
      const rows = await this.batches.listFor(user, all, String(owner ?? '').trim());
      return { ok: true, batches: rows };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  @Post()
  @HttpCode(201)
  async create(
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const batch = await this.batches.create(user, body ?? {});
      return { ok: true, batch };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  // ---------------------------------------------------------------- 详情

  @Get(':id')
  async detail(
    @Param('id') id: string,
    @CurrentUser() user: AuthedUser,
    @Query('item_state') itemState?: string,
  ): Promise<Record<string, unknown>> {
    try {
      const { batch, items } = await this.batches.detail(id, user, String(itemState ?? '').trim());
      return { ok: true, batch, items };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  // ---------------------------------------------------------------- 整批下载

  @Get(':id/files.zip')
  async zip(
    @Param('id') id: string,
    @CurrentUser() user: AuthedUser,
    @Res() res: Response,
    @Query('probe') probe?: string,
  ): Promise<void> {
    let info;
    try {
      info = await this.batches.batchVideos(id, user);
    } catch (err) {
      throw toHttpException(err);
    }
    const files = info.files;
    // ?probe=1：只报告「有没有 / 几条」，供前端禁用按钮，不生成 zip。
    if (String(probe ?? '').trim() === '1') {
      res.json({
        ok: true,
        count: files.length,
        batch_no: info.batch_no,
        download_url: `/api/batches/${encodeURIComponent(id)}/files.zip`,
      });
      return;
    }
    if (!files.length) {
      throw new HttpException({ ok: false, error: '暂时没有可下载的视频。' }, 404);
    }
    let zipFile: string;
    try {
      zipFile = await this.batches.zipPath(id, user, files);
    } catch (err) {
      throw toHttpException(err);
    }
    // 批次号里可能有空格/中文/斜杠：只留词字符、中文、点与短横线，最多 60 字符。
    const label = info.batch_no
      // eslint-disable-next-line no-control-regex
      .replace(/[^A-Za-z0-9_\u4e00-\u9fff.-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60);
    const filename = `${label || info.id}.zip`;
    res.setHeader('Content-Type', 'application/zip');
    res.setHeader(
      'Content-Disposition',
      `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
    );
    // 发完即删：每次打包都是一个唯一临时文件，避免并发下载互相截断。
    res.sendFile(path.resolve(zipFile), () => {
      try {
        fs.rmSync(zipFile, { force: true });
      } catch {
        /* 尽力而为 */
      }
    });
  }

  // ---------------------------------------------------------------- draft：换图

  @Post(':id/image')
  @HttpCode(200)
  async setImage(
    @Param('id') id: string,
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const result = await this.batches.setImage(id, user, body ?? {});
      return { ok: true, ...result };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  /** 锁定后的细节图增删：`op=add` / `op=remove`（主图不可换、不可删）。 */
  @Post(':id/images')
  @HttpCode(200)
  async images(
    @Param('id') id: string,
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    const data = body ?? {};
    const op = String(data.op ?? '').trim();
    try {
      if (op === 'add') {
        const result = await this.batches.addImages(id, user, data);
        return { ok: true, ...result };
      }
      if (op === 'remove') {
        const result = await this.batches.removeImages(id, user, data);
        return { ok: true, ...result };
      }
      throw new BatchError('op 只能是 add 或 remove。');
    } catch (err) {
      throw toHttpException(err);
    }
  }

  // ---------------------------------------------------------------- draft：候选清单

  @Post(':id/candidates')
  @HttpCode(200)
  async candidates(
    @Param('id') id: string,
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const result = await this.batches.candidates(id, user, body ?? {});
      return { ok: true, ...result };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  // ---------------------------------------------------------------- draft：锁定

  @Post(':id/lock')
  @HttpCode(200)
  async lock(
    @Param('id') id: string,
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const batch = await this.batches.lock(id, user, body ?? {});
      return { ok: true, batch };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  // ---------------------------------------------------------------- locked：生产

  @Post(':id/produce')
  @HttpCode(201)
  async produce(
    @Param('id') id: string,
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const result = await this.batches.produce(id, user, body ?? {});
      return { ok: true, ...result };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  @Post(':id/retry-failed')
  @HttpCode(201)
  async retryFailed(
    @Param('id') id: string,
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const result = await this.batches.retryFailed(id, user, body ?? {});
      return { ok: true, ...result };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  /** 批次侧联动：把本批次所有含超时/下载失败记录的 job 交给 jobs 侧复查（只查云端，不重提）。 */
  @Post(':id/recheck')
  @HttpCode(202)
  async recheck(
    @Param('id') id: string,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const result = await this.batches.recheckTimeouts(id, user);
      return { ok: true, ...result };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  // ---------------------------------------------------------------- locked：关闭 / 改提示词

  @Post(':id/close')
  @HttpCode(200)
  async close(@Param('id') id: string, @CurrentUser() user: AuthedUser): Promise<Record<string, unknown>> {
    try {
      const batch = await this.batches.close(id, user);
      return { ok: true, batch };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  @Post(':id/prompt')
  @HttpCode(200)
  async prompt(
    @Param('id') id: string,
    @Body() body: Record<string, unknown>,
    @CurrentUser() user: AuthedUser,
  ): Promise<Record<string, unknown>> {
    try {
      const batch = await this.batches.updatePrompt(id, user, body ?? {});
      return { ok: true, batch };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  // ---------------------------------------------------------------- 删除

  @Delete(':id')
  @HttpCode(200)
  async remove(@Param('id') id: string, @CurrentUser() user: AuthedUser): Promise<Record<string, unknown>> {
    try {
      const result = await this.batches.delete(id, user);
      return { ok: true, ...result };
    } catch (err) {
      throw toHttpException(err);
    }
  }
}
