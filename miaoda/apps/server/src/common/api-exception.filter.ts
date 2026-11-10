import { ArgumentsHost, Catch, ExceptionFilter, HttpException, HttpStatus, Logger } from '@nestjs/common';
import type { Response } from 'express';

/**
 * 统一错误体：与 Python `_error()` 一样返回 `{ok:false,error}`，
 * 前端只用 `error` 字段弹 toast，不用再判断 status。
 */
@Catch()
export class ApiExceptionFilter implements ExceptionFilter {
  private readonly logger = new Logger('ApiError');

  catch(exception: unknown, host: ArgumentsHost): void {
    const res = host.switchToHttp().getResponse<Response>();
    if (res.headersSent) return;

    let status = HttpStatus.INTERNAL_SERVER_ERROR;
    let message = '服务器内部错误，请稍后重试。';

    if (exception instanceof HttpException) {
      status = exception.getStatus();
      const body = exception.getResponse();
      if (typeof body === 'string') {
        message = body;
      } else if (body && typeof body === 'object') {
        const payload = body as { ok?: unknown; error?: unknown; message?: unknown };
        // 三种来源按优先级取文案：
        // 1) 我们自己抛的 `{ok:false,error}`（ConflictError 等）→ error 就是中文
        // 2) `new BadRequestException('中文')` 在 Nest 里变成 `{message:'中文',error:'Bad Request'}`，
        //    这里的 error 是 HTTP 原因短语，必须跳过，否则前端只会看到 "Bad Request"
        // 3) 校验管道给的 message 数组 → 用「；」拼接
        if (payload.ok === false && typeof payload.error === 'string') {
          message = payload.error;
        } else if (typeof payload.message === 'string') {
          message = payload.message;
        } else if (Array.isArray(payload.message)) {
          message = payload.message.join('；');
        } else if (typeof payload.error === 'string') {
          message = payload.error;
        }
      }
    } else {
      const err = exception as { type?: string; status?: number; statusCode?: number; message?: string };
      if (err?.type === 'entity.parse.failed') {
        status = HttpStatus.BAD_REQUEST;
        message = '请求内容不是合法 JSON。';
      } else if (err?.type === 'entity.too.large') {
        status = HttpStatus.PAYLOAD_TOO_LARGE;
        message = '请求内容过大。';
      } else {
        this.logger.error(err?.message || String(exception), (exception as Error)?.stack);
      }
    }

    // Nest 内置 404 的默认文案是 "Not Found"；统一成中文，和 Python 版一致
    if (status === HttpStatus.NOT_FOUND && /^not found$/i.test(message)) message = '页面或接口不存在。';

    res.status(status).json({ ok: false, error: message });
  }
}
