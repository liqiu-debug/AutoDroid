import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  build: {
    // element-plus 为全量引入（main.js），拆分后仍约 1MB（gzip ~326kB）；
    // 该 chunk 与 echarts chunk 均为内容稳定的三方库包，可被浏览器长期缓存，
    // 故将告警阈值放宽到 1MB。业务入口 chunk 已降至 ~95kB。
    chunkSizeWarningLimit: 1024,
    rollupOptions: {
      output: {
        // 大体积三方库拆独立 chunk：减小入口体积并提升缓存命中
        manualChunks: {
          'element-plus': ['element-plus', '@element-plus/icons-vue'],
          'echarts': ['echarts', 'vue-echarts']
        }
      }
    }
  },
  server: {
    proxy: {
      '/api/report-assets': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
        ws: true
      },
      '/ws': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
        changeOrigin: true
      }
    }
  }
})
