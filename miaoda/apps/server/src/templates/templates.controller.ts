/**
 * 模板接口：列表 + 预览代理 + 下载代理。
 *
 * 为什么预览要经过后端：模板视频在 COS 上，浏览器直连会拿到带签名的长 URL，
 * 且 Range 请求需要原样透传（拖进度条/断点续播）。这里做纯代理，不落盘。
 */
import { Readable } from 'node:stream';
import { Controller, Get, Param, Query, Res } from '@nestjs/common';
import type { Response } from 'express';
import { TemplateCatalogService, TemplateError } from './template-catalog.service';

@Controller('api/templates')
export class TemplatesController {
  constructor(private readonly catalog: TemplateCatalogService) {}

  @Get()
  async list() {
    await this.catalog.build();
    if (this.catalog.error) {
      throw Object.assign(new Error(this.catalog.error), { status: 503 });
    }
    return {
      ok: true,
      items: this.catalog.public(),
      categories: this.catalog.tagCategories,
      tag_values: this.catalog.tagValues,
      tag_filter_enabled: this.catalog.tagFilterEnabled,
    };
  }

  @Get(':id/preview')
  async preview(
    @Param('id') id: string,
    @Query('allow_deprecated') allowDeprecated: string,
    @Res() res: Response,
  ): Promise<void> {
    await this.proxy(id, res, allowDeprecated === '1', false);
  }

  @Get(':id/download')
  async download(@Param('id') id: string, @Res() res: Response): Promise<void> {
    await this.proxy(id, res, true, true);
  }

  private async proxy(id: string, res: Response, includeRetired: boolean, attachment: boolean): Promise<void> {
    await this.catalog.build();
    let item;
    try {
      item = this.catalog.resolve(id, includeRetired);
    } catch (err) {
      if (err instanceof TemplateError) {
        res.status(404).json({ ok: false, error: '找不到该视频场景' });
        return;
      }
      throw err;
    }
    const url = String(item.url || '');
    if (!url) {
      const message = item.key ? (attachment ? '下载暂不可用。' : '模板预览暂不可用。') : (attachment ? '该视频尚未上传到云端，无法下载。' : '该视频尚未上传到云端，暂无预览。');
      res.status(item.key ? 503 : 404).json({ ok: false, error: message });
      return;
    }
    const headers: Record<string, string> = { 'User-Agent': 'OutfitLocal/1' };
    const range = String(res.req.headers.range || '');
    if (range) headers.Range = range;
    let upstream: globalThis.Response;
    try {
      upstream = await fetch(url, { headers, signal: AbortSignal.timeout(60_000) });
    } catch (err) {
      res.status(502).json({ ok: false, error: `视频代理失败：${(err as Error).message}` });
      return;
    }
    res.status(upstream.status);
    res.setHeader('Content-Type', upstream.headers.get('content-type') || 'video/mp4');
    for (const name of ['content-length', 'content-range', 'accept-ranges']) {
      const value = upstream.headers.get(name);
      if (value) res.setHeader(name, value);
    }
    if (!upstream.headers.get('accept-ranges')) res.setHeader('Accept-Ranges', 'bytes');
    if (attachment) {
      const filename = item.name || item.relative_path || `${id}.mp4`;
      res.setHeader('Content-Disposition', `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`);
      res.setHeader('Cache-Control', 'no-store');
    } else {
      res.setHeader('Cache-Control', 'private, max-age=300');
    }
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (!upstream.body) {
      res.end();
      return;
    }
    const stream = Readable.fromWeb(upstream.body as Parameters<typeof Readable.fromWeb>[0]);
    stream.on('error', () => res.destroy());
    res.on('close', () => stream.destroy());
    stream.pipe(res);
  }
}
