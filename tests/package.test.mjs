import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";

for (const directory of ["engine", "fin-data", "workbench"]) {
  test(`${directory} distributes source entries without local bytecode`, async () => {
    const root = new URL(`../plugins/${directory}/`, import.meta.url);
    const manifest = JSON.parse(await readFile(new URL("package.json", root), "utf8"));
    const npm = process.platform === "win32" ? "cmd.exe" : "npm";
    const args = ["pack", "--dry-run", "--ignore-scripts", "--json"];
    const [packed] = JSON.parse(execFileSync(npm, process.platform === "win32" ? ["/d", "/c", "npm", ...args] : args,
      { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }));
    const paths = packed.files.map(file => file.path);
    assert.equal(paths.some(file => file.endsWith(".pyc")), false);
    assert.ok(paths.includes(manifest.main));
    if (directory === "workbench") {
      // WP7 面板退役：不再有 ./client 导出；Host 服务锚 + patch 行仍是分发内容
      assert.equal(manifest.exports["./client"], undefined);
      assert.equal(manifest.dsh.client, undefined);
      assert.equal(manifest.dsh.bundle?.patch, "./cordis.patch.yml");
      assert.ok(paths.includes("cordis.patch.yml"));
      const patch = await readFile(new URL("cordis.patch.yml", root), "utf8");
      assert.match(patch, /insert:/);
      assert.match(patch, /name: '@bstester\/dsh-trading-workbench'/);
    }
  });
}
