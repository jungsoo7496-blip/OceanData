import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 개발 서버는 API를 같은 출처로 프록시한다 (운영은 nginx.conf)
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/v1': 'http://localhost:8000' } },
})
