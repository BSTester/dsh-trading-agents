# dsh-quant（量化引擎，P2/P3）

确定性量化组件（纯 Python，可独立本地验证），供后续 Host 插件调度。

## backtest.py —— 回测引擎

```bash
python backtest.py --ticker 600519 --source akshare --strategy ma_cross --fast 5 --slow 20
python backtest.py --ticker 0700.HK --source stooq --strategy rsi
```

- 策略：`ma_cross`（双均线金叉/死叉）、`rsi`（超卖买/超买卖）
- 成本建模：佣金 0.03% 双边 + 印花税 0.1%（卖）+ 滑点 0.1% 双边
- 指标：总收益/年化/夏普/最大回撤/胜率/交易次数

## 数据源状态（重要）

| 源 | 状态 |
|---|---|
| `sina` | ✅ A股日线（新浪源，已验证：茅台/平安 896 根真实数据回测通过） |
| `synth` | ✅ 合成数据，引擎逻辑单测用（已验证） |
| `akshare`（东财日线） | ⚠️ 东财端点限流；`sina` 为替代首选 |
| `stooq` | ❌ 已上 JS 浏览器验证 |
| `yahoo` | ❌ 后端 query2 本机不可达 |
| 富途 MCP K线工具 | ⚠️ `quote_history_kline`/`quote_cur_kline` 裸 HTTP 返回 internal error；快照 `quote_stock_quote` 正常 |

**结论**：引擎逻辑已验证；生产数据入口待定——优先排查富途 K线工具为何 error
（可能需 dsh-mcp-client 的完整 SDK/SSE 握手，而非裸 HTTP），次选给 akshare 加重试/备用源。


## engine.py —— 量化交易闭环（P3 骨架）

```bash
python engine.py signal  --ticker 600519 --strategy rsi
python engine.py decide  --ticker 600519 --strategy rsi --apply   # 意图→本地台账
python engine.py report
```

- 信号（sina 真实数据）→ 风控（单笔风险 1% + ATR 2x 止损 + 最大持仓数 + 整手）→
  下单意图 → 本地台账成交 → 复盘（权益/收益/胜率）
- **账户隔离**：读 trade_mode 开关，sim/live 各有独立台账，实测不混用
- 已验证闭环：BUY→持仓→SELL 全流程，费用正确、权益守恒
- 待接：真实下单经会话内 MCP 工具（sim_trade_* / trading_*）+ 定时调度（timer）
