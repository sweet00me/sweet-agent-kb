
/**
 * 文件：vite.config.ts
 *
 * 用途：
 *   1. 配置 Vite 前端开发服务器。
 *   2. 启用 React 和 Tailwind CSS。
 *   3. 将前端 /api 请求代理到 FastAPI 后端。
 *
 * 开发通信流程：
 *   iPhone → Vite:5173 → FastAPI:8010
 *
 * 注意：
 *   FastAPI 必须在 127.0.0.1:8010 正常运行。
 */

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { cpSync, mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

// 开发和构建都从已安装依赖生成资源，不向外部 CDN 发送文档请求。
const pdfAssets = new URL('./public/pdfjs/', import.meta.url)
mkdirSync(fileURLToPath(pdfAssets), { recursive: true })
for (const directory of ['cmaps', 'standard_fonts', 'wasm']) {
  cpSync(
    fileURLToPath(new URL(`./node_modules/pdfjs-dist/${directory}/`, import.meta.url)),
    fileURLToPath(new URL(`${directory}/`, pdfAssets)),
    { recursive: true },
  )
}

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],

  // 开发环境 API 代理
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
      },
    },
  },
})
