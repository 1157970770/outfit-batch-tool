/**
 * 登录失败计数，逐条复刻 `app/web/auth.py::LoginLimiter`：
 * - IP 桶挡「换用户名继续试」，账号桶挡「换 IP 打同一账号」；
 * - 空桶用完即删；桶数超过 maxKeys 时按插入顺序淘汰最老的（近似 LRU）。
 */
export class LoginLimiter {
  private readonly failures = new Map<string, number[]>();
  private readonly order: string[] = [];

  constructor(
    readonly limit = 8,
    readonly window = 600,
    readonly maxKeys = 2000,
  ) {}

  allowed(key: string, limit?: number): boolean {
    const cap = Math.trunc(limit ?? this.limit);
    const now = Date.now() / 1000;
    const values = this.failures.get(key);
    if (!values || values.length === 0) return true;
    while (values.length > 0 && now - values[0] > this.window) values.shift();
    if (values.length === 0) {
      this.drop(key);
      return true;
    }
    return values.length < cap;
  }

  failed(key: string): void {
    let values = this.failures.get(key);
    if (!values) {
      values = [];
      this.failures.set(key, values);
      this.order.push(key);
      this.evict();
    }
    values.push(Date.now() / 1000);
  }

  succeeded(key: string): void {
    this.drop(key);
  }

  private drop(key: string): void {
    if (this.failures.delete(key)) {
      const index = this.order.indexOf(key);
      if (index >= 0) this.order.splice(index, 1);
    }
  }

  private evict(): void {
    const cap = Math.max(16, Math.trunc(this.maxKeys));
    while (this.failures.size > cap && this.order.length > 0) {
      this.drop(this.order[0]);
    }
  }
}
