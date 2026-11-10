/**
 * 历史记录的两种视角（P3）：
 * - `GET /api/history/groups`        按批次/账户/日期聚合的卡片列表 + 空批次计数
 * - `GET /api/history/groups/<key>`  某一张卡的明细（分页）
 *
 * 权限口径与 Python 一致：普通账号只看得见自己的任务；管理员 `scope=all` 看全部，
 * 还可以用 `owner` 收窄到某个账户。`scope=all` 但非管理员 → 403「此操作需要管理员权限。」。
 *
 * key 是**虚拟的**：`__ungrouped__`（未分组）、`__owner__:<账户>`、`__day__:<YYYY-MM-DD>`，
 * 都是接口层现算的，job / batch 的落盘结构一个字段都不动。
 */
import { Controller, Get, HttpCode, HttpException, Param, Query } from '@nestjs/common';
import { CurrentUser, type AuthedUser } from '../auth/decorators';
import { BatchManagerService } from './batch-manager.service';
import { toHttpException } from './batches.controller';

/** Python `int()` 口径：空串/非证书写法一律失败（"3.0" 也算失败）。 */
function parseIntParam(raw: string): number {
  const text = String(raw ?? '').trim();
  if (!/^[+-]?\d+$/.test(text)) throw NumberError;
  return Number(text);
}

const NumberError = new Error('分页参数无效。');

@Controller('api/history')
export class HistoryController {
  constructor(private readonly batches: BatchManagerService) {}

  /** `scope` 缺省 "mine"；`lane` 只认 small / big，其它值等于不筛。 */
  @Get('groups')
  @HttpCode(200)
  async groups(
    @CurrentUser() user: AuthedUser,
    @Query('scope') scope?: string,
    @Query('owner') owner?: string,
    @Query('group_by') groupBy?: string,
    @Query('lane') lane?: string,
  ): Promise<Record<string, unknown>> {
    const scopeText = String(scope ?? 'mine').trim() || 'mine';
    const allUsers = scopeText === 'all';
    if (allUsers && user.role !== 'admin') {
      throw new HttpException({ ok: false, error: '此操作需要管理员权限。' }, 403);
    }
    try {
      const groups = await this.batches.historyGroups(
        user,
        allUsers,
        String(owner ?? '').trim(),
        String(groupBy ?? '').trim() || 'batch',
        String(lane ?? '').trim(),
      );
      // 空批次（还没生产过）不生成点不开的卡片，只给个数字，前端据此提示「还有 N 个批次未生产」。
      const empty = await this.batches.emptyBatchCount(user, allUsers, String(owner ?? '').trim());
      return { ok: true, groups, empty_batches: empty };
    } catch (err) {
      throw toHttpException(err);
    }
  }

  @Get('groups/:key')
  @HttpCode(200)
  async items(
    @Param('key') key: string,
    @CurrentUser() user: AuthedUser,
    @Query('offset') offset?: string,
    @Query('limit') limit?: string,
    @Query('scope') scope?: string,
    @Query('owner') owner?: string,
    @Query('lane') lane?: string,
  ): Promise<Record<string, unknown>> {
    let offsetValue: number;
    let limitValue: number;
    try {
      offsetValue = parseIntParam(offset ?? '0');
      limitValue = parseIntParam(limit ?? '200');
    } catch {
      throw new HttpException({ ok: false, error: '分页参数无效。' }, 400);
    }
    // Python：`scope` 缺省是 **空串**（不是 "mine"），只有显式 all / mine 才收窄，
    // 其余一律 null —— 交给 history_items 沿用旧行为（管理员不传 scope 时不强塞 owner）。
    const scopeText = String(scope ?? '').trim();
    let allUsers: boolean | null;
    if (scopeText === 'all') {
      if (user.role !== 'admin') {
        throw new HttpException({ ok: false, error: '此操作需要管理员权限。' }, 403);
      }
      allUsers = true;
    } else if (scopeText === 'mine') {
      allUsers = false;
    } else {
      allUsers = null;
    }
    // Express 已经解过 URL 编码；再兜一次，兼容前端把 `%3A` 原样塞进路径的写法。
    // unquote 在 Python 里永不抛错，这里的 decodeURIComponent 遇到畸形 `%` 会抛 URIError，
    // 所以失败就退回原串，不能把 500 暴露出去。
    let groupKey = key;
    try {
      groupKey = decodeURIComponent(key);
    } catch {
      groupKey = key;
    }
    try {
      const result = await this.batches.historyItems(
        groupKey,
        user,
        offsetValue,
        limitValue,
        String(owner ?? '').trim(),
        allUsers,
        String(lane ?? '').trim(),
      );
      return { ok: true, ...result };
    } catch (err) {
      throw toHttpException(err);
    }
  }
}
