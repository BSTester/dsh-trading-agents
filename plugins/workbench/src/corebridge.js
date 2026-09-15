// trading-core 快照读取：Host 一律经 pycore 子命令取数，绝不直读 SQLite。
// 三个只读子命令（snapshot-plan / snapshot-schedule / snapshot-reconcile）
// 由 trading_core/cli.py 提供，JSON 输出经 pycore 解析。
import { pycore } from "./pycore.js";

export function createCoreBridge({ call = pycore } = {}) {
  return {
    plan() {
      return call(["snapshot-plan"]);
    },
    schedule() {
      return call(["snapshot-schedule"]);
    },
    reconcile() {
      return call(["snapshot-reconcile"]);
    },
  };
}
