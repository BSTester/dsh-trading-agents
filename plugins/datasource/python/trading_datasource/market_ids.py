"""模拟账户市场口径（**唯一实现**，零依赖模块）。

为什么单独一个模块：模拟账户的 ``market_id`` 数字口径（港股 1 / A 股 3 / 美股 100）
被三处需要——``trading_datasource.channel``（REST 参数翻译）、
``trading_core.broker``（sim 账户选择，历史上是镜像常量）与锁定测试。
放在 ``market.py`` 会把 ``futu_mcp`` 的导入期会话风险带进来（``market.py`` 顶层导入
``futu_mcp``），放这里则只是一个纯常量模块，谁都能安全导入。

数字来源（2026-09-16 实测，非文档推断）：``GET /api/v1.0/sim-trade/accounts`` 返回
``accounts[]{account_id, market_id, account_title}``，实测样本——``9393`` 港股模拟账户
``market_id=1``、``3182575`` A 股模拟账户 ``market_id=3``、``11587526`` 美股融资融券模拟
账户 ``market_id=100``；另有港股期权 ``9``、期货 ``10/11/12/13``、日股 ``16``。
官方模拟交易文档**未给** market_id 枚举表，故此处只登记实测确认的三个市场口径；
未知 id 一律交给后端判定（本层不编造枚举）。
"""

#: 市场链 → 模拟账户 market_id（实测口径；SH/SZ/BJ 同为 A 股 3）
SIM_MARKET_IDS = {"SH": 3, "SZ": 3, "BJ": 3, "HK": 1, "US": 100}


def sim_market_id(value):
    """把 ``market`` 归一为模拟账户 market_id（int）。

    * ``int``（或数字字符串）→ 原样返回：调用方已从账户列表拿到 market_id
      （``trading_core.broker._sim_positions_and_equity`` 与
      ``platform/server/trading.py`` 的 sim 账户解析就是这条路径）；
    * 市场链名（SH/SZ/BJ/HK/US）→ ``SIM_MARKET_IDS`` 查表：``execute.run`` 传的
      ``o["market"]`` 是交易所前缀，两条调用路径的差异在此收口；
    * 其余（None/未知链名/非数字）→ ``None``。**不猜**：调用方拿到 None 时如实
      缺参失败（后端报 ``missing required parameter: market``），不伪造市场。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    if text in SIM_MARKET_IDS:
        return SIM_MARKET_IDS[text]
    if text.isdigit():
        return int(text)
    return None
