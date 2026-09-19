// V3.0 平台配置：环境变量优先，其余复用既有项目的配置文件（只读，不回写）。
// 密钥类值一律不落日志、不进配置对象——只保留「是否存在 / 有效期」等元信息。
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

function readTrim(file) {
  try {
    const value = fs.readFileSync(file, 'utf8').trim()
    return value === '' ? null : value
  } catch {
    return null
  }
}

export function loadConfig(env = process.env, home = env.DSH_HOME || path.join(os.homedir(), '.dsh')) {
  // 配置来源与运行时 home 分离：容器化部署时把宿主机配置目录只读挂进来，
  // 用 QUANT_CONFIG_HOME 指向它，而 DSH_HOME 仍是运行时自己的 home（凭据库/会话）。
  const configHome = env.QUANT_CONFIG_HOME || home
  const legacy = readJson(path.join(configHome, 'trading-platform.json')) || {}
  const futuToken = readTrim(path.join(configHome, 'futu-token'))
  const futuTokenExpiry = readTrim(path.join(configHome, 'futu-token-expiry'))
  const futuOpenapi = readJson(path.join(configHome, 'futu-openapi.json'))

  const config = {
    env,
    home,
    configHome,
    service: {
      host: env.QUANT_V3_HOST || '127.0.0.1',
      port: Number(env.QUANT_V3_PORT || 8407),
      // 部署角色（规格 §5.1 服务拆分）：同一镜像按角色挂载子系统，可独立扩缩容
      //   all       单机全功能（默认，保持既有行为）
      //   gateway   HTTP API + MCP 工具面（不含调度循环与静态 UI）
      //   scheduler 调度循环 + Headless 通道（只提供 /healthz）
      //   web       静态 UI + 只读 API（不含调度循环，MCP 仍可调用）
      role: env.QUANT_V3_ROLE || 'all',
    },
    // 既有工作台（数据来源，只读消费；交易确认边界也在那边）
    workbench: {
      base: env.WORKBENCH_BASE || `http://127.0.0.1:${legacy?.service?.port ?? 8397}`,
      timeoutMs: Number(env.WORKBENCH_TIMEOUT_MS || 30000),
    },
    // 三通道
    channels: {
      mcp: { transport: env.QUANT_MCP_TRANSPORT || 'stdio', enabled: true },
      sdk: {
        enabled: env.QUANT_SDK_ENABLED === '1',
        dshBin: env.QUANT_SDK_BIN || env.DSH_BIN || 'dsh',
        argv: undefined,
        profile: env.QUANT_SDK_PROFILE || 'sdk',
        provider: env.QUANT_SDK_PROVIDER || 'deepseek-official',
        model: env.QUANT_SDK_MODEL || 'deepseek-flash',
        reasoningEffort: env.QUANT_SDK_EFFORT || 'high',
        maxTokens: Number(env.QUANT_SDK_MAX_TOKENS || 49152),
        cwd: env.QUANT_SDK_CWD || process.cwd(),
        // SDK 运行时独立的 DSH_HOME（缺省用 config.home）——sdk profile 是 shipped profile，可直接引导
        home: env.QUANT_SDK_HOME || undefined,
        // 握手不需要模型密钥（真实运行时 initialize 可在无密钥下成功）；只有会话需要，故默认不预检
        requireKey: env.QUANT_SDK_REQUIRE_KEY === '1',
        note: 'sdk 为 shipped profile（dsh --profile sdk，@deepseek-ai/dsh-sdk-app），握手无需密钥；session/prompt 需要模型密钥',
      },
      headless: {
        enabled: true,
        dshBin: env.DSH_BIN || 'dsh',
        profile: env.QUANT_HEADLESS_PROFILE || 'headless',
        timeoutMs: Number(env.QUANT_HEADLESS_TIMEOUT_MS || 300000),
        concurrency: Number(env.QUANT_HEADLESS_CONCURRENCY || 3),
        tokenBudgetPerCall: Number(env.QUANT_HEADLESS_TOKEN_BUDGET || 200000),
      },
    },
    // 数据来源（全部保留，只声明与探测，不改动）
    sources: {
      futu: {
        channel: legacy.futu_channel || 'openapi',
        mcpTokenPresent: futuToken !== null,
        mcpTokenExpiry: futuTokenExpiry,
        openapiMode: futuOpenapi?.mode ?? null,
        openapiConfigKeys: Object.keys(futuOpenapi || {}),
      },
      watchlist: Array.isArray(legacy.watchlist) ? legacy.watchlist : [],
      autoPipeline: legacy.auto_pipeline || null,
    },
    dataDir: env.QUANT_V3_DATA || path.join(process.cwd(), 'data'),
    webDir: path.resolve(path.dirname(decodeURIComponent(new URL(import.meta.url).pathname)), '../web'),
  }
  return Object.freeze(config)
}

export default loadConfig
