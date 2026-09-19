// V3 工作台（Ant Design Pro 版）契约测试：钉住「与设计稿版一一对应」的结构不变量。
// 不渲染 React（无 jsdom），只做源码级断言：菜单分组、页面集合、取数通道、无占位数据。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");
const read = (rel) => readFileSync(join(SRC, rel), "utf8");

// 设计稿版的 9 页 + 分组（监控/研究/交易/系统）——两版必须一致
const GROUPS = [
  ["监控", ["overview", "brain"]],
  ["研究", ["market", "strategy"]],
  ["交易", ["risk", "execution"]],
  ["系统", ["gateway", "tools", "settings"]],
];
const PAGES = GROUPS.flatMap(([, keys]) => keys);
const DESIGN_PAGES = ["index", "brain", "market", "strategy", "risk", "execution", "gateway", "tools", "settings"];

test("菜单分组与页面集合与设计稿版一致（4 组 9 页）", () => {
  const app = read("app.jsx");
  for (const [cat] of GROUPS) {
    assert.ok(app.includes(`cat: "${cat}"`), `app.jsx 缺分组 ${cat}`);
  }
  for (const key of PAGES) {
    assert.ok(app.includes(`key: "${key}"`), `app.jsx 缺页面 ${key}`);
  }
  assert.equal((app.match(/key: "/g) ?? []).length, PAGES.length, "PAGES 数量不符（应为 9 页）");
});

test("9 个页面文件都存在且各自导出一个组件", () => {
  const files = readdirSync(join(SRC, "pages")).filter((name) => name.endsWith(".jsx"));
  assert.deepEqual(files.sort(), PAGES.map((key) => `${key}.jsx`).sort());
  for (const key of PAGES) {
    const source = read(join("pages", `${key}.jsx`));
    assert.match(source, /export default function/, `${key}.jsx 未导出默认组件`);
  }
});

test("页面只经既有两条通道取数（/api/v3/* 读、/api/wb/* 写），无绝对地址", () => {
  for (const key of PAGES) {
    const source = read(join("pages", `${key}.jsx`));
    assert.ok(/useV3\(|getV3\(/.test(source), `${key}.jsx 未取数`);
    assert.ok(!/https?:\/\/(?!127\.0\.0\.1|localhost)/.test(source), `${key}.jsx 出现外部绝对地址`);
    assert.ok(!/from\s+["'](?!react|antd|@ant-design|dayjs|\.\.\/|\.\/)/.test(source), `${key}.jsx 引入了未声明依赖`);
  }
  const api = read("services/api.js");
  assert.ok(api.includes("/api/v3/"), "api.js 缺 /api/v3 通道");
  assert.ok(api.includes("/api/wb/"), "api.js 缺 /api/wb 通道");
});

test("写动作只在显式调用时发起（无自动写、无轮询写）", () => {
  const api = read("services/api.js");
  // postWb 只能在事件/hook 调用中被触发；useV3（读）不得调用 postWb
  const useV3Body = api.slice(api.indexOf("export function useV3"), api.indexOf("export function useWbAction"));
  assert.ok(!useV3Body.includes("postWb("), "useV3 不应发起写请求");
  for (const key of PAGES) {
    const source = read(join("pages", `${key}.jsx`));
    const writes = (source.match(/postWb\(|getV3\(.*POST/g) ?? []).length;
    if (writes === 0) continue;
    // 有写动作的页面必须把写调用包在事件处理/显式函数里，且不得出现在 useEffect 顶层自动执行
    assert.ok(/onClick|handle|confirm|submit/i.test(source), `${key}.jsx 有写动作但看不到人工触发的处理函数`);
  }
});

test("页面源码不出现「示例」字样", () => {
  for (const key of PAGES) {
    const source = read(join("pages", `${key}.jsx`));
    assert.equal((source.match(/示例/g) ?? []).length, 0, `${key}.jsx 出现「示例」`);
  }
});

test("无数据源措辞统一（noSourceText 可用；页面不自造第三种说法）", () => {
  const api = read("services/api.js");
  assert.ok(api.includes("noSourceText"), "api.js 缺 noSourceText 统一措辞工具");
  for (const key of PAGES) {
    const source = read(join("pages", `${key}.jsx`));
    // 只允许两种说法：统一工具，或字面「无数据源」；不得出现「暂无数据」「演示」等自造措辞
    assert.ok(!/暂无数据|演示数据|mock|dummy/i.test(source), `${key}.jsx 出现非统一措辞`);
  }
});

test("项目不引入设计稿版之外的新运行时依赖", () => {
  const pkg = JSON.parse(readFileSync(join(HERE, "..", "package.json"), "utf8"));
  const runtime = Object.keys(pkg.dependencies ?? {}).sort();
  assert.deepEqual(runtime, ["@ant-design/pro-components", "antd", "dayjs", "react", "react-dom"]);
  assert.ok(existsSync(join(SRC, "components", "charts.jsx")), "缺少共享图表组件");
});

test("设计稿版与工作台两套页面文件都在（两版并存）", () => {
  const designDir = join(HERE, "..", "..", "web", "public", "v3");
  for (const page of DESIGN_PAGES) {
    assert.ok(existsSync(join(designDir, `${page}.html`)), `设计稿版缺 ${page}.html`);
    assert.ok(existsSync(join(designDir, `${page}.js`)), `设计稿版缺 ${page}.js`);
  }
  assert.ok(existsSync(join(designDir, "shared.js")), "设计稿版缺 shared.js");
});
