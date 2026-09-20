import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base=/：构建产物由 FastAPI 在**根路径**直接服务（设计稿原样版已删除）
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 3000 },
});
