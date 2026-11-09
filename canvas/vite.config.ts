import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // lib 模式不会替换 process.env.NODE_ENV（直接把 React 的 dev 分支留在包里会崩在浏览器）
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
  build: {
    outDir: '../app/web/static/canvas',
    emptyOutDir: true,
    cssCodeSplit: false,
    lib: {
      entry: 'src/main.tsx',
      name: 'CanvasBundle',
      formats: ['iife'],
      fileName: () => 'canvas.js',
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        assetFileNames: (assetInfo) =>
          assetInfo.name && assetInfo.name.endsWith('.css') ? 'canvas.css' : 'assets/[name][extname]',
      },
    },
  },
})
