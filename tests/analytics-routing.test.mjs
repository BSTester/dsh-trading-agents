// 工作台 provider → python 脚本的路由测试。
//
// 为什么需要：`sources` / `events` / `factors` / `ic` / `sensitivity` 五个 provider
// 曾长期调用 `analytics.py <子命令>`，而 `analytics.py` 的子命令只有
// {equity, positions, correlation, risk, trades} —— 运行时表现为 argparse 报
// `invalid choice`，**接口从来没成功过**。这类错误在代码里完全看不出来，
// 只有真跑一次才会暴露，所以用这组测试把路由钉死。
//
// 全部离线：exec 用桩替换，python 脚本只读源码分析，不联网。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { createAnalyticsProvider } from "../plugins/workbench/src/analytics.js";

const PYTHON_DIR = fileURLToPath(new URL("../plugins/workbench/python/", import.meta.url));

/** 记录每次 exec 的 (脚本名, 参数)，并返回空 JSON，使 provider 走完自己的解析。 */
function recorder() {
  const calls = [];
  const exec = async (_python, argv) => {
    calls.push({ script: path.basename(argv[0]), args: argv.slice(1) });
    // 让返回体带上脚本名，便于断言"错误信息里的脚本名也是对的"
    return { stdout: JSON.stringify({ ok: true }) };
  };
  return { calls, exec };
}

function provider(calls, exec) {
  return createAnalyticsProvider({ exec, python: () => "python", now: () => 1 });
}

/** 每个 provider 期望执行的脚本，以及首个参数（子命令或选项）。 */
const EXPECTED = {
  equity: ["analytics.py", "equity"],
  positions: ["positions.py", "--mode"],
  correlation: ["analytics.py", "correlation"],
  risk: ["analytics.py", "risk"],
  trades: ["analytics.py", "trades"],
  sensitivity: ["sensitivity.py", "--ticker"],
  events: ["events.py", "--ticker"],
  factors: ["factors.py", "snapshot"],
  ic: ["factors.py", "ic"],
  sources: ["sources.py", null],          // 无子命令，且默认不带参数
  quality: ["quality.py", "--ticker"],
  instrument: ["instruments.py", "--ticker"],
};

/** 各 provider 的最小合法入参。 */
const PAYLOADS = {
  equity: { mode: "sim" },
  positions: { mode: "sim" },
  correlation: { tickers: ["600519", "000001"], window: 120 },
  risk: {},
  trades: { mode: "sim", limit: 50 },
  sensitivity: { ticker: "600519" },
  events: { ticker: "00700.HK", days: 400 },
  factors: { tickers: ["600519", "000001"], window: 250 },
  ic: { tickers: ["600519", "000001", "601318"], factor: "mom_20", window: 250 },
  sources: {},
  quality: { ticker: "US.AAPL" },
  instrument: { ticker: "00700.HK" },
};

test("每个 provider 都把参数交给正确的脚本", async () => {
  for (const [name, [expectedScript, expectedFirst]] of Object.entries(EXPECTED)) {
    const { calls, exec } = recorder();
    await provider(calls, exec)[name](PAYLOADS[name]);
    assert.equal(calls.length, 1, `${name} 应恰好执行一次子进程`);
    assert.equal(calls[0].script, expectedScript, `${name} 执行了错误的脚本`);
    if (expectedFirst !== null) {
      assert.equal(calls[0].args[0], expectedFirst, `${name} 首个参数不对`);
    } else {
      assert.deepEqual(calls[0].args, [], `${name} 不应带位置参数`);
    }
  }
});

test("期望表覆盖了全部 provider（新增接口必须同步登记）", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/analytics.js", import.meta.url), "utf8");
  // 注意 risk() 没有参数，正则不能要求 (payload
  const declared = [...source.matchAll(/^    async (\w+)\(/gm)].map((m) => m[1]).sort();
  assert.deepEqual(declared, Object.keys(EXPECTED).sort(),
    "有 provider 没被这张表覆盖——新接口的路由同样需要断言");
});

test("analytics.py 的子命令只能来自它自己声明的集合", async () => {
  // 这条规则正是五个接口挂掉的原因：把不存在的子命令交给 analytics.py
  const source = await readFile(path.join(PYTHON_DIR, "analytics.py"), "utf8");
  const subcommands = new Set([...source.matchAll(/add_parser\(\s*"([^"]+)"/g)].map((m) => m[1]));
  assert.deepEqual([...subcommands].sort(),
    ["correlation", "equity", "positions", "risk", "trades"],
    "analytics.py 的子命令变了，请同步检查各 provider 的路由");

  for (const [name, [script, first]] of Object.entries(EXPECTED)) {
    if (script !== "analytics.py") continue;
    assert.ok(subcommands.has(first), `${name} 把 analytics.py 没有的子命令 ${first} 交给了它`);
  }
});

test("被执行的脚本都真实存在", async () => {
  const { readFile: read } = await import("node:fs/promises");
  for (const [name, [script]] of Object.entries(EXPECTED)) {
    const file = path.join(PYTHON_DIR, script);
    await assert.doesNotReject(read(file), `${name} 指向了不存在的脚本 ${script}`);
  }
});

test("无子命令脚本不得收到多余的位置参数", async () => {
  // sources.py / events.py / sensitivity.py 都是"选项式"CLI，多一个位置参数就会报
  // unrecognized arguments —— 与把子命令交给 analytics.py 是同一类错误。
  const optionOnly = ["sources.py", "events.py", "sensitivity.py", "instruments.py", "positions.py", "quality.py"];
  for (const [name, [script, first]] of Object.entries(EXPECTED)) {
    if (!optionOnly.includes(script)) continue;
    const { calls, exec } = recorder();
    await provider(calls, exec)[name](PAYLOADS[name]);
    assert.ok(calls[0].args[0] === undefined || calls[0].args[0].startsWith("--"),
      `${name} 给选项式脚本 ${script} 传了位置参数 ${calls[0].args[0]}`);
    assert.ok(first === null || String(first).startsWith("--"), `${name} 的期望表写法不对`);
  }
});

test("sources 请求探测时才不带 --no-probe", async () => {
  const { calls, exec } = recorder();
  await provider(calls, exec).sources({ no_probe: true });
  assert.deepEqual(calls[0].args, ["--no-probe"]);
});
