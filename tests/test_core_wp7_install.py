"""WP7 任务 5：平台安装器（scripts/install_platform.py）。

全部离线：不创建真实 venv、不跑 pip/npm、不起服务——执行路径用注入的 runner/probe
替身验证；幂等判断（venv 已存在、dist 比 src 新）与 Windows 分支用纯函数 + 临时目录测。
约定：main 每步输出一行 JSON 摘要 {"step","ok","detail"}，末尾多一行 step=summary。
"""
import importlib.util
import io
import json
import os
import socket
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "install_platform_under_test", _REPO / "scripts" / "install_platform.py")
ip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ip)


def _run_main(argv, runner=None, probe=None, health=None):
    """跑 main 并返回 (exit_code, stdout)。默认 runner/probe 一被调用就失败——
    干跑/跳过路径必须零副作用，替身被调用即说明有副作用泄漏。"""
    noisy_runner = runner or (lambda argv: (_ for _ in ()).throw(AssertionError(f"不该执行 {argv}")))
    noisy_probe = probe or (lambda port: (_ for _ in ()).throw(AssertionError(f"不该探测端口 {port}")))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = ip.main(argv, runner=noisy_runner, probe=noisy_probe, health=health)
    return code, buf.getvalue()


def _json_steps(out):
    """摘出 JSON 摘要行（{"step","ok","detail"}），按输出顺序返回。"""
    steps = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            row = json.loads(line)
            if isinstance(row, dict) and {"step", "ok", "detail"} <= set(row):
                steps.append(row)
    return steps


class FakeVenv:
    """临时 DSH_HOME，可选地摆一个假 venv 解释器（幂等用例用）。"""

    def __enter__(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="wp7-install-")
        self.home = Path(self._tmp.name)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def make_venv(self):
        py = ip.venv_python(self.home)
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("#!/bin/sh\nexit 0\n")
        return py


