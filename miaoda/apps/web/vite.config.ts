import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import * as path from 'node:path';

// 套壳模式：前端只做 UI，接口一律打到本机 Python 服务（设计区 8766 / 生产 8765）。
// 本地开发用代理绕开跨域；部署到妙搭时改成绝对地址（VITE_API_BASE）。
const SERVER = process.env.OUTFIT_SERVER_ORIGIN || 'http://127.0.0.1:8766';

export default defineConfig({
  plugins: [react()],
  // 相对路径：妙搭可能把静态站点挂在子路径下，用 './' 才能找到 assets。
  base: './',
  resolve: {
    alias: {
      '@outfit/shared': path.resolve(__dirname, '../../packages/shared/src/index.ts'),
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: SERVER, changeOrigin: false },
      '/healthz': { target: SERVER, changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
});
