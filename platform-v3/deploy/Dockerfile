# 量化交易决策平台 V3.0（Harness-Centric）——零依赖 Node 镜像
# 角色由 QUANT_V3_ROLE 决定（all/gateway/scheduler/web），同一镜像多角色复用。
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
# 只拷运行所需：服务端 + 静态 UI（无第三方依赖，无需 npm install）
COPY package.json ./
COPY server ./server
COPY web ./web
# 运行时数据（headless 日志、OMS 台账、策略轮、审计）与 dsh 运行时 home 挂载卷。
# 注意：具名卷默认归 root，容器以 node 用户运行 → 必须预建目录并授权，否则 dsh 无法
# 创建 profiles/ 目录（实测报 EACCES: mkdir '/dsh-runtime/profiles/headless'）。
RUN mkdir -p /app/data /dsh-runtime && chown -R node:node /app /dsh-runtime
# scheduler 角色需要 dsh 运行时（容器内拉起 headless）：构建时加 --build-arg INSTALL_DSH=1
# 说明：默认不装，保持镜像精简；单机部署也可让 scheduler 跑在宿主机上复用既有 dsh。
ARG INSTALL_DSH=""
RUN if [ -n "$INSTALL_DSH" ]; then npm install -g @deepseek-ai/dsh && dsh --version; fi
USER node
ENV QUANT_V3_HOST=0.0.0.0
ENV QUANT_V3_PORT=8407
ENV QUANT_V3_DATA=/app/data
EXPOSE 8407
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.QUANT_V3_PORT||8407)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server/index.mjs"]
