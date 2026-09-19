# 量化交易决策平台 V3.0（Harness-Centric）——单容器全功能镜像
#
# 为什么用 glibc 基础镜像：容器内要跑 dsh 运行时（Headless / SDK 通道），而官方
# node:22-alpine 是 musl，缺 glibc 下的 node-pty 原生模块，dsh 会直接抛
#   Failed to load native module: pty
# （实测）。bookworm-slim 下 dsh 可正常加载插件树。
#
# dsh 本身通过挂载复用宿主机已装好的那份（含预编译原生模块），
# 避免在 aarch64 上现场编译 node-pty：
#   -v ~/.npm/_npx/<hash>:/npx:ro  且  DSH_BIN=/npx/node_modules/.bin/dsh
# 若你的环境可联网编译，也可改成 RUN npm install -g @deepseek-ai/dsh。
FROM node:22-bookworm-slim

WORKDIR /app
ENV NODE_ENV=production
COPY package.json ./
COPY server ./server
COPY web ./web
# 运行时数据 + dsh 运行时 home：具名卷默认归 root，容器以 node 运行，
# 必须预建并授权，否则实测报 EACCES mkdir '/dsh-runtime/profiles/headless'
RUN mkdir -p /app/data /dsh-runtime && chown -R node:node /app /dsh-runtime
USER node
ENV QUANT_V3_HOST=0.0.0.0
ENV QUANT_V3_PORT=8407
ENV QUANT_V3_DATA=/app/data
ENV QUANT_V3_ROLE=all
ENV DSH_BIN=/npx/node_modules/.bin/dsh
EXPOSE 8407
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.QUANT_V3_PORT||8407)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server/index.mjs"]
