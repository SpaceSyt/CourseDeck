import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:48321',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (req) => req.setHeader('origin', 'http://127.0.0.1:48321'));
        },
      },
    },
  },
});
