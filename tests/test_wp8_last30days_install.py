"""WP8：last30days 社媒研究技能安装器（scripts/install_last30days.py）——锁定测试。

安装器把上游 last30days-skill（https://github.com/mvanhorn/last30days-skill）clone 到
DSH home（默认 ``~/.dsh/last30days-skill``），供 agent.cordis.yml 的 customSkillDirs 挂载：
* 幂等：不存在 → ``git clone --depth 1``；已存在 → ``git -C pull --ff-only``；
  ``--remove`` 删除目录，重复执行同样成功。
* 每步一行 JSON 摘要 ``{"step","ok","detail"}``；install/update 的 detail 含克隆到的
  commit SHA（``git rev-parse HEAD``），verify 步校验 ``skills/last30days/SKILL.md`` 存在，
  缺失即 ``ok=false`` 且退出码 1。
* 不装任何依赖（yt-dlp 等由上游首次配置自理）。

全部离线：对本地伪造 git 仓库做真实 clone/pull（本地路径 clone 不出网），
``git init`` 的伪造上游含 skills/last30days/SKILL.md，update 场景追加提交断言拉到新 SHA。
"""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(ROOT / "scripts" / f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ip = _load("install_last30days")


def _run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = ip.main(argv)
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


def _git(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {args} 失败：{proc.stderr}")
    return proc.stdout.strip()


def make_upstream(root: Path, with_skill: bool = True):
    """伪造上游仓库：``git init`` + 含（或不含）skills/last30days/SKILL.md 的提交。

    返回 (路径, head_sha 函数, 新增一个提交的函数)。本地路径 clone 是离线的。
    """
    upstream = root / "upstream"
    upstream.mkdir()
    skill = upstream / "skills" / "last30days"
    skill.mkdir(parents=True)
    (upstream / "README.md").write_text("upstream repo\n", encoding="utf-8")
    if with_skill:
        (skill / "SKILL.md").write_text(
            "---\nname: last30days\ndescription: upstream engine\n---\n# last30days\n",
            encoding="utf-8")
    _git(upstream, "init", "-q")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "test")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "init")

    def head():
        return _git(upstream, "rev-parse", "HEAD")

    def add_commit(message):
        (upstream / "skills" / "last30days" / "SKILL.md").write_text(
            f"---\nname: last30days\ndescription: {message}\n---\n# last30days\n",
            encoding="utf-8")
        _git(upstream, "add", "-A")
        _git(upstream, "commit", "-q", "-m", message)
        return head()

    return upstream, head, add_commit


class ArgParsingTests(unittest.TestCase):
    """参数解析与默认值（不出网、不碰真实 git）。"""

    def test_default_repo_url_and_target_dirname(self):
        self.assertEqual(ip.DEFAULT_REPO_URL,
                         "https://github.com/mvanhorn/last30days-skill")
        self.assertEqual(ip.TARGET_DIRNAME, "last30days-skill")

    def test_resolve_home_prefers_explicit_then_dsh_home_env_then_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "home-a"
            old = os.environ.get("DSH_HOME")
            try:
                os.environ["DSH_HOME"] = str(Path(tmp) / "home-b")
                self.assertEqual(ip.resolve_home(str(explicit)), explicit.resolve())
                self.assertEqual(ip.resolve_home(None), (Path(tmp) / "home-b").resolve())
                del os.environ["DSH_HOME"]
                self.assertTrue(str(ip.resolve_home(None)).endswith(".dsh"))
            finally:
                if old is not None:
                    os.environ["DSH_HOME"] = old
                else:
                    os.environ.pop("DSH_HOME", None)

    def test_parse_args_overrides(self):
        args = ip.parse_args(["--home", "/tmp/h1", "--repo-url", "https://example.com/x",
                              "--remove"])
        self.assertEqual(args.home, "/tmp/h1")
        self.assertEqual(args.repo_url, "https://example.com/x")
        self.assertTrue(args.remove)
        args_default = ip.parse_args([])
        self.assertIsNone(args_default.home)
        self.assertEqual(args_default.repo_url, ip.DEFAULT_REPO_URL)
        self.assertFalse(args_default.remove)


