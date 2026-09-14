import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import re
import shutil
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_plugins.py"
PRESET = """# keep comments and unrelated disabled rows
- id: fin-data
  name: '@bstester/dsh-fin-data'
  disabled: true
- id: other
  name: other
  disabled: true
  config:
    disabled: true
- id: trading-engine
  name: '@bstester/dsh-trading-engine'
  disabled: true
- id: futu-keepalive
  name: '@bstester/dsh-futu-keepalive'
  disabled: true
"""


PACKAGE_PYTHON = {
    "datasource": ["python/trading_datasource/__init__.py", "python/trading_datasource/market.py"],
    "fin-data": ["python/fin_sentiment.py"],
    "core": ["python/trading_core/__init__.py"],
    "engine": ["python/engine.py"],
    "workbench": ["python/bars.py"],
    "futu-keepalive": ["src/index.js"],
}


def write_tarball(path, plugin):
    """写一个真实的 tar.gz，结构等同于 npm pack 的 package/ 布局。"""
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as tar:
        for member in PACKAGE_PYTHON[plugin]:
            payload = ("VALUE = %r\n" % member).encode()
            info = tarfile.TarInfo("package/" + member)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "shared plugin installer is missing")
        spec = importlib.util.spec_from_file_location("install_plugins", SCRIPT)
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.scratch = tempfile.TemporaryDirectory(prefix=".install-test-", dir=ROOT)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.repo = self.root / "checkout with spaces"
        self.repo.mkdir()
        self.home = self.root / "custom dsh home"
        (self.repo / "agent.cordis.yml").write_text(PRESET)
        for directory in ("workbench", "fin-data", "engine", "futu-keepalive"):
            package = self.repo / "plugins" / directory
            package.mkdir(parents=True)
            manifest = {"name": directory, "version": "0.2.0"}
            if directory == "workbench":
                manifest["dsh"] = {"bundle": {"patch": "profile.patch.yml"}}
                (package / "profile.patch.yml").write_text("plugins: []\n")
            (package / "package.json").write_text(json.dumps(manifest))

    def test_activation_is_idempotent_and_preserves_other_rows_and_crlf(self):
        original = PRESET.replace("\n", "\r\n")
        result = self.installer.activate_preset(original)
        self.assertEqual(result.count("  disabled: false\r\n"),
                         len(self.installer.PRESET_PACKAGES))
        self.assertIn("  name: other\r\n  disabled: true\r\n", result)
        self.assertIn("    disabled: true\r\n", result)
        self.assertNotIn("workbench", result)
        self.assertEqual(self.installer.activate_preset(result), result)

    def test_activation_rejects_unknown_target_shape(self):
        for malformed in (PRESET.replace("- id: fin-data", "- id: missing"),
                          PRESET + PRESET,
                          PRESET.replace("  disabled: true", "  disabled: !!js custom", 1),
                          PRESET.replace("@bstester/dsh-fin-data", "unrelated")):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                self.installer.activate_preset(malformed)

    def fake_commands(self, fail=None):
        self.added = []
        self.packed = []

        def run(args, **kwargs):
            if args[0] == "npm":
                destination = Path(args[args.index("--pack-destination") + 1])
                self.assertTrue(destination.is_absolute())
                self.assertTrue(destination.is_relative_to(self.repo))
                name = Path(kwargs["cwd"]).name
                self.packed.append(destination)
                if fail == "pack":
                    raise subprocess.CalledProcessError(5, args)
                filename = name + "-0.2.0.tgz"
                write_tarball(destination / filename, name)
                return subprocess.CompletedProcess(args, 0, json.dumps([{"filename": filename}]))
            self.assertEqual(args[:5], ["dsh", "plugin", "--profile", "web", "add"])
            archive = Path(args[5])
            self.assertTrue(archive.is_absolute())
            self.assertTrue(archive.is_file())
            self.assertEqual(kwargs["env"]["DSH_HOME"], str(self.home))
            self.added.append(archive)
            # 假 dsh 也要像真的那样把包放进 profile 的 node_modules，
            # 否则「自检」类测试会因为环境不真实而误报。
            plugin = re.fullmatch(r"([a-z-]+)-[0-9a-f]{64}\.tgz", archive.name).group(1)
            package = self.installer.PACKAGE_NAMES.get(plugin, plugin)
            target = self.home / "profiles" / "web" / "node_modules" / package
            target.mkdir(parents=True, exist_ok=True)
            (target / "package.json").write_text(json.dumps({"name": package, "version": "0.2.0"}))
            if fail == "add":
                raise subprocess.CalledProcessError(7, args)
            return subprocess.CompletedProcess(args, 0)

        return run

    def test_pack_absolute_paths_root_host_first_and_persistent_sources(self):
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
            self.installer.install_plugins(self.repo, self.home)
        # 归档是内容寻址的 <plugin>-<sha256>.tgz；按文件名还原插件顺序
        names = [re.fullmatch(r"([a-z-]+)-[0-9a-f]{64}\.tgz", path.name).group(1)
                 for path in self.added]
        self.assertEqual(names, list(self.installer.PLUGINS) * 2)
        self.assertTrue(all(not path.exists() for path in self.packed))
        self.assertEqual(len(list((self.home / "trading-plugin-packages").glob("*.tgz"))),
                         len(self.installer.PLUGINS) + len(self.installer.LIBRARIES))
        self.assertEqual((self.repo / "agent.cordis.yml").read_text(),
                         self.installer.activate_preset(PRESET))

    def test_data_layer_is_extracted_to_unified_tree(self):
        """统一数据层必须解到 <DSH>/trading-python/，供各插件按同一路径定位。"""
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
        root = self.home / "trading-python"
        self.assertTrue((root / "datasource" / "trading_datasource" / "market.py").is_file())
        self.assertTrue((root / "fin-data" / "fin_sentiment.py").is_file())
        # 未被跨包引用的插件不必进统一目录
        self.assertFalse((root / "engine").exists())

    def test_data_layer_is_not_installed_as_a_plugin(self):
        """datasource 是库不是 Harness 插件，不能被 dsh plugin add。"""
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
        self.assertEqual(len(self.added), len(self.installer.PLUGINS))

    def test_reinstall_refreshes_unified_tree(self):
        """重装即刷新，因此统一目录不可能与仓库版本漂移。"""
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
        stale = self.home / "trading-python" / "datasource" / "stale.py"
        stale.write_text("old")
        self.installer.write_data_layer_pth  # 触碰以确保模块属性存在
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
        self.assertFalse(stale.exists(), "重装必须清掉统一目录里的陈旧文件")

    def test_link_writes_pth_into_trading_venv(self):
        """venv 建好后才写 .pth；路径指向统一目录里的 datasource。"""
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
        site = self.home / "trading-venv" / "lib" / "python3.13" / "site-packages"
        site.mkdir(parents=True)
        result = self.installer.write_data_layer_pth(self.home)
        self.assertIsNotNone(result)
        pth = site / "dsh-trading-python.pth"
        self.assertTrue(pth.is_file())
        # .pth 每个已解出的库一行：datasource 行 + core 行（与 helper 同一口径）
        lines, missing = self.installer.data_layer_pth_lines(self.home)
        self.assertEqual([Path(line) for line in lines],
                         [self.home / "trading-python" / "datasource",
                          self.home / "trading-python" / "core"])
        self.assertEqual(pth.read_text(), "".join(line + "\n" for line in lines))

    def test_link_returns_none_without_venv(self):
        """venv 不存在时返回 None，由安装脚本提示重试，而不是写入错误路径。"""
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
        self.assertIsNone(self.installer.write_data_layer_pth(self.home))

    def _install_once(self, with_venv=True):
        """跑一次安装；自检需要 venv 存在（否则会正确地报"找不到 site-packages"）。"""
        if with_venv:
            (self.home / "trading-venv" / "lib" / "python3.13" / "site-packages").mkdir(
                parents=True, exist_ok=True)
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)

    def test_check_passes_on_a_complete_install(self):
        self._install_once()
        self.assertEqual(self.installer.check_install(self.repo, self.home), 0)

    def test_check_detects_missing_pth(self):
        """统一数据层装上了但 .pth 没写时，脚本会报 ModuleNotFoundError —— 必须能被自检抓到。"""
        self._install_once()
        self.installer.write_data_layer_pth(self.home)
        site = self.home / "trading-venv" / "lib" / "python3.13" / "site-packages"
        (site / "dsh-trading-python.pth").unlink()
        self.assertEqual(self.installer.check_install(self.repo, self.home), 1)

    def test_check_detects_missing_plugin(self):
        self._install_once()
        self.installer.write_data_layer_pth(self.home)
        shutil.rmtree(self.home / "trading-python" / "datasource")
        self.assertEqual(self.installer.check_install(self.repo, self.home), 1)

    def test_check_detects_disabled_row(self):
        """行没被启用时插件装了也不会加载。"""
        self._install_once()
        self.installer.write_data_layer_pth(self.home)
        preset = self.repo / "agent.cordis.yml"
        preset.write_text(preset.read_text().replace(
            "- id: futu-keepalive\n  name: '@bstester/dsh-futu-keepalive'\n  disabled: false",
            "- id: futu-keepalive\n  name: '@bstester/dsh-futu-keepalive'\n  disabled: true"))
        self.assertEqual(self.installer.check_install(self.repo, self.home), 1)

    def test_check_reports_missing_venv_instead_of_crashing(self):
        self._install_once(with_venv=False)
        self.assertEqual(self.installer.check_install(self.repo, self.home), 1)

    def test_required_command_failures_keep_preset_disabled_and_clean_staging(self):
        for failure in ("pack", "add"):
            with self.subTest(failure=failure), \
                    patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                    patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands(failure)):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.installer.install_plugins(self.repo, self.home)
                self.assertEqual((self.repo / "agent.cordis.yml").read_text(), PRESET)
                self.assertTrue(all(not path.exists() for path in self.packed))

    def test_missing_tools_fail_before_changes(self):
        with patch.object(self.installer.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                self.installer.install_plugins(self.repo, self.home)
        self.assertEqual((self.repo / "agent.cordis.yml").read_text(), PRESET)

    def test_cli_reports_required_failure_as_nonzero(self):
        result = subprocess.run(
            [os.sys.executable, "-B", str(SCRIPT), "install", "--repo", str(self.repo),
             "--dsh-home", str(self.home)],
            env={**os.environ, "PATH": ""}, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required command", result.stderr)
        self.assertNotIn("enabled", result.stdout)

    def test_invalid_pack_output_fails_without_activation(self):
        for output in ("not json", "[]", '[{"filename":"../escaped.tgz"}]'):
            with self.subTest(output=output), \
                    patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                    patch.object(self.installer.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0, output)):
                with self.assertRaises(ValueError):
                    self.installer.install_plugins(self.repo, self.home)
                self.assertEqual((self.repo / "agent.cordis.yml").read_text(), PRESET)
                self.assertEqual(list(self.repo.glob(".install-pack-*")), [])

    def test_workbench_must_export_real_root_bundle(self):
        manifest = self.repo / "plugins" / "workbench" / "package.json"
        manifest.write_text('{"dsh":{"client":{}}}')
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name):
            with self.assertRaises(ValueError):
                self.installer.install_plugins(self.repo, self.home)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def prepare_git(self):
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Installer test")
        self.git("add", "agent.cordis.yml")
        self.git("commit", "-qm", "preset fixture")

    def test_update_preserves_only_known_toggles_and_upstream_changes(self):
        self.prepare_git()
        preset = self.repo / "agent.cordis.yml"
        enabled = self.installer.activate_preset(PRESET)
        preset.write_text(enabled)
        actual_run = subprocess.run

        def run(args, **kwargs):
            if args[-2:] == ["pull", "--ff-only"]:
                self.assertEqual(preset.read_text(), PRESET)
                preset.write_text(PRESET.replace("keep comments", "upstream comments"))
                return subprocess.CompletedProcess(args, 0)
            return actual_run(args, **kwargs)

        with patch.object(self.installer.subprocess, "run", side_effect=run):
            self.installer.update_checkout(self.repo)
        self.assertEqual(preset.read_text(), enabled.replace("keep comments", "upstream comments"))

    def test_repeated_real_local_git_updates_do_not_conflict_with_activation(self):
        self.prepare_git()
        upstream = self.root / "upstream"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(upstream)], check=True)
        branch = self.git("branch", "--show-current").strip()
        self.git("remote", "add", "origin", str(upstream))
        preset = self.repo / "agent.cordis.yml"
        preset.write_text(self.installer.activate_preset(PRESET))
        for version in ("one", "two"):
            updated = PRESET.replace("keep comments", "upstream " + version)
            (upstream / "agent.cordis.yml").write_text(updated)
            subprocess.run(["git", "-C", str(upstream), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-qam", version],
                           check=True, capture_output=True)
            self.git("fetch", "-q", "origin")
            self.git("branch", "--set-upstream-to=origin/" + branch)
            self.installer.update_checkout(self.repo)
            self.assertEqual(preset.read_text(), self.installer.activate_preset(updated))

    def test_failed_pull_restores_activation(self):
        self.prepare_git()
        preset = self.repo / "agent.cordis.yml"
        enabled = self.installer.activate_preset(PRESET)
        preset.write_text(enabled)
        # No remote configured: local-only git pull must fail.
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.update_checkout(self.repo)
        self.assertEqual(preset.read_text(), enabled)

    def test_update_accepts_legacy_removed_disabled_flags(self):
        self.prepare_git()
        preset = self.repo / "agent.cordis.yml"
        legacy = self.installer.activate_preset(PRESET).replace("  disabled: false\n", "")
        preset.write_text(legacy)
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.update_checkout(self.repo)
        self.assertEqual(preset.read_text(), legacy)

    def test_interrupted_pull_restores_activation(self):
        self.prepare_git()
        preset = self.repo / "agent.cordis.yml"
        enabled = self.installer.activate_preset(PRESET)
        preset.write_text(enabled)
        actual_run = subprocess.run

        def run(args, **kwargs):
            if args[-2:] == ["pull", "--ff-only"]:
                raise KeyboardInterrupt()
            return actual_run(args, **kwargs)

        with patch.object(self.installer.subprocess, "run", side_effect=run):
            with self.assertRaises(KeyboardInterrupt):
                self.installer.update_checkout(self.repo)
        self.assertEqual(preset.read_text(), enabled)

    def test_update_refuses_unrelated_or_staged_edits_without_reverting(self):
        self.prepare_git()
        preset = self.repo / "agent.cordis.yml"
        custom = self.installer.activate_preset(PRESET) + "# personal edit\n"
        preset.write_text(custom)
        with self.assertRaisesRegex(ValueError, "local|edit|commit"):
            self.installer.update_checkout(self.repo)
        self.assertEqual(preset.read_text(), custom)
        preset.write_text(self.installer.activate_preset(PRESET))
        self.git("add", "agent.cordis.yml")
        staged = self.git("diff", "--cached")
        with self.assertRaises(ValueError):
            self.installer.update_checkout(self.repo)
        self.assertEqual(self.git("diff", "--cached"), staged)

    def test_both_entrypoints_use_shared_helper_and_export_home(self):
        bash = (ROOT / "install.sh").read_text()
        powershell = (ROOT / "install.ps1").read_text()
        for entry in (bash, powershell):
            self.assertIn("install_plugins.py", entry)
            self.assertNotIn("sed -i", entry)
            self.assertNotIn("--pack-destination /tmp", entry)
        self.assertIn("export DSH_HOME", bash)
        self.assertIn("$env:DSH_HOME = $dshHome", powershell)
        self.assertIn("$LASTEXITCODE", powershell)
        self.assertNotIn("Set-Content -Path $modeFile", powershell)

    @unittest.skipUnless(os.name != "nt", "Bash entrypoint requires a POSIX shell")
    def test_install_never_initializes_or_repairs_account_mode(self):
        destination = self.home / ".agent-presets" / "dsh-trading-agents"
        (destination / ".git").mkdir(parents=True)
        (destination / "scripts").mkdir()
        (destination / "scripts" / "install_plugins.py").write_text("raise SystemExit(0)\n")
        binary = self.home / "trading-venv" / "bin"
        binary.mkdir(parents=True)
        for name in ("python", "pip"):
            command = binary / name
            command.write_text("#!/bin/sh\nexit 0\n")
            command.chmod(0o700)
        (self.home / "futu-token").write_text("nonfunctional-test-placeholder")
        mode = self.home / "trading-account-mode"
        for original in (None, "", "live\n", "invalid\n"):
            with self.subTest(original=original):
                mode.unlink(missing_ok=True)
                if original is not None:
                    mode.write_text(original)
                result = subprocess.run(["bash", str(ROOT / "install.sh")],
                                        env={**os.environ, "DSH_HOME": str(self.home)},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(mode.read_text() if mode.exists() else None, original)

    @unittest.skipUnless(os.name != "nt", "Bash entrypoint requires a POSIX shell")
    def test_bash_stops_before_optional_setup_when_plugin_install_fails(self):
        destination = self.home / ".agent-presets" / "dsh-trading-agents"
        (destination / ".git").mkdir(parents=True)
        (destination / "scripts").mkdir()
        helper = destination / "scripts" / "install_plugins.py"
        helper.write_text(
            "import json, os, pathlib, sys\n"
            "home = pathlib.Path(os.environ['DSH_HOME'])\n"
            "(home / (sys.argv[1] + '.json')).write_text(json.dumps(sys.argv))\n"
            "sys.exit(9 if sys.argv[1] == 'install' else 0)\n"
        )
        result = subprocess.run(["bash", str(ROOT / "install.sh")],
                                env={**os.environ, "DSH_HOME": str(self.home)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 9)
        args = json.loads((self.home / "install.json").read_text())
        self.assertEqual(args[-2:], ["--dsh-home", str(self.home)])
        self.assertTrue((self.home / "update.json").exists())
        self.assertFalse((self.home / "trading-venv").exists())
        self.assertFalse((self.home / "trading-account-mode").exists())


if __name__ == "__main__":
    unittest.main()
