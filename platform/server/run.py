"""服务入口（WP6 补遗 C）：``python -m server.run``（cwd = ``platform/``）。

对齐 Node 服务层原实现（已退役，见 git 历史 ``aaa5f42^``）的启动/就绪/优雅退出：
  * 监听成功后打印**单行 JSON**：ok/service/url/mcp/tools/auth
    （端口从真实 socket address 取，``port=0`` 时即内核分配的端口）；
  * 监听失败不悬挂，打印 ``{"ok": false, ...}`` 后以 1 退出；
  * SIGINT/SIGTERM 优雅退出：uvicorn 自带信号处理（等价于原实现的 ``server.close()``），
    ``timeout_graceful_shutdown=5`` 对应其 5s 强制回收。

**解释器要求（Q-8）**：服务应在 ``$DSH_HOME/trading-venv`` 内启动
（``~/.dsh/trading-venv/bin/python -m server.run``）。``compute.PYTHON`` 取
``sys.executable``，因此用系统 Python 启动服务会让所有分析/核心子进程改用系统解释器
——缺依赖时只在取数时才暴露，表现为满屏 ``trading/*-unavailable``。为免静默降级，
``main()`` 在解释器与 venv 不一致时先往 stderr 打一行告警 JSON（不硬失败：集成测试
与嵌入式调用要用系统 Python 注入替身，硬失败会挡住它们）。

启动方式说明（有意差异 1，必须记录）：任务书写的字面入口 ``python -m platform.server.run``
在本环境**不可能工作**——``platform`` 是标准库的**模块**（``/usr/lib/python3*/platform.py``），
``ModuleNotFoundError: 'platform' is not a package``，即使在仓库根目录执行同样失败
（已用最小目录复现）。因此沿用本仓库既有约定：把 ``platform/`` 放进 ``sys.path`` 后以
``server`` 包组装（``import platform.server…`` 会被标准库遮蔽——见
tests/test_wp6_service_locks.py 与 A/B 模块的 ``from server import _js``）。运行方式：

    ~/.dsh/trading-venv/bin/python -m server.run     # cwd = platform/（推荐）
    python -c "import sys; sys.path.insert(0, 'platform'); from server import run; run.main()"

下面的自举让 ``python platform/server/run.py`` 与上面两种方式加载**同一份** ``server`` 包。
"""
import json
import os
import sys
from pathlib import Path

PLATFORM = str(Path(__file__).resolve().parents[1])
if PLATFORM not in sys.path:  # 自举：与「sys.path 加 platform/ 后 from server import ...」同源
    sys.path.insert(0, PLATFORM)

# 数据层优先取**仓库内**代码（WP8 修正）：从仓库运行服务时，venv 的
# ``dsh-trading-python.pth`` 会把 ``$DSH_HOME/trading-python/{datasource,core}``
# 加进 sys.path——那是安装器解出的**副本**，新增模块/修复不会自动同步
# （实测踩两次：缺 ``futu_openapi``、缺 ``AppSigner.sign_ws``，表现为服务端
# 取数/推送报 ModuleNotFound/AttributeError 而仓库测试全绿）。
# 仓库存在时把仓库路径插到最前，保证「代码版本 = 数据层版本」；打包安装
# （无仓库）场景仍回落到 ``$DSH_HOME`` 副本。
REPO_ROOT = Path(PLATFORM).parent
for _dir in (REPO_ROOT / "plugins" / "datasource" / "python",
             REPO_ROOT / "plugins" / "core" / "python"):
    if _dir.is_dir() and str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import uvicorn  # noqa: E402

from server.app import create_app  # noqa: E402
from server.config import load_config  # noqa: E402

SERVICE = "quant-platform"


def tool_count():
    """工具面清单长度（Node 原实现（已退役）的 ``manifest.length``）。

    唯一事实来源是 ``mcp_tools.TOOLS``/``TOOL_COUNT``；清单读不到时如实报 0，让就绪行
    立刻暴露故障。**不再维护兜底字面量**——先前的 26 曾连续两轮没跟上工具面扩张
    （27→33），每加一个工具就要同步一个裸数字的「维护点」本身就是缺陷（2026-09 修订）。
    """
    try:
        from server import mcp_tools  # noqa: PLC0415
    except ImportError:
        return 0
    tools = getattr(mcp_tools, "TOOLS", None)
    if tools is None:
        return getattr(mcp_tools, "TOOL_COUNT", 0)
    try:
        return len(tools)
    except TypeError:
        return getattr(mcp_tools, "TOOL_COUNT", 0)


