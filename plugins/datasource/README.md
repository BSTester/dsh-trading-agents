# @bstester/dsh-datasource —— 统一数据层

这不是 Harness 插件（没有 cordis 行，不会出现在工具列表里），而是一个**被各插件共用的 Python 库**。

## 为什么需要它

重构前同一件事有多份实现，改一处不会传导到另一处：

| 能力 | 重构前的副本 | 现状 |
|---|---|---|
| 行情路由 `load_bars` | `workbench/bars.py` + `engine/market_data.py`（91 行逐字相同） | 只有 `market.py` |
| 富途 MCP 客户端 | `futu_client.py` + `bars.py` + `market_data.py` + `sources.py` 各一份 | 只有 `futu_mcp.py` |
| 回测核心 | `engine/backtest.py` + `workbench/backtest.py`（278 行，仅差一行 import） | 只有 `backtest.py` |

净删除约 700 行重复代码。真正的收益不是行数，而是**富途侧变更只需改一处**。

## 模块

| 模块 | 职责 |
|---|---|
| `futu_mcp` | 富途远程 MCP 客户端：JSON-RPC 握手、会话复用、退避重试、错误归一化、凭证探测 |
| `market` | 行情路由（富途优先，A 股长历史走新浪）、符号归一化、本地缓存回退 |
| `backtest` | 回测核心：策略、成本建模、绩效指标 |
| `locate` | 跨插件定位兄弟插件的 python 脚本（量化侧据此复用 fin-data 的情绪渠道） |
| `fundamentals` | ROE/ROA 备用源（Yahoo Finance，A 股再退 AKShare）——富途无资产负债表接口 |

## 安装后如何被找到

安装器把本包的 `python/` 解包到 `$DSH_HOME/trading-python/datasource/`，并向交易 venv 写入
`site-packages/dsh-trading-python.pth`。因此**任意插件脚本都能直接 import，不需要设置 PYTHONPATH**：

```python
from trading_datasource.market import load_bars
bars, source, stale = load_bars("00700.HK", "1d", 300)
```

仓库开发布局下等价做法：

```bash
PYTHONPATH=plugins/datasource/python python plugins/workbench/python/bars.py --ticker AAPL
```

> 该目录由安装器生成，仓库里的 `plugins/datasource/python` 是**唯一事实来源**；
> 重装即刷新，因此不存在人工同步导致的漂移。

## 富途返回信封与凭证续期

服务端同时存在**两种返回信封**，必须都认：

| 工具类别 | 信封 |
|---|---|
| 行情类 `quote_*` | `{"ret_code": 0, "data": {...}}` |
| 账户类 `account_*` | `{"s": "ok", "d": {...}}` |

只认一种会把另一种的成功响应判成失败——实测 `account_authorized_trd_accs` 因此报
`ret=None None`，拿不到 `acc_id`，**持仓必然读不出来**。

access_token 的官方有效期是 `expires_in = 7200`（2 小时）。过期时服务端对所有工具返回
`internal error` 而**不是 401**，所以 `call_tool` 遇到该特征会先用 refresh_token 自动续期
并重试一次（`auto_refresh=False` 可关闭）；续期成功会记下新的到期时刻供状态页显示。
业务错误（如 `ret=-3 invalid parameter`）不会触发续期，避免掩盖真实原因。

## 刻意不做的两件事

- **不 eager 导入子模块**。`import trading_datasource` 不会拉起 `urllib`/`ssl`；
  纯本地路径（回测、风控、离线测试）不应为网络栈付出代价，也不应在禁网沙箱里因 `socket` 被替换而失败。
- **不在缺数据时返回占位值**。渠道不可用一律明确报「不可用」：`load_bars` 抛错、
  `read_sentiment` 返回 `available: False`、`locate` 返回 `None`。工作台因此宁可为空也不显示假数据。

## 测试

`tests/test_data_layer.py`（离线，42 例）覆盖：单一实现断言（旧副本文件不得复活、
MCP 端点不得再出现在别处、两插件必须解析到同一个函数对象）、路由优先级、
全部数据源失败时必须报错而非返回空、富途错误语义（`"no data"` 是合法空结果而非异常）、
凭证探测（`internal error` 不得被当作 token 有效）、跨插件定位缺失时返回 `None`、
「带不带情绪，信号与 ATR 必须完全一致」这条可复现性性质，
以及两种返回信封的解析与凭证自动续期（含"业务错误不得触发续期"）。
