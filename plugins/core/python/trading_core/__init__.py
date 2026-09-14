"""trading-core：量化平台核心库（规格 docs/superpowers/specs/2026-09-14-quant-platform-design.md）。

子模块：store（PIT 存储）/ calendar（交易日历）/ sync（同步）/ quality（质量）/ cli（单入口）。
本包不是 Harness 插件：安装器把 python/ 解到 $DSH_HOME/trading-python/core/ 并写入 .pth。
"""

__version__ = "0.1.0"
