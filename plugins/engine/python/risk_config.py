#!/usr/bin/env python3
"""风控参数配置（单一事实来源）。

文件：~/.dsh/trading-risk.json（缺失时使用默认值，不自动写盘直到显式设置）。
所有字段都在读取时校验，非法配置直接报错——风控参数静默失效比报错更危险。

用法（命令行）:
  python risk_config.py show
  python risk_config.py set risk_per_trade=0.005 max_positions=3
  python risk_config.py reset
"""
import json
import math
import os
import sys
from pathlib import Path

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
CONFIG_PATH = DSH / "trading-risk.json"

DEFAULTS = {
    "risk_per_trade": 0.01,      # 单笔风险占权益比例
    "stop_atr_mult": 2.0,        # 止损 = 入场价 − N×ATR
    "max_positions": 5,          # 最大同时持仓数
    "daily_loss_limit_pct": 0.03,  # 单日亏损熔断阈值（占权益）
    "max_position_pct": 0.25,    # 单一标的最大仓位占权益比例
}

LIMITS = {
    "risk_per_trade": (0.001, 0.05),
    "stop_atr_mult": (0.5, 6.0),
    "max_positions": (1, 20),
    "daily_loss_limit_pct": (0.005, 0.2),
    "max_position_pct": (0.05, 1.0),
}


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("风控配置必须是对象")
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"未知风控字段：{', '.join(sorted(unknown))}")
    result = dict(DEFAULTS)
    result.update(config)
    for key, value in result.items():
        low, high = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} 必须是有限数值")
        if key == "max_positions":
            if int(value) != value:
                raise ValueError("max_positions 必须是整数")
            value = int(value)
        if not (low <= value <= high):
            raise ValueError(f"{key} 超出允许范围 {low}..{high}")
        result[key] = value
    return result


def load():
    """读取生效风控参数；文件缺失或非法时抛错（非法即拒绝交易，不静默降级）。"""
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"风控配置无法解析：{error}")
    return validate(raw)


def save(config):
    DSH.mkdir(parents=True, exist_ok=True)
    validated = validate(config)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(validated, indent=1, ensure_ascii=False))
    os.chmod(temp, 0o600)
    temp.replace(CONFIG_PATH)
    return validated


def main():
    args = sys.argv[1:]
    if not args or args[0] == "show":
        source = str(CONFIG_PATH) if CONFIG_PATH.exists() else "(默认值，未落盘)"
        print(json.dumps({"config": load(), "source": source, "defaults": DEFAULTS},
                         ensure_ascii=False))
        return 0
    if args[0] == "set":
        patch = {}
        for item in args[1:]:
            if "=" not in item:
                print(json.dumps({"error": f"参数格式应为 key=value：{item}"}, ensure_ascii=False)); return 1
            key, value = item.split("=", 1)
            try:
                patch[key.strip()] = float(value) if key.strip() != "max_positions" else int(value)
            except ValueError:
                print(json.dumps({"error": f"无法解析数值：{item}"}, ensure_ascii=False)); return 1
        try:
            print(json.dumps({"config": save({**load(), **patch})}, ensure_ascii=False))
        except ValueError as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False)); return 1
        return 0
    if args[0] == "reset":
        CONFIG_PATH.unlink(missing_ok=True)
        print(json.dumps({"config": dict(DEFAULTS), "source": "(已重置为默认值)"}, ensure_ascii=False))
        return 0
    print(json.dumps({"error": "用法：show | set key=value... | reset"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
