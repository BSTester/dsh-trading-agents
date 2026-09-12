// @bstester/dsh-fin-data — unified financial data tools for DeepSeek Harness.
//
// Registers two native model tools backed by vendored Python scripts
// (executed with the trading-venv interpreter when available):
//
//   fin_news(ticker, name?, count?, lang?)    Futu flash news → AKShare (A-share)
//                                              → Yahoo RSS (HK/US), auto-routed
//   fin_sentiment(ticker, x_query?, count?)   X via logged-in dedicated browser
//                                              (CDP) + AKShare 千股千评 (A-share)
//
// Routing, degradation and error handling live in code — the model just calls.

import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";

const run = promisify(execFile);
const pkgRoot = path.dirname(fileURLToPath(new URL(".", import.meta.url)));

/** trading-venv 解释器路径（安装器创建；缺失则报可读错误）。 */
function venvPython() {
  return path.join(
    os.homedir(), ".dsh", "trading-venv",
    process.platform === "win32" ? "Scripts\\python.exe" : "bin/python",
  );
}

async function runPython(script, args) {
  const scriptPath = path.join(pkgRoot, "python", script);
  const py = venvPython();
  try {
    const { stdout } = await run(py, [scriptPath, ...args], {
      timeout: 200_000,
      maxBuffer: 8 * 1024 * 1024,
    });
    const json = stdout.slice(stdout.indexOf("{"));
    return JSON.parse(json);
  } catch (error) {
    const detail = error.stderr ? String(error.stderr).slice(0, 300) : error.message;
    throw new Error(`fin-data ${script} failed（检查 ~/.dsh/trading-venv 是否已装依赖，或重跑仓库 install.sh）: ${detail}`);
  }
}

function renderJson(_args, value) {
  return [{ type: "text", text: JSON.stringify(value) }];
}

export const name = "fin-data";
export const inject = ["tools"];

export function apply(ctx) {
  ctx.tools.register({
    name: "fin_news",
    description:
      "获取财经快讯/新闻（多源自动路由：富途快讯 → AKShare(A股) → Yahoo RSS(港美股)，任一源失败自动降级并在 sources_status 标注）。返回 items[]（source/title/url/time）。",
    parameters: {
      ticker: { type: "string", required: true, description: "标的代码，如 00700.HK / AAPL / 600519" },
      name: { type: "string", description: "公司名（可选，富途快讯关键词更准，如 腾讯/Apple）" },
      count: { type: "number", description: "条数，默认 8，最大 20" },
      lang: { type: "string", description: "富途快讯语言：zh-CN/zh-HK/en，默认 zh-HK" },
    },
    output: { render: renderJson },
    async execute(args) {
      const a = ["--ticker", String(args.ticker), "--count", String(Math.min(args.count ?? 8, 20))];
      if (args.name) a.push("--name", String(args.name));
      a.push("--lang", String(args.lang ?? "zh-HK"));
      return await runPython("fin_news.py", a);
    },
  });

  ctx.tools.register({
    name: "fin_sentiment",
    description:
      "获取标的市场情绪/舆情：X 实时讨论（经本机已登录的专属浏览器，不可达自动跳过）+ A股千股千评（综合得分/关注指数/机构参与度）。返回 x.items[] 与 a_share_comment。",
    parameters: {
      ticker: { type: "string", required: true, description: "标的代码，如 TSLA / 600519" },
      x_query: { type: "string", description: "X 搜索词（建议英文，如 '$TSLA OR Tesla'）；默认用 ticker" },
      count: { type: "number", description: "X 推文条数，默认 8" },
    },
    output: { render: renderJson },
    async execute(args) {
      const a = ["--ticker", String(args.ticker), "--count", String(Math.min(args.count ?? 8, 20))];
      if (args.x_query) a.push("--x-query", String(args.x_query));
      return await runPython("fin_sentiment.py", a);
    },
  });
}
