import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
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
"""


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
        for directory in ("workbench", "fin-data", "engine"):
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
        self.assertEqual(result.count("  disabled: false\r\n"), 2)
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
                (destination / filename).write_bytes(name.encode())
                return subprocess.CompletedProcess(args, 0, json.dumps([{"filename": filename}]))
            self.assertEqual(args[:5], ["dsh", "plugin", "--profile", "web", "add"])
            archive = Path(args[5])
            self.assertTrue(archive.is_absolute())
            self.assertTrue(archive.is_file())
            self.assertEqual(kwargs["env"]["DSH_HOME"], str(self.home))
            self.added.append(archive)
            if fail == "add":
                raise subprocess.CalledProcessError(7, args)
            return subprocess.CompletedProcess(args, 0)

        return run

    def test_pack_absolute_paths_root_host_first_and_persistent_sources(self):
        with patch.object(self.installer.shutil, "which", side_effect=lambda name: name), \
                patch.object(self.installer.subprocess, "run", side_effect=self.fake_commands()):
            self.installer.install_plugins(self.repo, self.home)
            self.installer.install_plugins(self.repo, self.home)
        self.assertEqual([path.read_bytes().decode() for path in self.added],
                         ["workbench", "fin-data", "engine"] * 2)
        self.assertTrue(all(not path.exists() for path in self.packed))
        self.assertEqual(len(list((self.home / "trading-plugin-packages").glob("*.tgz"))), 3)
        self.assertEqual((self.repo / "agent.cordis.yml").read_text(),
                         self.installer.activate_preset(PRESET))

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
