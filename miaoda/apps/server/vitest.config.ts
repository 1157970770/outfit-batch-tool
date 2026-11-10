import { defineConfig } from 'vitest/config';

/** 后端单测只跑纯函数与内存对象：鉴权哈希、会话签名、限流、路径重锚、图片解析、ZIP 打包。 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    globals: true,
    restoreMocks: true,
  },
});
