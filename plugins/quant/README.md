# dsh-quant（旧路径兼容入口）

`python/engine.py` 和 `python/backtest.py` 只保留 CLI 转发，唯一实现在
`plugins/engine/python/`。Harness 通过 engine 插件提供信号、回测和报告预览；
工作台只展示结果，不调用本地成交或券商下单。以下命令供开发排障，不是第二个产品指令入口。

## backtest.py —— 回测引擎

```bash
python backtest.py --ticker 600519 --source akshare --strategy ma_cross --fast 5 --slow 20
python backtest.py --ticker 0700.HK --source stooq --strategy rsi
```

- 策略：`ma_cross`（双均线金叉/死叉）、`rsi`（超卖买/超买卖）
- 成本建模：佣金 0.03% 双边 + 印花税 0.1%（卖）+ 滑点 0.1% 双边
- 指标：总收益/年化/夏普/最大回撤/胜率/交易次数
- 信号在当根收盘后产生，下一根开盘成交；最后一根信号不成交。
- 买入预算包含费用并按整手取整；卖出收益扣除双边费用；期末持仓按收盘标记为未平仓，不伪造成交。
- 费用、整手和交易日规则是固定的 A 股模拟假设，不代表当前券商费率，也不适用于所有市场。

## 数据源状态（历史排查记录，不代表当前可用性）

| 源 | 状态 |
|---|---|
| `sina` | ✅ A股日线（新浪源，已验证：茅台/平安 896 根真实数据回测通过） |
| `synth` | ✅ 合成数据，引擎逻辑单测用（已验证） |
| `akshare`（东财日线） | ⚠️ 东财端点限流；`sina` 为替代首选 |
| `stooq` | ❌ 已上 JS 浏览器验证 |
| `yahoo` | ❌ 后端 query2 本机不可达 |
| 富途 MCP K线工具 | ⚠️ `quote_history_kline`/`quote_cur_kline` 裸 HTTP 返回 internal error；快照 `quote_stock_quote` 正常 |

**部署要求**：生产数据入口仍需在目标环境联调——优先排查富途 K线工具为何 error
（可能需 dsh-mcp-client 的完整 SDK/SSE 握手，而非裸 HTTP），次选给 akshare 加重试/备用源。


## engine.py —— 本地模拟台账，不是券商交易闭环

```bash
python engine.py signal  --ticker 600519 --strategy rsi
python engine.py decide  --ticker 600519 --strategy rsi --apply   # 意图→本地台账
python engine.py report
```

- 风险预算按现金与持仓市值之和只计算一次；ATR 使用包含前收盘跳空的真实波幅。
- 止损可独立于 HOLD/BUY 信号触发，但仅在调用时检查，不是实时保护；T+1 禁止当日买入后卖出，缺失买入日期也拒绝卖出。
- `decide --apply` 只修改 `DSH_HOME/quant-ledger.json` 的 sim 本地台账，带价格日期和 `local_simulation` 来源；日线观察价格不是可成交报价。
- live 模式拒绝 `decide` 和本地成交，报告返回账户数据不可用，不生成虚构资金、持仓或收益。
- 券商查询和下单仍由 Harness 中的 MCP 工具完成；模式切换、实盘批准、工具响应展示不等于成交确认、持续对账或无人值守闭环。
