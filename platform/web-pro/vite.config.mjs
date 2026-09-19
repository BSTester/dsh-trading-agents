import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base=/pro/：构建产物由 FastAPI 在 /pro/* 下直接服务（与设计稿版 /v3/ 并存）
export default defineConfig({
  base: "/pro/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 3000 },
});