class ArgParsingTest(unittest.TestCase):
    def test_help_prints_usage_and_skip_flags(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(buf):
                ip.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = buf.getvalue()
        for needle in ("usage", "--home", "--skip-venv", "--skip-deps", "--skip-web",
                       "--skip-service", "--skip-all", "--dry-run"):
            self.assertIn(needle, text, f"--help 缺 {needle}")

    def test_home_is_required(self):
        with self.assertRaises(SystemExit) as caught:
            ip.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_flag_defaults_and_explicit_flags(self):
        args = ip.parse_args(["--home", "/tmp/x", "--skip-deps", "--dry-run"])
        self.assertEqual(args.home, "/tmp/x")
        self.assertTrue(args.skip_deps)
        self.assertTrue(args.dry_run)
        self.assertFalse(args.skip_venv)
        self.assertFalse(args.skip_web)
        self.assertFalse(args.skip_service)
        self.assertFalse(args.skip_all)

    def test_skip_all_turns_on_every_skip(self):
        args = ip.parse_args(["--home", "/tmp/x", "--skip-all"])
        self.assertTrue(args.skip_venv and args.skip_deps and args.skip_web
                        and args.skip_service and args.skip_all)


class VenvPythonPureFunctionTest(unittest.TestCase):
    """venv_python 是纯函数：Windows 分支在 Linux 上也能测。"""

    def test_windows_uses_scripts_python_exe(self):
        self.assertEqual(str(ip.venv_python("/h", is_windows=True)),
                         os.path.join("/h", "trading-venv", "Scripts", "python.exe"))

    def test_posix_uses_bin_python(self):
        self.assertEqual(str(ip.venv_python("/h", is_windows=False)),
                         os.path.join("/h", "trading-venv", "bin", "python"))

    def test_default_follows_current_os(self):
        home = "/some/home"
        self.assertEqual(str(ip.venv_python(home)),
                         str(ip.venv_python(home, is_windows=(os.name == "nt"))))

    def test_venv_dir_is_sibling_layout(self):
        self.assertEqual(ip.venv_dir(Path("/h")), Path("/h") / "trading-venv")


class BuildPlanTest(unittest.TestCase):
    def setUp(self):
        self.home = Path("/fake/home")
        self.plan = ip.build_plan(self.home, _REPO)

    def test_step_order_is_fixed(self):
        self.assertEqual([s["step"] for s in self.plan],
                         ["venv", "deps", "web", "service", "verify"])

    def test_every_step_has_summary_shape(self):
        for step in self.plan:
            self.assertIn("step", step)
            self.assertIn("skip", step)
            self.assertIn("commands", step)
            self.assertIn("why", step)

    def test_venv_command_creates_home_venv(self):
        argv = self.plan[0]["commands"][0]
        self.assertIn("-m", argv)
        self.assertIn("venv", argv)
        self.assertIn(str(ip.venv_dir(self.home)), argv)

    def test_deps_command_installs_repo_requirements_into_venv(self):
        argv = self.plan[1]["commands"][0]
        self.assertIn(str(ip.venv_python(self.home)), argv)
        self.assertIn("-m", argv)
        self.assertIn("pip", argv)
        self.assertIn("install", argv)
        self.assertIn("-r", argv)
        reqs = [a for a in argv if a.endswith("requirements.txt")]
        self.assertEqual(reqs, [str(_REPO / "platform" / "requirements.txt")])

    def test_web_commands_are_npm_install_then_build(self):
        install, build = self.plan[2]["commands"]
        self.assertEqual(install[:2], ["npm", "install"])
        self.assertIn("--prefix", install)
        self.assertIn("run", build)
        self.assertIn("build", build)
        self.assertIn("--prefix", build)

    def test_service_and_verify_never_auto_start(self):
        self.assertEqual(self.plan[3]["commands"], [], "service 步不自动常驻")
        self.assertEqual(self.plan[4]["commands"], [], "verify 步只探测不启动")

    def test_skip_flags_mark_steps_without_commands(self):
        plan = ip.build_plan(self.home, _REPO, skip_venv=True, skip_deps=True,
                             skip_web=True, skip_service=True)
        for step in plan:
            # verify 只读且免费，永不跳过（跳过其余步骤时它退化为一次 warning）
            self.assertTrue(step["skip"] or step["step"] == "verify", step["step"])
            self.assertEqual(step["commands"], [], step["step"])

    def test_partial_skip_only_marks_target_step(self):
        plan = ip.build_plan(self.home, _REPO, skip_deps=True)
        marks = {s["step"]: s["skip"] for s in plan}
        self.assertEqual(marks, {"venv": False, "deps": True, "web": False,
                                 "service": False, "verify": False})


class DryRunTest(unittest.TestCase):
    def test_dry_run_prints_plan_without_side_effects(self):
        with FakeVenv() as fake:
            code, out = _run_main(["--home", str(fake.home), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("[dry-run]", out)
            for needle in ("-m venv", "pip", "npm", "install", "build"):
                self.assertIn(needle, out, f"dry-run 计划缺 {needle}")
            steps = _json_steps(out)
            self.assertEqual([s["step"] for s in steps if s["step"] != "summary"],
                             ["venv", "deps", "web", "service", "verify"])
            self.assertTrue(all(s["ok"] for s in steps))
            self.assertFalse((fake.home / "trading-venv").exists(), "干跑不得建 venv")

    def test_dry_run_does_not_probe_ports(self):
        with FakeVenv() as fake:
            code, _ = _run_main(["--home", str(fake.home), "--dry-run"])
            self.assertEqual(code, 0)  # probe 替身未被调用（被调用即抛错）


class SkipAllTest(unittest.TestCase):
    def test_skip_all_executes_nothing_and_reports_skipped(self):
        with FakeVenv() as fake:
            probe_hits = []

            def probe(port):
                probe_hits.append(port)
                return False

            code, out = _run_main(
                ["--home", str(fake.home), "--skip-all"], probe=probe)
            self.assertEqual(code, 0)
            steps = {s["step"]: s for s in _json_steps(out)}
            for name in ("venv", "deps", "web", "service"):
                self.assertTrue(steps[name]["ok"], name)
                self.assertIn("skipped", steps[name]["detail"], name)
            self.assertIn("warning", steps["verify"]["detail"],
                          "verify 只读：无服务时出 warning，不算失败")
            self.assertEqual(probe_hits, [8397], "只有 verify 做一次只读端口探测")
            self.assertEqual(list(fake.home.iterdir()), [], "skip-all 不得产生任何副作用")

    def test_skip_all_dry_run_still_zero_side_effects(self):
        with FakeVenv() as fake:
            code, out = _run_main(["--home", str(fake.home), "--skip-all", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("[dry-run]", out)
            self.assertEqual(list(fake.home.iterdir()), [])


class IdempotenceTest(unittest.TestCase):
    def test_venv_exists_reports_already_exists_without_recreate(self):
        with FakeVenv() as fake:
            marker = fake.make_venv()
            calls = []
            code, out = _run_main(
                ["--home", str(fake.home), "--skip-deps", "--skip-web", "--skip-service"],
                runner=lambda argv: calls.append(argv) or 0, probe=lambda port: False)
            self.assertEqual(code, 0)
            steps = {s["step"]: s for s in _json_steps(out)}
            self.assertIn("already-exists", steps["venv"]["detail"])
            self.assertEqual(calls, [], "venv 已存在时不得重建")
            self.assertEqual(marker.read_text(), "#!/bin/sh\nexit 0\n", "既有 venv 不被改动")

    def test_web_needs_build_true_when_dist_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="wp7-web-") as tmp:
            self.assertTrue(ip.web_needs_build(Path(tmp)))

    def test_web_needs_build_false_when_dist_newer_than_src(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="wp7-web-") as tmp:
            web = Path(tmp)
            (web / "src").mkdir()
            (web / "dist").mkdir()
            (web / "src" / "app.jsx").write_text("export default 1;\n")
            (web / "dist" / "index.html").write_text("<html></html>\n")
            old = time.time() - 3600
            os.utime(web / "src" / "app.jsx", (old, old))
            self.assertFalse(ip.web_needs_build(web), "dist 比 src 新 → 跳过重建")

    def test_web_needs_build_true_when_src_newer_than_dist(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="wp7-web-") as tmp:
            web = Path(tmp)
            (web / "src").mkdir()
            (web / "dist").mkdir()
            (web / "src" / "app.jsx").write_text("export default 2;\n")
            (web / "dist" / "index.html").write_text("<html></html>\n")
            old = time.time() - 3600
            os.utime(web / "dist" / "index.html", (old, old))
            self.assertTrue(ip.web_needs_build(web), "src 改动比 dist 新 → 必须重建")

    def test_service_config_defaults_and_overrides(self):
        with FakeVenv() as fake:
            self.assertEqual(ip.service_config(fake.home),
                             {"port": 8397, "host": "127.0.0.1", "token": None})
            (fake.home / "trading-platform.json").write_text(
                json.dumps({"service": {"port": 9000, "host": "0.0.0.0", "token": "t"}}),
                encoding="utf-8")
            self.assertEqual(ip.service_config(fake.home),
                             {"port": 9000, "host": "0.0.0.0", "token": "t"})

    def test_service_config_invalid_port_falls_back(self):
        with FakeVenv() as fake:
            (fake.home / "trading-platform.json").write_text(
                json.dumps({"service": {"port": "abc"}}), encoding="utf-8")
            self.assertEqual(ip.service_config(fake.home)["port"], 8397)


class ExecutionPathTest(unittest.TestCase):
    """执行路径用注入 runner/probe/health 替身走通，不出网、不落副作用。"""

    def _prepared_home(self, fake):
        fake.make_venv()
        (fake.home / "web-src-placeholder").touch()
        return fake.home

    def test_full_run_with_fake_runner_succeeds(self):
        with FakeVenv() as fake:
            home = self._prepared_home(fake)
            web_dir = _REPO / "platform" / "web"
            calls = []

            def runner(argv):
                calls.append(list(argv))
                return 0

            code, out = _run_main(["--home", str(home)], runner=runner, probe=lambda port: False)
            self.assertEqual(code, 0)
            joined = [" ".join(c) for c in calls]
            self.assertTrue(any("-m venv" not in c and "pip" in c and "install" in c for c in joined),
                            f"deps 应跑 pip：{joined}")
            self.assertTrue(any(c.startswith("npm install") for c in joined), f"web 应装依赖：{joined}")
            # 真仓库 dist 通常比 src 新 → build 可跳过；只要 install 跑过即视为覆盖
            self.assertIn("--prefix", " ".join(joined))
            self.assertIn(str(web_dir), " ".join(joined))
            steps = {s["step"]: s for s in _json_steps(out)}
            self.assertIn("already-exists", steps["venv"]["detail"])
            self.assertTrue(steps["deps"]["ok"])
            self.assertTrue(steps["service"]["ok"])
            self.assertIn("warning", steps["verify"]["detail"], "服务未跑 → verify 出 warning")

    def test_failing_runner_makes_step_fail_and_exit_nonzero(self):
        with FakeVenv() as fake:  # 不预置 venv：venv 步才会真正执行并失败
            code, out = _run_main(["--home", str(fake.home)], runner=lambda argv: 1,
                                probe=lambda port: False)
            self.assertEqual(code, 1)
            steps = {s["step"]: s for s in _json_steps(out)}
            self.assertFalse(steps["venv"]["ok"], "venv 步失败必须体现在摘要里")

    def test_deps_fails_clearly_when_venv_missing(self):
        with FakeVenv() as fake:
            code, out = _run_main(["--home", str(fake.home), "--skip-venv", "--skip-web",
                                 "--skip-service"], runner=lambda argv: 0,
                                probe=lambda port: False)
            self.assertEqual(code, 1)
            steps = {s["step"]: s for s in _json_steps(out)}
            self.assertFalse(steps["deps"]["ok"])
            self.assertIn("venv", steps["deps"]["detail"])

    def test_service_already_running_reported(self):
        with FakeVenv() as fake:
            home = self._prepared_home(fake)
            code, out = _run_main(["--home", str(home), "--skip-venv", "--skip-deps", "--skip-web"],
                                runner=lambda argv: 0, probe=lambda port: True,
                                health=lambda url: {"ok": True, "mode": "sim"})
            self.assertEqual(code, 0)
            steps = {s["step"]: s for s in _json_steps(out)}
            self.assertIn("already-running", steps["service"]["detail"])
            self.assertIn("启动命令", out)
            self.assertIn("trading-platform.json", out, "必须打印配置样例")

    def test_verify_uses_healthz_payload(self):
        with FakeVenv() as fake:
            home = self._prepared_home(fake)
            code, out = _run_main(["--home", str(home), "--skip-venv", "--skip-deps",
                                 "--skip-web", "--skip-service"],
                                runner=lambda argv: 0, probe=lambda port: True,
                                health=lambda url: {"ok": True, "mode": "sim"})
            self.assertEqual(code, 0)
            steps = {s["step"]: s for s in _json_steps(out)}
            self.assertIn("sim", steps["verify"]["detail"])
            self.assertIn("mcp__quantwb__", steps["verify"]["detail"], "要提示工具面")

    def test_verify_healthz_failure_is_warning_not_failure(self):
        with FakeVenv() as fake:
            home = self._prepared_home(fake)

            def boom(url):
                raise OSError("connection refused")

            code, out = _run_main(["--home", str(home), "--skip-venv", "--skip-deps",
                                 "--skip-web", "--skip-service"],
                                runner=lambda argv: 0, probe=lambda port: True, health=boom)
            self.assertEqual(code, 0, "healthz 失败不阻塞安装")
            steps = {s["step"]: s for s in _json_steps(out)}
            self.assertTrue(steps["verify"]["ok"])
            self.assertIn("warning", steps["verify"]["detail"])

    def test_service_step_writes_repo_marker_for_autostart(self):
        """service 步写仓库标记文件（与 install_plugins.py 双保险）：
        platform-autostart 插件据此定位仓库并拉起 platform/server/run.py。"""
        with FakeVenv() as fake:
            home = self._prepared_home(fake)
            code, out = _run_main(["--home", str(home), "--skip-venv", "--skip-deps",
                                   "--skip-web"],
                                  runner=lambda argv: 0, probe=lambda port: False)
            self.assertEqual(code, 0)
            marker = home / "trading-platform-repo"
            self.assertTrue(marker.is_file(), "service 步必须写仓库标记文件")
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), str(ip.REPO_ROOT))
        # 既有约定：跳过的步骤零副作用（SkipAllTest 断言 home 为空），service 被跳过时不得补写
        with FakeVenv() as fake:
            _run_main(["--home", str(fake.home), "--skip-venv", "--skip-deps",
                       "--skip-web", "--skip-service"],
                      runner=lambda argv: 0, probe=lambda port: False)
            self.assertFalse((fake.home / "trading-platform-repo").exists())


class HealthAndPortHelpersTest(unittest.TestCase):
    def test_describe_health_carries_mode_and_tool_surface(self):
        text = ip.describe_health({"ok": True, "mode": "sim"})
        self.assertIn("sim", text)
        self.assertIn("mcp__quantwb__", text)

    def test_describe_health_tolerates_missing_mode(self):
        text = ip.describe_health({})
        self.assertIn("unknown", text)
        self.assertTrue(isinstance(text, str) and text)

    def test_port_in_use_detects_listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            self.assertTrue(ip.port_in_use(port))
        finally:
            sock.close()
        self.assertFalse(ip.port_in_use(port), "监听关闭后应报空闲")


if __name__ == "__main__":
    unittest.main(verbosity=2)
