"""仓库数据层路径解析与「子进程吃哪份代码」的端到端证据（2026-09-17 部署缺口修复）。

背景（实机取证）：venv 的 ``.pth`` 指向安装器解出的**副本**
（``$DSH_HOME/trading-python/{datasource,core}``），而 ``subprocess`` 起的是新解释器、
不继承父进程 ``sys.path`` → 作业链里的每条腿（``sync-bars``/``plan-auto``/``auto-execute``/
``reconcile-daily``/``sentiment-snapshot``…）都跑**旧副本代码**，表现为「修复不生效」。
修复：仓库在场则把仓库数据层前置到子进程 ``PYTHONPATH``（``repo_paths`` 单一实现，
core 与平台侧共用）；无仓库（生产）回落副本。

本文件里最重要的是 ``CrossProcessResolutionTest``：它**真起一个子进程**（不 mock）打印
``trading_core.__file__``，证明策略在跨进程边界真的成立——而不是只证明「env 字典拼对了」。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_datasource import repo_paths  # noqa: E402
from trading_core import daemon, sentiment  # noqa: E402


class RepoPathsTest(unittest.TestCase):
    def test_repo_root_and_layers(self):
        self.assertEqual(repo_paths.repo_root(), ROOT)
        dirs = repo_paths.repo_layer_dirs(("datasource", "core", "fin-data"))
        self.assertEqual(dirs[0], str(ROOT / "plugins" / "datasource" / "python"))
        self.assertEqual(dirs[1], str(ROOT / "plugins" / "core" / "python"))
        self.assertEqual(dirs[2], str(ROOT / "plugins" / "fin-data" / "python"))

    def test_layers_skip_missing_directory(self):
        # 不存在的层名不出现（「存在才前置」——不能把死路径塞进 PYTHONPATH）
        self.assertEqual(repo_paths.repo_layer_dirs(("datasource", "nope")),
                         [str(ROOT / "plugins" / "datasource" / "python")])

    def test_env_prepends_repo_and_preserves_existing(self):
        with unittest.mock.patch.dict(os.environ, {"PYTHONPATH": "/custom/other"}):
            env = repo_paths.repo_pythonpath_env()
        self.assertIsNotNone(env)
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(ROOT / "plugins" / "datasource" / "python"))
        self.assertEqual(parts[1], str(ROOT / "plugins" / "core" / "python"))
        self.assertIn("/custom/other", parts)

    def test_env_does_not_duplicate_repo_dirs(self):
        repo_dir = str(ROOT / "plugins" / "core" / "python")
        with unittest.mock.patch.dict(os.environ, {"PYTHONPATH": repo_dir}):
            parts = repo_paths.repo_pythonpath_env()["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts.count(repo_dir), 1, "已在前置列表里的目录不得重复出现")

    def test_env_is_plain_os_environ_copy(self):
        # 只改 PYTHONPATH，别把其它环境变量丢掉（子进程要靠 DSH_HOME 等）
        with unittest.mock.patch.dict(os.environ, {"DSH_MARKER_X": "1"}, clear=False):
            env = repo_paths.repo_pythonpath_env()
        self.assertEqual(env["DSH_MARKER_X"], "1")

    def test_no_repo_returns_none(self):
        # 打包安装（只有副本）语义：返回 None → subprocess 继承环境 → 走副本
        with unittest.mock.patch.object(repo_paths, "repo_root", lambda: None):
            self.assertEqual(repo_paths.repo_layer_dirs(), [])
            self.assertIsNone(repo_paths.repo_pythonpath_env())
            self.assertIsNone(repo_paths.repo_script("plugins/fin-data/python/fin_news.py"))

    def test_repo_script_hit_and_miss(self):
        self.assertEqual(repo_paths.repo_script("plugins/fin-data/python/fin_sentiment.py"),
                         ROOT / "plugins" / "fin-data" / "python" / "fin_sentiment.py")
        self.assertIsNone(repo_paths.repo_script("plugins/fin-data/python/absent.py"))


class DaemonRunnerEnvTest(unittest.TestCase):
    """`daemon._subprocess_runner` 必须把「仓库优先」的环境交给子进程。"""

    def _run_with_capture(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")

            class _Done:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Done()

        with tempfile.TemporaryDirectory() as home, \
                unittest.mock.patch.dict(os.environ, {"DSH_HOME": home}), \
                unittest.mock.patch.object(daemon.subprocess, "run", fake_run):
            daemon._subprocess_runner(["quality", "--market", "SH"])
        return captured

    def test_runner_passes_repo_first_env(self):
        captured = self._run_with_capture()
        self.assertEqual(captured["argv"][:3], [sys.executable, "-m", "trading_core"])
        env = captured["env"]
        self.assertIsNotNone(env, "仓库在场时必须给出仓库优先的 PYTHONPATH")
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(ROOT / "plugins" / "datasource" / "python"))
        self.assertEqual(parts[1], str(ROOT / "plugins" / "core" / "python"))

    def test_runner_passes_none_without_repo(self):
        # 生产语义：无仓库 → env=None → 继承环境 → 解析到安装副本（与修复前一致）
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env")

            class _Done:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Done()

        with tempfile.TemporaryDirectory() as home, \
                unittest.mock.patch.dict(os.environ, {"DSH_HOME": home}), \
                unittest.mock.patch.object(repo_paths, "repo_root", lambda: None), \
                unittest.mock.patch.object(daemon.subprocess, "run", fake_run):
            daemon._subprocess_runner(["quality", "--market", "SH"])
        self.assertIsNone(captured["env"])


class SentimentScriptPathTest(unittest.TestCase):
    """fin-data 脚本优先仓库；`_default_runner` 交给子进程的环境同样仓库优先。"""

    def test_scripts_for_prefers_repo(self):
        with tempfile.TemporaryDirectory() as home:
            paths, absent = sentiment.scripts_for(home)
        self.assertEqual(paths[sentiment.SOURCE_FIN_SENTIMENT],
                         ROOT / "plugins" / "fin-data" / "python" / "fin_sentiment.py")
        self.assertEqual(paths[sentiment.SOURCE_NEWS],
                         ROOT / "plugins" / "fin-data" / "python" / "fin_news.py")

    def test_scripts_for_falls_back_to_installed_copy(self):
        with tempfile.TemporaryDirectory() as home:
            installed = Path(home) / sentiment.FIN_DATA_DIR
            installed.mkdir(parents=True)
            (installed / "fin_sentiment.py").write_text("{}", encoding="utf-8")
            (installed / "fin_news.py").write_text("{}", encoding="utf-8")
            with unittest.mock.patch.object(repo_paths, "repo_root", lambda: None):
                paths, absent = sentiment.scripts_for(home)
        self.assertEqual(paths[sentiment.SOURCE_FIN_SENTIMENT],
                         installed / "fin_sentiment.py")
        self.assertIn(sentiment.SOURCE_LAST30DAYS, absent)

    def test_default_runner_env_prefers_repo_data_layer(self):
        captured = {}

        class _Proc:
            pid = 4242
            returncode = 0

            def communicate(self, timeout=None):
                return "{}", ""

            def poll(self):
                return 0

        def fake_popen(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return _Proc()

        with unittest.mock.patch.object(sentiment.subprocess, "Popen", fake_popen):
            sentiment._default_runner(["some-script.py"])
        parts = captured["env"]["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(ROOT / "plugins" / "datasource" / "python"))
        self.assertEqual(parts[1], str(ROOT / "plugins" / "fin-data" / "python"),
                         "fin-data 脚本自己也要从仓库走（脚本与数据层同版本）")


class ComputeSharesImplementationTest(unittest.TestCase):
    """平台侧不再是第二份实现：`compute._subprocess_env` 与 `repo_paths` 同结果。"""

    def test_compute_env_matches_shared_helper(self):
        sys.path.insert(0, str(ROOT / "platform"))
        from server import compute  # noqa: PLC0415
        self.assertEqual(compute._subprocess_env(), repo_paths.repo_pythonpath_env())


class CrossProcessResolutionTest(unittest.TestCase):
    """**关键证据**：真起子进程，证明它加载的是仓库代码（不是副本）。"""

    PROBE = "import trading_core, sys; print(trading_core.__file__)"

    def _probe(self, env):
        proc = subprocess.run([sys.executable, "-c", self.PROBE], capture_output=True,
                              text=True, env=env, timeout=60,
                              cwd=tempfile.gettempdir())  # cwd 移出仓库：排除 cwd 巧合
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_child_loads_repo_code_with_shared_env(self):
        resolved = self._probe(repo_paths.repo_pythonpath_env())
        expected = str(ROOT / "plugins" / "core" / "python")
        self.assertTrue(resolved.startswith(expected),
                        f"子进程应加载仓库 trading_core，实际 {resolved}")

    def test_without_env_child_loads_installed_copy(self):
        """反证：不给该环境时，子进程走 venv .pth 的**安装副本**——正是缺口本身。"""
        copy_root = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "trading-python"
        if not (copy_root / "core" / "trading_core").is_dir():
            self.skipTest("本机无安装副本，无法做反证（缺它即缺口不存在）")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        resolved = self._probe(env)
        self.assertTrue(resolved.startswith(str(copy_root / "core")),
                        f"无环境时应走安装副本，实际 {resolved}")

    def test_real_runner_executes_readonly_cli(self):
        """真跑一次 runner（不 mock subprocess）：只读快照命令应 exit 0 且给出 JSON 摘要。"""
        home = os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))
        with unittest.mock.patch.dict(os.environ, {"DSH_HOME": home}):
            result = daemon._subprocess_runner(["snapshot-schedule"])
        self.assertEqual(result["exit_code"], 0, result.get("tail"))
        self.assertIsInstance(result["summary"], dict)


if __name__ == "__main__":
    unittest.main()