def ready_line(address, config):
    """就绪行（Node 原实现（已退役））：address 优先（``port=0`` 由内核分配端口）。"""
    host, port = config.get("host"), config.get("port")
    if address:
        host, port = address[0], address[1]
    base = f"http://{host}:{port}"
    return {
        "ok": True,
        "service": SERVICE,
        "url": base,
        "mcp": f"{base}/mcp",
        "tools": tool_count(),
        "auth": "token" if config.get("token") else "loopback-only",
    }


class ReadyServer(uvicorn.Server):
    """监听成功后立刻打印单行就绪 JSON（uvicorn 默认的 INFO 行被 log_level=warning 抑制）。"""

    def __init__(self, config, ready_config):
        super().__init__(config)
        self._ready_config = ready_config
        self.ready_line = None

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self.ready_line = ready_line(self.servers[0].sockets[0].getsockname(), self._ready_config)
            print(json.dumps(self.ready_line, ensure_ascii=False), flush=True)


def build_server(app, config, port=None):
    """按配置造 uvicorn 服务器（不启监听）：便于测试就绪行与 address 解析。

    ``port`` 可覆盖配置端口（测试里用 0 让内核分配，避免与真实服务撞端口）。
    """
    return ReadyServer(uvicorn.Config(app, host=config.get("host"),
                                      port=config.get("port") if port is None else port,
                                      log_level="warning", timeout_graceful_shutdown=5), config)


def interpreter_warning(home=None):
    """Q-8：``$DSH_HOME/trading-venv/bin/python`` 存在且与 ``sys.executable`` 不同 → 告警行。

    返回告警 dict（``main()`` 打到 stderr）或 ``None``。刻意不硬失败：测试/嵌入式调用会
    用系统 Python，但子进程仍按 ``sys.executable`` 行走注入替身（compute.PYTHON）。
    """
    venv_python = Path(home if home is not None
                       else os.environ.get("DSH_HOME") or Path.home() / ".dsh") \
        / "trading-venv" / "bin" / "python"
    try:
        if not venv_python.exists():
            return None
        same = os.path.realpath(str(venv_python)) == os.path.realpath(sys.executable)
    except OSError:  # 路径不可解析时不猜：宁可不告警
        return None
    if same:
        return None
    return {
        "level": "warning",
        "service": SERVICE,
        "message": f"服务未在 trading-venv 内启动：当前解释器 {sys.executable}，"
                   f"venv 解释器 {venv_python}；分析/核心子进程将使用当前解释器",
    }


def main(argv=None):
    del argv  # 入口无参数：配置全部来自 env / trading-platform.json（Node 原实现同）
    home = os.environ.get("DSH_HOME")
    warning = interpreter_warning(home)
    if warning is not None:
        print(json.dumps(warning, ensure_ascii=False), file=sys.stderr, flush=True)
    config = load_config(home)
    # 2026-09-21 修：此前不传 config → ``service.mcp_surface`` 等部署级配置在正常启动
    # 路径上读不到（只有显式传 config= 的测试才生效）。这里把 load_config 的结果传进去。
    app = create_app(home, config=config)
    server = build_server(app, config)
    try:
        server.run()
    except SystemExit as error:
        # uvicorn 0.53 的 bind 失败不是 OSError 而是 ``sys.exit(3)``（STARTUP_FAILURE）：
        # 捕 SystemExit 才能覆盖 EADDRINUSE 这条真实路径（Node 原实现已退役的等价物）。
        if error.code in (None, 0):  # 正常退出码不当失败
            return 0
        print(json.dumps({"ok": False, "service": SERVICE,
                          "error": f"服务启动失败（{config.get('host')}:{config.get('port')}）："
                                   f"uvicorn 退出码 {error.code}"}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return 1
    except OSError as error:  # 双保险：非 uvicorn 路径的绑定/权限失败同样友好退出
        print(json.dumps({"ok": False, "service": SERVICE, "error": str(error)},
                         ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
