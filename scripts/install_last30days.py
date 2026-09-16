#!/usr/bin/env python3
"""last30days 社媒研究技能安装器（可选组件，幂等）。

把上游 last30days-skill（AI 智能体驱动的社媒/全网近 30 天研究引擎，
https://github.com/mvanhorn/last30days-skill，MIT）clone 到 DSH home 下：

    python3 scripts/install_last30days.py [--home <DSH_HOME>] [--remove] [--repo-url <url>]

* 默认 clone：``git clone --depth 1 https://github.com/mvanhorn/last30days-skill
  <home>/last30days-skill``；home 缺省取 ``$DSH_HOME``，其次 ``~/.dsh``。
* 已存在：``git -C <dir> pull --ff-only``（update）；``--remove`` 删除目录（幂等）。
* 每步一行 JSON 摘要 ``{"step","ok","detail"}``；install/update 的 detail 含克隆到的
  commit SHA（``git rev-parse HEAD``）；verify 步校验 ``skills/last30days/SKILL.md``
  存在，缺失即 ``ok=false`` 并以退出码 1 结束。
* 不安装任何依赖：yt-dlp、ScrapeCreators、各来源 CLI 等由上游首次配置自理
  （见上游 README.zh-CN.md）；第三方代码在本机运行的信任边界见 install/HARNESS_SETUP.md。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO_URL = "https://github.com/mvanhorn/last30days-skill"
TARGET_DIRNAME = "last30days-skill"
RELATIVE_SKILL = "skills/last30days/SKILL.md"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="clone/update 上游 last30days-skill 到 DSH home（幂等，不装依赖）")
    parser.add_argument("--home", default=None,
                        help="DSH home 目录（默认 $DSH_HOME，其次 ~/.dsh）")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL,
                        help=f"上游仓库地址（默认 {DEFAULT_REPO_URL}；测试可指向本地路径）")
    parser.add_argument("--remove", action="store_true",
                        help="删除已安装目录后退出（目录缺失同样成功）")
    return parser.parse_args(argv)


def resolve_home(explicit=None):
    """explicit > $DSH_HOME > ~/.dsh（与 futu-token 等既有约定同一套 home 语义）。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_home = os.environ.get("DSH_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".dsh").resolve()


def _emit(step, ok, detail):
    print(json.dumps({"step": step, "ok": bool(ok), "detail": detail},
                     ensure_ascii=False))


def _git(args):
    """真实 git 调用，返回 (returncode, stdout 去尾空白, stderr 去尾空白)。"""
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main(argv=None):
    args = parse_args(argv)
    target = resolve_home(args.home) / TARGET_DIRNAME

    if args.remove:
        if target.is_dir():
            shutil.rmtree(target)
            _emit("remove", True, f"removed {target}")
        else:
            _emit("remove", True, f"already absent: {target}")
        return 0

    if target.is_dir():
        if not (target / ".git").exists():
            _emit("update", False,
                  f"{target} 已存在但不是 git 仓库（缺 .git）；先 --remove 再重装")
            return 1
        code, out, err = _git(["-C", str(target), "pull", "--ff-only"])
        if code != 0:
            _emit("update", False,
                  f"git -C {target} pull --ff-only 失败（exit {code}）：{err or out}；"
                  "必要时 --remove 后重装")
            return 1
        mode = "update"
    else:
        code, out, err = _git(["clone", "--depth", "1", args.repo_url, str(target)])
        if code != 0:
            _emit("install", False,
                  f"git clone --depth 1 {args.repo_url} 失败（exit {code}）：{err or out}")
            return 1
        mode = "install"

    code, sha, err = _git(["-C", str(target), "rev-parse", "HEAD"])
    if code != 0:
        _emit(mode, False, f"git rev-parse HEAD 失败（exit {code}）：{err or sha}")
        return 1
    _emit(mode, True, f"{TARGET_DIRNAME} 就绪于 {target}（commit {sha}）")

    if (target / RELATIVE_SKILL).is_file():
        _emit("verify", True,
              f"{RELATIVE_SKILL} 存在（commit {sha}）；不安装依赖，yt-dlp 等由上游首次配置自理")
        return 0
    _emit("verify", False,
          f"{RELATIVE_SKILL} 缺失（commit {sha}）：上游布局变化？--remove 后用 --repo-url 重装")
    return 1


if __name__ == "__main__":
    sys.exit(main())
