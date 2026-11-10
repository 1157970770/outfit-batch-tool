import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';

/**
 * 前端单测只跑 jsdom：这里验证的是「槽位顺序、标签筛选、页面能渲染」这类纯逻辑，
 * 真实浏览器里的视觉回归不在这一层做（见 docs/妙搭部署.md 的验收清单）。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
      '@outfit/shared': path.resolve(__dirname, '../../packages/shared/src/index.ts'),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
    restoreMocks: true,
  },
});
