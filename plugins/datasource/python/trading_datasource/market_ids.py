"""市场口径常量（**唯一实现**，零依赖模块）。

为什么单独一个模块：两套市场数字口径被多处需要，且历史上有镜像副本
（WP13 审查 M1 收敛——镜像靠锁定测试守着，属于「用测试维护重复」）：

* ``SIM_MARKET_IDS`` —— 模拟账户的 ``market_id``（港股 1 / A 股 3 / 美股 100），
  被 ``trading_datasource.channel``（REST 参数翻译）、``trading_core.broker``
  （sim 账户选择，历史上的镜像常量）与 ``platform/server/trading.py`` 需要；
* ``OPENAPI_ENABLE_MARKET`` —— OpenAPI 授权账户的 ``enable_market``
  （1=HK 2=US 4=ChinaStock），被 ``trading_core.broker``（live 账户匹配，历史镜像）
  与 ``platform/server/trading.py``（历史镜像）需要。

放在 ``market.py`` 会把 ``futu_mcp`` 的导入期会话风险带进来（``market.py`` 顶层导入
``futu_mcp``），放这里则只是一个纯常量模块，谁都能安全导入（含 core 与 platform）。

**两套数字不互相换算**：各自来自各自通道（见下），用途也不同。

``SIM_MARKET_IDS`` 来源（2026-09-16 实测，非文档推断）：``GET /api/v1.0/sim-trade/accounts``
返回 ``accounts[]{account_id, market_id, account_title}``，实测样本——``9393`` 港股模拟账户
``market_id=1``、``3182575`` A 股模拟账户 ``market_id=3``、``11587526`` 美股融资融券模拟
账户 ``market_id=100``；另有港股期权 ``9``、期货 ``10/11/12/13``、日股 ``16``。
官方模拟交易文档**未给** market_id 枚举表，故此处只登记实测确认的三个市场口径；
未知 id 一律交给后端判定（本层不编造枚举）。
"""

#: 市场链 → 模拟账户 market_id（实测口径；SH/SZ/BJ 同为 A 股 3）
SIM_MARKET_IDS = {"SH": 3, "SZ": 3, "BJ": 3, "HK": 1, "US": 100}

#: 市场前缀 → OpenAPI 授权账户的 enable_market
#: （官方 naming-dictionary#enable-market：1=HK 2=US 4=ChinaStock 5=Futures 6=SG
#: 12=CA 15=JP 18=KR；本仓库只登记已接入的三市场）。**与 SIM_MARKET_IDS 口径不同**，
#: 两套数字各自服务各自通道，禁止互相换算。
OPENAPI_ENABLE_MARKET = {"HK": 1, "US": 2, "SH": 4, "SZ": 4, "BJ": 4}


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
