"""仓库数据层路径解析：**唯一实现**，core 子进程与平台服务两处共用。

## 为什么需要这个模块

venv 的 ``dsh-trading-python.pth`` 指向**安装器解出的副本**
（``$DSH_HOME/trading-python/{datasource,core}``）。``subprocess`` 起的是**新解释器**，
不继承父进程的 ``sys.path``，因此在仓库内开发时子进程会解析到**旧副本**——表现为
「代码改了、修复不生效」（实测：新增子命令报 ``invalid choice``；副本 md5 与仓库不一致）。

本模块把「仓库在场则仓库优先，否则用副本」这一策略收敛成一份实现：服务进程的
``compute._subprocess_env`` 与 core 作业的 ``daemon._subprocess_runner`` 都调它，
不再各写一份（历史上有两份，语义靠人工同步，迟早漂移）。

## 语义（三条，缺一不可）

1. **存在才前置**：只把实际存在的仓库目录放进 ``PYTHONPATH``；
2. **保留既有项且不重复**：调用方原有的 ``PYTHONPATH`` 其它项必须在结果里保留，
   且已前置的目录不重复出现；
3. **仓库不在场 → 返回 None**：打包安装（只有副本）时返回 ``None``，调用方按
   ``subprocess`` 的默认行为（继承环境）走副本——生产语义与安装前完全一致。

## 与安装器的关系

副本是**安装时生成的产物**，不是人工同步的：改了 ``core``/``datasource``/``fin-data``
的代码后，若要**在无仓库的环境**生效，必须重跑安装/刷新（``scripts/platform_service.sh
refresh``）。有仓库的开发机上子进程直接吃仓库代码，不需要刷新。
"""
import os
from pathlib import Path

#: 仓库根的判定标记：`plugins/datasource/python` 存在即为仓库布局。
_REPO_MARKER = ("plugins", "datasource", "python")
#: 数据层默认纳入的目录（按前置顺序）——core 的作业子进程需要 datasource + core。
DEFAULT_LAYERS = ("datasource", "core")


def repo_root():
    """仓库根；当前代码跑在安装副本里（无仓库布局）时返回 ``None``。

    由 ``__file__`` 逐级上溯并检查标记目录，而不是数 ``parents[n]``：布局调整时
    前者仍然正确，后者会静默指到别的目录。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent.joinpath(*_REPO_MARKER)).is_dir():
            return parent
    return None


def repo_layer_dirs(layers=DEFAULT_LAYERS):
    """仓库里实际存在的数据层目录（绝对路径字符串，按 ``layers`` 给定顺序）。

    仓库不在场或某个层目录不存在时，该层不出现——**只前置真实存在的目录**。
    """
    root = repo_root()
    if root is None:
        return []
    out = []
    for name in layers:
        path = root / "plugins" / name / "python"
        if path.is_dir():
            out.append(str(path))
    return out


def repo_pythonpath_env(layers=DEFAULT_LAYERS):
    """子进程环境：仓库数据层前置到 ``PYTHONPATH``；无仓库时返回 ``None``。

    返回的 dict 是 ``os.environ`` 的副本（只改 ``PYTHONPATH``）；``None`` 表示
    「按默认继承环境走」，不要把 ``None`` 误当空环境传给 ``subprocess``。
    """
    prefix = repo_layer_dirs(layers)
    if not prefix:
        return None
    existing = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(prefix + [part for part in existing
                                                  if part not in prefix])
    return env


def repo_script(relative):
    """仓库内某个脚本的路径（如 ``plugins/fin-data/python/fin_sentiment.py``）。

    仓库不在场或脚本不存在 → ``None``（调用方据此回落安装副本路径）。
    ``relative`` 用 ``/`` 分隔，跨平台由 ``Path`` 归一。
    """
    root = repo_root()
    if root is None:
        return None
    path = root.joinpath(*str(relative).split("/"))
    return path if path.is_file() else None


__all__ = ["DEFAULT_LAYERS", "repo_root", "repo_layer_dirs", "repo_pythonpath_env",
           "repo_script"]
