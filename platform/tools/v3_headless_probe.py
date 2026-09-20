#!/usr/bin/env python
"""Headless Runner 真机探针（FR-GATEWAY-003/004、FR-MON-003 的**真机证据**生产线）。

它做三件事，全部用**独立 DSH_HOME**（默认 ``~/.dsh/headless-probe``，绝不碰线上 ``~/.dsh``）：

  1. 用平台自己的 :class:`server.v3_headless.HeadlessRunner` 真跑一次最小 headless 任务
     （默认「只回复 OK，不要调用任何工具」）；
  2. 打印 **stdout / stderr / exit code / duration / token 估算 / outcome**（原样，不加工）；
  3. 回读平台库 ``headless_log``，把**落库行原文**打出来（FR-MON-003 的落库证据）。

用法::

    cd platform
    ~/.dsh/trading-venv/bin/python tools/v3_headless_probe.py \
        --dsh-home ~/.dsh/headless-probe --profile quant-headless \
        [--prompt "只回复 OK，不要调用任何工具"] [--require-whitelist]

前置（一次性，见 ``platform/install/quant-headless/README.md``「方式 A」）：独立 DSH_HOME 里
要有 ``profiles/quant-headless/``（profile 文件 + hooks.json + tool-whitelist.json）、
``profiles/node_modules``（共享包软链）与凭据 ``.credentials.yaml``。

退出码：0 = 真机跑通且落库成功；1 = 起进程被拦（白名单/缺 profile，属**配置问题**）；
2 = headless 运行本身失败（exit≠0/超时/超预算）；3 = 落库行读不回来。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import v3_db, v3_headless  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsh-home", default=os.environ.get("QUANT_HEADLESS_PROBE_DSH_HOME")
                        or str(Path.home() / ".dsh" / "headless-probe"),
                        help="子进程的 DSH_HOME（独立；默认 ~/.dsh/headless-probe）")
    parser.add_argument("--profile", default="quant-headless")
    parser.add_argument("--prompt", default="只回复 OK，不要调用任何工具")
    parser.add_argument("--task-type", default="manual")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--token-budget", type=int, default=200000)
    parser.add_argument("--home", default="",
                        help="平台台账目录（默认新建临时目录，不碰线上 ~/.dsh/v3.db）")
    parser.add_argument("--require-whitelist", action="store_true", default=True)
    parser.add_argument("--allow-unverified-profile", dest="require_whitelist",
                        action="store_false",
                        help="排障用逃生口：跳过白名单校验（**默认不加**）")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="v3-headless-probe-"))
    dsh_home = str(Path(args.dsh_home).expanduser())
    v3_db.init_db(home)
    config = json.loads(json.dumps(v3_headless.DEFAULT_CONFIG))
    config.update({"profile": args.profile, "dshHome": dsh_home,
                   "timeoutSeconds": args.timeout, "tokenBudget": args.token_budget,
                   "requireWhitelist": bool(args.require_whitelist)})
    runner = v3_headless.HeadlessRunner(str(home), config=config)
    print(f"[probe] platform home = {home}")
    print(f"[probe] DSH_HOME      = {dsh_home}")
    print(f"[probe] dsh           = {runner.params()['dshBin']} "
          f"({runner.params()['dshBinSource']})")
    print(f"[probe] profile dir   = {runner.profile_dir()}")
    ok, detail = v3_headless.verify_whitelist(runner.profile_dir())
    print(f"[probe] whitelist     = {ok} {detail.get('error') or ''}")
    record = runner.execute(args.prompt, task_type=args.task_type, trigger="manual")
    print("[probe] ---- call record ----")
    for key in ("outcome", "success", "exit_code", "kill_reason", "killed", "signal",
                "duration_ms", "tokens_estimate", "token_estimate_note", "stdout",
                "stderr", "error", "streams_separated"):
        value = record.get(key)
        if key in ("stdout", "stderr") and isinstance(value, str) and len(value) > 2000:
            value = value[:2000] + f"...（截断，共 {len(value)} 字符，库里有全文）"
        print(f"[probe] {key} = {value!r}")
    print("[probe] ---- DB row (headless_log) ----")
    rows = v3_db.list_events(home, "headless_log", limit=1)
    if not rows:
        print("[probe] 落库失败/无行 —— FR-MON-003 未满足")
        return 3
    print(json.dumps(rows[0], ensure_ascii=False, indent=2, sort_keys=True)[:6000])
    if record.get("outcome") in ("disabled", "profile-missing", "whitelist-unverified",
                                 "dsh-not-found"):
        return 1
    if record.get("success"):
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
