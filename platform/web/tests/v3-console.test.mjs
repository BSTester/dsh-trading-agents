// V3 控制台（设计稿原样页面 + 数据 binder）的静态契约测试。
//
// 为什么有这一套：既有 AntD 工作台删除后，`platform/web` 不再有 React/antd 构建，
// 只剩 `public/v3/**`（由 FastAPI 直接服务）。这套测试替代原来的 408 例组件测试，
// 钉住的是「设计稿原样 + 真实数据注入」这条链路的**不变量**：
//   1) 9 页设计稿 HTML 存在，且只多两行注入脚本（结构未被改动）；
//   2) 每页都有 binder，且 binder 只调用同源 /api/v3/* 与 /api/wb/*（不直连外部域名）；
//   3) 控制台不引入任何第三方运行时依赖（无 React/antd import，无 CDN script）；
//   4) 设计稿页面里不再残留「示例」字样（设计稿原文含占位声明，注入后由 binder 改写）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const V3 = join(HERE, "..", "public", "v3");

const PAGES = ["index", "brain", "market", "strategy", "risk", "execution", "gateway", "tools", "settings"];
const read = (name) => readFileSync(join(V3, name), "utf8");

test("设计稿 9 页原样存在，且只注入两行脚本", () => {
  for (const page of PAGES) {
    const html = read(`${page}.html`);
    assert.match(html, /<!DOCTYPE html>/i, `${page}.html 不是完整文档`);
    assert.ok(html.includes('<link rel="stylesheet"') || html.includes("<style"), `${page}.html 缺少样式`);
    assert.ok(html.includes('<script src="shared.js"></script>'), `${page}.html 缺 shared.js 注入`);
    assert.ok(html.includes(`<script src="${page}.js" defer></script>`), `${page}.html 缺本页 binder 注入`);
    const injected = (html.match(/<script src="(shared|[a-z]+)\.js"/g) ?? []).length;
    assert.ok(injected <= 2, `${page}.html 注入脚本超过两行（${injected}）——设计稿可能被改动`);
  }
});

test("设计稿结构锚点仍在（顶栏 + 侧栏 + 设计稿 token），证明用的是设计稿自身标记", () => {
  for (const page of PAGES) {
    const html = read(`${page}.html`);
    // 设计稿各页侧栏是同一套设计的不同变体（有 .sidenav/.nav-cat/.nav-item 的不同组合），
    // 故只要求「至少一种导航结构标记」存在，而不是每页都齐全。
    const navMarkers = ["sidenav", "nav-cat", "nav-item", "nav-group"].filter((m) => html.includes(m));
    assert.ok(navMarkers.length >= 1, `${page}.html 缺设计稿导航结构（${navMarkers.join("/")}）`);
    assert.ok(html.includes("topbar"), `${page}.html 缺设计稿顶栏 topbar`);
    assert.ok(html.includes("--bg:#0b0e13") || html.includes("--bg: #0b0e13"), `${page}.html 缺设计稿 token`);
  }
});

test("每页都有 binder，且只调用同源接口（无绝对 URL / 无 CDN）", () => {
  for (const page of PAGES) {
    const js = read(`${page}.js`);
    assert.ok(js.length > 500, `${page}.js 内容过短，可能未实现`);
    assert.ok(js.includes("window.V3"), `${page}.js 未使用共享工具 V3`);
    assert.ok(/\/api\/v3\//.test(js), `${page}.js 未调用 /api/v3/*`);
    const absolute = js.match(/https?:\/\/[^\s"'`)]+/g) ?? [];
    for (const url of absolute) {
      // 允许注释里出现的文档链接；不允许真实请求外部域名
      assert.ok(!/fetch\(|api\(|post\(/.test(js.slice(Math.max(0, js.indexOf(url) - 40), js.indexOf(url))), `${page}.js 直接请求外部地址：${url}`);
    }
    assert.ok(!/from\s+["']react["']|antd|@ant-design/.test(js), `${page}.js 引入了第三方 UI 依赖`);
  }
});

test("共享工具导出约定的 API", () => {
  const shared = read("shared.js");
  for (const key of ["api", "post", "num", "money", "signed", "stamp", "setText", "nodata", "replaceWith", "svgLine"]) {
    assert.ok(shared.includes(`${key}`), `shared.js 缺少 ${key}`);
  }
  assert.ok(shared.includes("window.V3"), "shared.js 未挂载 window.V3");
});

test("设计稿里的占位文案都有改写机制（binder 直接改写或用 shared.demoSweep）", () => {
  const shared = read("shared.js");
  const sweeper = shared.includes("demoSweep");
  for (const page of PAGES) {
    const html = read(`${page}.html`);
    const js = read(`${page}.js`);
    const htmlHits = (html.match(/示例/g) ?? []).length;
    if (htmlHits === 0) continue;                       // 该页设计稿本身没有占位文案
    const handled = (js.match(/示例/g) ?? []).length > 0 || (sweeper && /demoSweep\(/.test(js));
    assert.ok(handled, `${page}：设计稿含 ${htmlHits} 处「示例」，但 binder 没有改写机制`);
  }
});

test("public/v3 目录只包含设计稿页面、binder 与共享工具", () => {
  const files = readdirSync(V3).sort();
  const allowed = new Set([...PAGES.map((p) => `${p}.html`), ...PAGES.map((p) => `${p}.js`), "shared.js"]);
  for (const file of files) {
    assert.ok(allowed.has(file), `public/v3 出现未预期的文件：${file}`);
  }
  assert.equal(files.length, allowed.size, "public/v3 文件数不符合预期");
});

test("binder 对失败路径有处理（不得让页面白屏）", () => {
  for (const page of PAGES) {
    const js = read(`${page}.js`);
    assert.ok(/\.ok\b/.test(js), `${page}.js 未检查接口 ok 字段`);
    assert.ok(/catch\(/.test(js) || /nodata\(/.test(js), `${page}.js 缺少失败处理`);
  }
});

test("控制台不再依赖 dist 构建产物（public/ 即服务目录）", () => {
  // 设计稿页面直接放在 public/v3，由 FastAPI 从 public/ 提供；不应再有 dist 专属引用
  for (const page of PAGES) {
    const html = read(`${page}.html`);
    assert.ok(!/\/assets\//.test(html), `${page}.html 引用了构建产物 assets/`);
    assert.ok(existsSync(join(V3, `${page}.js`)), `${page}.js 缺失`);
  }
});
