import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
    plugins: [react()],
    server: {
        port: 3000,
        proxy: {
            '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true,
                rewrite: function (path) { return path.replace(/^\/api/, ''); }
            }
        }
    },
    build: {
        emptyOutDir: false,
        rollupOptions: {
            output: {
                manualChunks: {
                    'react-vendor': ['react', 'react-dom', 'react-router'],
                    'mui-vendor': ['@mui/material', '@mui/icons-material', '@emotion/react', '@emotion/styled'],
                    'data-vendor': ['swr']
                }
            }
        }
    },
    test: {
        environment: 'jsdom',
        setupFiles: './src/test/setup.ts',
        css: true
    }
});
