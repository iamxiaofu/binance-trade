import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 生产构建产物输出到 dist/，由 FastAPI StaticFiles 托管在站点根路径。
// 开发时 `npm run dev` 把 /api 与 /ws 代理到本地 FastAPI(127.0.0.1:8000)。
export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
      '/healthz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
