#!/usr/bin/env bash
# WP5 kill switch 演练（薄封装）：内核 drills.run_kill_drill 完成
# 「建 kill 文件 → 断言风控拒单（规则 1）→ 清除」，本脚本只定位 venv 与
# 仓库根并打印 JSON。用法：scripts/drills.sh [kill文件路径]
#   缺省 kill 路径 = ${DSH_HOME:-~/.dsh}/trading-kill（与风控/daemon 同一路径）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PYTHON="$DSH_HOME/trading-venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3
exec "$PYTHON" -B -c 'import json, sys
sys.path.insert(0, sys.argv[1])
from trading_core import drills
print(json.dumps(drills.run_kill_drill(sys.argv[2]), ensure_ascii=False, indent=2))' \
  "$ROOT/plugins/core/python" "${1:-$DSH_HOME/trading-kill}"
