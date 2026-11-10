import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import * as path from 'node:path';

const SERVER = process.env.OUTFIT_SERVER_ORIGIN || 'http://127.0.0.1:3001';

export default defineConfig({
  plugins: [react()],
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
