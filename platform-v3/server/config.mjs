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
  const legacy = readJson(path.join(home, 'trading-platform.json')) || {}
  const futuToken = readTrim(path.join(home, 'futu-token'))
  const futuTokenExpiry = readTrim(path.join(home, 'futu-token-expiry'))
  const futuOpenapi = readJson(path.join(home, 'futu-openapi.json'))

  const config = {
    env,
    home,
    service: {
      host: env.QUANT_V3_HOST || '127.0.0.1',
      port: Number(env.QUANT_V3_PORT || 8407),
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
        requireKey: true,
        note: '需要部署 dsh-sdk-app bundle（dsh --profile sdk，@deepseek-ai/dsh-sdk-app）与模型密钥；未就绪时状态如实标注 pending',
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
