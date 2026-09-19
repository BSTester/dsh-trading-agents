"""platform 服务端测试包（``server.*`` 的单元/契约测试）。

存在的意义只有一条：让 ``python -m unittest tests.test_v3_sources`` 与
``python -m unittest discover -s tests`` 都能导入。所有测试都必须注入假 fetch / 假三方
模块，**不打网络**（见 ``test_v3_sources.BlockRealNetwork``）。
"""
