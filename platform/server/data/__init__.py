"""平台侧**历史数据读取**子包（FR-DATA-003）。

规格 ``docs/v3-spec.md:288`` 点名「平台侧的 ``data/cache.py`` 作为唯一数据读取接口」。
本包就是那个落点：``platform/server/data/cache.py``。

为什么是 ``server/data/`` 而不是仓库根的 ``platform/data/``：本平台的 Python 侧只有
``platform/server`` 一个服务端源码目录（``server.app`` 的接线循环只 import
``server.<模块>``），把读取层放在 ``server/data/`` 才能被服务与测试**同一个**导入路径
加载（``from server.data import cache``）；放在 ``platform/data/`` 会变成一个需要额外
``sys.path`` 手术的旁路包。规格要求的是「唯一入口」这一**语义**与 ``data/cache.py``
这一**相对路径命名**，两者都满足。

只导出 :mod:`server.data.cache`；本 ``__init__`` 不重导出符号，避免出现「两套入口名」。
子模块**按需加载**（PEP 562）：``from server.data import cache`` 与 ``import server.data``
之后取 ``server.data.cache`` 都可用，同时不会在 ``python -m server.data.cache`` 之前先把
它 import 一遍（那会让 runpy 打出 ``found in sys.modules … unpredictable behaviour`` 警告）。
"""

__all__ = ["cache"]


def __getattr__(name):
    """按需导入子模块（``server.data.cache``）；其它名字照常报 ``AttributeError``。"""
    if name == "cache":
        import importlib

        return importlib.import_module("server.data.cache")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
