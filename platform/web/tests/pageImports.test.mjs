// 页面级静态导入锁（E2E 回归钉，2026-09-17）。
//
// 症状：`overview.jsx` 在渲染里调用了 `fieldState(...)`（来自 `services/fieldState.js`），
// 但 import 列表漏了它——**vite build 不报错**（标识符在打包期不是错误）、单测也只测纯函数，
// 于是问题直到真浏览器打开页面才以 `ReferenceError` 白屏暴露（另一 E2E harness 实测）。
//
// 本测试做一件很窄的事：把 `src/services/**` 与 `src/charts/**` 的**导出名**收集起来，
// 对 `src/pages/**` 的每个页面断言「用到的导出名必须在 import 列表里」。它拦不住所有
// 运行期错误，但能拦住「用了却没导入」这一整类（本次事故的成因）。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "..", "src");

function filesIn(dir, suffix) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...filesIn(full, suffix));
    else if (name.endsWith(suffix)) out.push(full);
  }
  return out;
}

/** 收集某目录下所有导出名（`export function X` / `export const X` / `export {X}`）。 */
function exportedNames(dir) {
  const names = new Set();
  for (const file of filesIn(dir, ".js").concat(filesIn(dir, ".jsx"))) {
    const source = readFileSync(file, "utf8");
    for (const match of source.matchAll(/export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)/g)) {
      names.add(match[1]);
    }
    for (const match of source.matchAll(/export\s*\{([^}]*)\}/g)) {
      for (const piece of match[1].split(",")) {
        const name = piece.trim().split(/\s+as\s+/).pop().trim();
        if (name) names.add(name);
      }
    }
  }
  return names;
}

const SHARED = exportedNames(join(src, "services"));
for (const name of exportedNames(join(src, "charts"))) SHARED.add(name);

/** 页面是否以**具名导入**引入某标识符（任意路径）。 */
function importsName(source, name) {
  for (const match of source.matchAll(/import\s*\{([^}]*)\}\s*from\s*["'][^"']+["']/g)) {
    for (const piece of match[1].split(",")) {
      const imported = piece.trim().split(/\s+as\s+/)[0].trim();
      if (imported === name) return true;
    }
  }
  return false;
}

/** 去掉 import 段后，页面正文里是否**使用**了该标识符。 */
function usesName(source, name) {
  const body = source.replace(/^\s*import[\s\S]*?;?\s*$/gm, "");
  const pattern = new RegExp(`(^|[^\\w$.])${name}\\s*[({.[]`, "m");
  return pattern.test(body);
}

test("每个页面用到的 services/charts 导出名都已导入（防白屏 ReferencesError）", () => {
  const problems = [];
  for (const page of filesIn(join(src, "pages"), ".jsx")) {
    const source = readFileSync(page, "utf8");
    for (const name of SHARED) {
      if (usesName(source, name) && !importsName(source, name)) {
        problems.push(`${page.replace(src + "/", "")} 使用了 ${name} 但没有导入`);
      }
    }
  }
  assert.deepEqual(problems, [], problems.join("\n"));
});

test("锁本身能拦：构造一个「用了没导入」的源码样本", () => {
  const source = 'import { Card } from "antd";\nexport default function X() { return fieldState({}); }';
  assert.equal(importsName(source, "fieldState"), false);
  assert.equal(usesName(source, "fieldState"), true);
});
