import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = fileURLToPath(new URL('.', import.meta.url))

// 多页构建：h5（买家侧）与 console（售后指挥台）。
// seller.html 仍是免构建静态页，不参与本工程构建。
export default defineConfig({
  plugins: [vue()],
  // 相对基址：构建产物放在 /ui/dist/ 下可直接加载，也便于换目录部署
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        h5: `${root}h5.html`,
        console: `${root}console.html`,
      },
    },
  },
})