class InstallUpdateRemoveTests(unittest.TestCase):
    """真实 git 对本地伪造上游：install / update / remove 幂等。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="last30days-install-")
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.target = self.home / ip.TARGET_DIRNAME

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_clones_local_upstream_and_reports_sha(self):
        upstream, head, _ = make_upstream(self.root)
        code, out = _run_main(["--home", str(self.home), "--repo-url", str(upstream)])
        self.assertEqual(code, 0, out)
        self.assertTrue(self.target.is_dir(), "clone 目录不存在")
        installed = self.target / "skills" / "last30days" / "SKILL.md"
        self.assertTrue(installed.is_file(), "SKILL.md 未随 clone 落地")
        self.assertIn("upstream engine", installed.read_text(encoding="utf-8"))
        steps = _json_steps(out)
        by_step = {row["step"]: row for row in steps}
        self.assertTrue(by_step["install"]["ok"])
        self.assertIn(head(), by_step["install"]["detail"],
                      "install 摘要必须含克隆到的 commit SHA")
        self.assertTrue(by_step["verify"]["ok"])
        self.assertIn("skills/last30days/SKILL.md", by_step["verify"]["detail"])

    def test_update_pulls_new_upstream_commit(self):
        upstream, head, add_commit = make_upstream(self.root)
        code, _ = _run_main(["--home", str(self.home), "--repo-url", str(upstream)])
        self.assertEqual(code, 0)
        new_sha = add_commit("second commit")
        code, out = _run_main(["--home", str(self.home), "--repo-url", str(upstream)])
        self.assertEqual(code, 0, out)
        steps = _json_steps(out)
        by_step = {row["step"]: row for row in steps}
        self.assertTrue(by_step["update"]["ok"], "已存在目录应走 update（pull --ff-only）")
        self.assertIn(new_sha, by_step["update"]["detail"], "update 摘要必须含新 commit SHA")
        self.assertEqual(head(), new_sha)
        self.assertIn("second commit",
                      (self.target / "skills" / "last30days" / "SKILL.md")
                      .read_text(encoding="utf-8"))

    def test_update_is_idempotent_when_upstream_unchanged(self):
        upstream, _, _ = make_upstream(self.root)
        for _ in range(2):
            code, out = _run_main(["--home", str(self.home), "--repo-url", str(upstream)])
            self.assertEqual(code, 0, out)
        by_step = {row["step"]: row for row in _json_steps(out)}
        self.assertTrue(by_step["update"]["ok"], "无新提交时 pull --ff-only 也应成功")

    def test_remove_deletes_directory_and_is_idempotent(self):
        upstream, _, _ = make_upstream(self.root)
        code, _ = _run_main(["--home", str(self.home), "--repo-url", str(upstream)])
        self.assertEqual(code, 0)
        code, out = _run_main(["--home", str(self.home), "--remove"])
        self.assertEqual(code, 0, out)
        self.assertFalse(self.target.exists(), "--remove 后目录应被删除")
        self.assertTrue({row["step"] for row in _json_steps(out)} >= {"remove"})
        code, out = _run_main(["--home", str(self.home), "--remove"])
        self.assertEqual(code, 0, f"目录已缺失时 --remove 仍应成功（幂等）：{out}")

    def test_install_fails_when_skill_md_missing(self):
        upstream, _, _ = make_upstream(self.root, with_skill=False)
        code, out = _run_main(["--home", str(self.home), "--repo-url", str(upstream)])
        self.assertEqual(code, 1, "SKILL.md 缺失必须以退出码 1 失败")
        by_step = {row["step"]: row for row in _json_steps(out)}
        self.assertFalse(by_step["verify"]["ok"], "verify 步必须 ok=false")
        self.assertIn("skills/last30days/SKILL.md", by_step["verify"]["detail"])

    def test_existing_non_git_directory_fails_with_guidance(self):
        self.target.mkdir(parents=True)
        (self.target / "README.md").write_text("not a git repo\n", encoding="utf-8")
        code, out = _run_main(["--home", str(self.home)])
        self.assertEqual(code, 1, "已存在目录但不是 git 仓库必须失败并给出处置指引")
        detail = "".join(row["detail"] for row in _json_steps(out))
        self.assertIn("--remove", detail, "失败详情应指引先 --remove 再重装")


if __name__ == "__main__":
    unittest.main(verbosity=2)
