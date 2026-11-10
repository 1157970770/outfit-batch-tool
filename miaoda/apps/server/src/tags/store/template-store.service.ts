import { Injectable, Logger } from '@nestjs/common';
import { join } from 'node:path';

import { AppConfigService } from '../../config/app-config.service';
import { TemplateStore } from './template-store';

/**
 * 真源单例的持有者：整个进程共用一份 `TemplateStore`，避免多实例各自缓存 revision。
 *
 * 为什么需要 refresh：桌面打标器（Python）可能在别处改过同一个文件，所以每次读快照前
 * 先按文件指纹判断要不要重载（对应 Python `app.tag_store.refresh_if_changed()`）。
 * 装载失败（文件损坏且 .bak 也坏）不抛给调用方——标签页要能显示 `enabled:false` 与原因，
 * 而不是整页 500；写接口会另外按 503 处理。
 */
@Injectable()
export class TemplateStoreService {
  private readonly logger = new Logger('TagStore');
  private cached: TemplateStore | null = null;
  private loadError = '';

  constructor(private readonly config: AppConfigService) {}

  /** 真源路径：`<dataDir>/template_tags.json`（与 Python 版同一个文件）。 */
  get file(): string {
    return join(this.config.dataDir, 'template_tags.json');
  }

  /** 上次装载失败的原因（成功时为空串）。 */
  get error(): string {
    return this.loadError;
  }

  /** 取单例；文件不存在时按空库启动（第一次写入才落盘），损坏时返回 null。 */
  get store(): TemplateStore | null {
    const file = this.file;
    if (this.cached && this.cached.path === file) return this.cached;
    try {
      this.cached = TemplateStore.load(file, true);
      this.loadError = '';
      if (this.cached.recoveredFromBak) {
        this.logger.warn(`真源损坏，已从上一次的 .bak 恢复：${file}`);
      }
    } catch (err) {
      this.cached = null;
      this.loadError = (err as Error).message;
      this.logger.error(`标签真源装载失败：${this.loadError}`);
    }
    return this.cached;
  }

  /** 别的进程写过就重载；返回是否真的重载了。 */
  refreshIfChanged(): boolean {
    const store = this.cached;
    if (!store) return false;
    const changed = store.refreshIfChanged();
    if (changed) this.logger.log(`检测到标签真源被外部修改，已重载（revision=${store.revision}）`);
    return changed;
  }
}
