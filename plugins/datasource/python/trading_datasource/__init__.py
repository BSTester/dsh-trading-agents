"""统一数据层：全仓库唯一的富途 MCP 客户端、行情路由与回测核心。

模块划分：
  futu_mcp   富途远程 MCP 客户端（唯一的 JSON-RPC 握手/会话/重试实现）
  market     行情路由（富途优先，A股长历史走新浪）+ 本地缓存
  backtest   回测核心（策略、成本建模、绩效指标）
  locate     跨插件定位兄弟插件的 python 脚本

导入方式：
  from trading_datasource.market import load_bars
  from trading_datasource.futu_mcp import call_tool, FutuUnavailable

安装器把本包解包到 $DSH_HOME/trading-python/datasource，并向交易 venv 写入
dsh-trading-python.pth，因此任意插件脚本都能直接 import，无需 PYTHONPATH。
仓库开发布局下用 PYTHONPATH=plugins/datasource/python 达到同样效果。
"""
__all__ = ["backtest", "futu_mcp", "locate", "market"]

# 刻意不做 eager 导入：futu_mcp 需要 urllib/ssl，而纯本地路径（回测、风控、离线测试）
# 不应因为 import 本包就付出网络栈的代价，更不应在禁网沙箱里因 socket 被替换而失败。
# 用法统一为显式子模块导入：
#   from trading_datasource.market import load_bars
#   from trading_datasource import futu_mcp
__version__ = "0.1.0"
