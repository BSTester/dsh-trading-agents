// 指令目录的 Node 侧写入端（工作台 → daemon 唯一通道，规格 §8.2）。
// 协议必须与 trading_core/commands.py 完全一致：
//   白名单 5 种；文件名 = nonce.json；原子写（tmp + rename）防半文件。
// daemon 轮询 pending/ 处理后移入 processed/，Node 侧只写不读。
import { randomBytes } from "node:crypto";
import { mkdir, rename, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

// 与 trading_core.commands.COMMANDS 同一白名单；变更时两处同步。
export const COMMANDS = ["execute_plan", "cancel_plan", "kill", "unkill", "run_job"];

export function commandsDir(home) {
  return path.join(home, "trading-commands");
}

export async function writeCommand(home, type, payload = {}) {
  if (!COMMANDS.includes(type)) {
    throw new Error(`指令不在白名单：${type}`);
  }
  const pending = path.join(commandsDir(home), "pending");
  await mkdir(pending, { recursive: true });
  const nonce = randomBytes(16).toString("hex");
  const body = JSON.stringify({ type, nonce, ...payload });
  const target = path.join(pending, `${nonce}.json`);
  const tmp = `${target}.tmp`;
  await writeFile(tmp, body, "utf8");
  await rename(tmp, target);
  return nonce;
}
