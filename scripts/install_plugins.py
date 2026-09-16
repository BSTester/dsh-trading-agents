#!/usr/bin/env python3
"""Install local Harness packages and activate only the two preset tool rows."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


PLUGINS = ("workbench", "fin-data", "engine", "futu-keepalive", "platform-autostart")
# 统一数据层：不是 Harness 插件（没有 cordis 行），只作为 python 库被各插件共用。
LIBRARIES = ("datasource", "core")
# 需要出现在"统一 python 目录"里、可被其他插件 import 或调用的包：
#   datasource —— 被 engine/workbench 直接 import
#   fin-data   —— 被量化侧以子进程调用（共用同一套新闻/情绪渠道）
#   core       —— 量化平台核心库（PIT 存储/日历/同步/质量），WP1 起被各插件共用
UNIFIED_PYTHON = ("datasource", "fin-data", "core")
UNIFIED_ROOT_NAME = "trading-python"
# 目录名 → npm 包名（自检用；工作台没有 preset 行，故不放进 PRESET_PACKAGES）
PACKAGE_NAMES = {
    "workbench": "@bstester/dsh-trading-workbench",
    "fin-data": "@bstester/dsh-fin-data",
    "engine": "@bstester/dsh-trading-engine",
    "futu-keepalive": "@bstester/dsh-futu-keepalive",
    "platform-autostart": "@bstester/dsh-platform-autostart",
    "datasource": "@bstester/dsh-datasource",
    "core": "@bstester/dsh-trading-core",
}
DATA_LAYER_PTH_NAME = "dsh-trading-python.pth"
# 库目录 → 解包后必须存在的标记（extract 与自检共用）。
LIBRARY_MARKERS = {"datasource": "trading_datasource", "fin-data": "fin_sentiment.py",
                   "core": "trading_core"}


def data_layer_pth_lines(dsh_home):
    """返回应写入 .pth 的目录行与缺失的库。

    返回 (lines, missing)：lines 是已就绪库的绝对路径列表；missing 是未解出的库名。
    datasource 缺失时调用方维持旧行为（返回 None 提示先 install）。
    """
    root = unified_python_root(dsh_home)
    lines, missing = [], []
    for lib in LIBRARIES:  # 只遍历 LIBRARIES：fin-data 是子进程调用，不进 .pth
        marker = LIBRARY_MARKERS[lib]
        if (root / lib / marker).is_dir():
            lines.append(str(root / lib))
        else:
            missing.append(lib)
    return lines, missing


PRESET_PACKAGES = {
    "fin-data": "@bstester/dsh-fin-data",
    "trading-engine": "@bstester/dsh-trading-engine",
    "futu-keepalive": "@bstester/dsh-futu-keepalive",
    # 会话启动自动检测并拉起工作台服务；仓库定位依赖 write_repo_marker 写的标记文件。
    "platform-autostart": "@bstester/dsh-platform-autostart",
}

# 仓库标记文件：platform-autostart 插件据此定位 <repo>/platform/server/run.py。
REPO_MARKER_NAME = "trading-platform-repo"


def write_repo_marker(repo, dsh_home):
    """写仓库标记文件（内容=--repo 值，覆盖写）。

    install/update（本脚本）与 install_platform.py 的 service 步各写一次，双保险；
    解析口径在 plugins/platform-autostart/src/autostart.js 的 resolvePaths（首行去空白）。
    """
    marker = Path(dsh_home) / REPO_MARKER_NAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(Path(repo).resolve()) + "\n", encoding="utf-8")
    return marker


def activate_preset(text):
    # Do not parse/dump YAML: Harness uses !!js tags, and unrelated rows and
    # comments must survive byte-for-byte (including Windows line endings).
    blocks = re.split(r"(?=^- )", text, flags=re.MULTILINE)
    found = set()
    for index, block in enumerate(blocks):
        row = re.match(r"- id: ([\w-]+)[ \t]*(?:#[^\r\n]*)?\r?$", block, re.MULTILINE)
        if not row or row[1] not in PRESET_PACKAGES:
            continue
        row_id = row[1]
        if row_id in found:
            raise ValueError("Duplicate preset row: " + row_id)
        found.add(row_id)
        name = re.search(r"^  name: ['\"]?([^'\"\r\n ]+)['\"]?[ \t]*\r?$", block, re.MULTILINE)
        if not name or name[1] != PRESET_PACKAGES[row_id]:
            raise ValueError("Unexpected package in preset row: " + row_id)
        disabled = list(re.finditer(r"^  disabled: ([^\r\n]*)", block, re.MULTILINE))
        if len(disabled) > 1:
            raise ValueError("Duplicate disabled flag: " + row_id)
        if disabled and not re.fullmatch(r"(true|false)[ \t]*(?:#.*)?", disabled[0][1]):
            raise ValueError("Unsupported disabled expression: " + row_id)
        blocks[index] = re.sub(r"(^  disabled: )true(?=[ \t\r\n]|$)", r"\g<1>false",
                               block, flags=re.MULTILINE)
    if found != set(PRESET_PACKAGES):
        raise ValueError("Missing required preset rows: " + ", ".join(set(PRESET_PACKAGES) - found))
    return "".join(blocks)


def read_text(path):
    return path.read_bytes().decode("utf-8")


def write_text(path, text):
    staging = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".install-write-", delete=False) as output:
            staging = Path(output.name)
            output.write(text.encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        shutil.copymode(path, staging)
        staging.replace(path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def normalized_activation(text):
    blocks = re.split(r"(?=^- )", activate_preset(text), flags=re.MULTILINE)
    for index, block in enumerate(blocks):
        row = re.match(r"- id: ([\w-]+)", block)
        if row and row[1] in PRESET_PACKAGES:
            blocks[index] = re.sub(r"^  disabled: false[ \t]*\r?\n", "", block, flags=re.MULTILINE)
    return "".join(blocks)


def update_checkout(repo):
    """Temporarily undo only recognized installer toggles, never user edits."""
    repo = Path(repo).resolve()
    preset = repo / "agent.cordis.yml"
    git = ["git", "-C", str(repo)]
    staged = subprocess.run(git + ["diff", "--cached", "--", preset.name],
                            check=True, capture_output=True).stdout
    if staged:
        raise ValueError("Preset has staged edits; commit or stash them before updating.")
    base = subprocess.run(git + ["show", "HEAD:" + preset.name],
                          check=True, capture_output=True).stdout.decode("utf-8")
    current = read_text(preset)
    # Git may check out CRLF on Windows while the object stores LF.
    baseline = base.replace("\n", "\r\n") if "\r\n" in current and "\r\n" not in base else base
    toggled = current != baseline
    if toggled:
        if normalized_activation(current) != normalized_activation(baseline):
            raise ValueError("Preset has local edits beyond installer toggles; commit or stash them before updating.")
        write_text(preset, baseline)
    pulled = False
    try:
        subprocess.run(git + ["pull", "--ff-only"], check=True)
        pulled = True
    finally:
        if toggled:
            updated = read_text(preset)
            if updated == baseline:
                write_text(preset, current)
            elif pulled:
                write_text(preset, activate_preset(updated))


def unified_python_root(dsh_home):
    return Path(dsh_home) / UNIFIED_ROOT_NAME


def site_packages_dir(dsh_home):
    """定位交易 venv 的 site-packages；venv 尚未创建时返回 None。"""
    venv = Path(dsh_home) / "trading-venv"
    for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
        for candidate in sorted(venv.glob(pattern)):
            if candidate.is_dir():
                return candidate
    return None


def extract_python(archive, plugin, dsh_home, expect):
    """把包内 python/ 的内容解到 <DSH>/trading-python/<plugin>/。

    这是**单一来源**：仓库里的 python 是唯一事实来源，此处是安装时生成的副本，
    因此不存在人工同步导致的漂移（重装即刷新）。
    """
    import tarfile
    destination = unified_python_root(dsh_home) / plugin
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.startswith("package/python/")]
        if not members:
            raise ValueError(f"{plugin} 包内没有 python/ 目录")
        for member in members:
            member.name = "/".join(member.name.split("/")[2:])  # 去掉 package/python 前缀
            if not member.name:
                continue
            tar.extract(member, destination, filter="data")
    if not (destination / expect).exists():
        raise ValueError(f"{plugin} 解包后缺少 {expect}")
    return destination


def write_data_layer_pth(dsh_home):
    """向交易 venv 写入 .pth（每个已解出的库一行），让插件脚本直接 import。

    返回 (site_packages, lines) 或 None（datasource 未解出或 venv 不存在时）。
    """
    lines, missing = data_layer_pth_lines(dsh_home)
    if "datasource" in missing:
        return None
    site = site_packages_dir(dsh_home)
    if site is None:
        return None
    path = site / DATA_LAYER_PTH_NAME
    content = "".join(line + "\n" for line in lines)
    if not path.exists() or read_text(path) != content:
        path.write_text(content, encoding="utf-8")
    return site, lines


def check_install(repo, dsh_home):
    """自检安装完整性。返回 0 表示无问题。

    存在的意义：**统一数据层必须由安装器解出**，只跑 `dsh plugin add` 装不出它，
    而缺了它的表现是一句 `ModuleNotFoundError: No module named 'trading_datasource'`，
    很难一眼看出该做什么。
    """
    repo, dsh_home = Path(repo).resolve(), Path(dsh_home).expanduser().resolve()
    problems, notes = [], []
    # 说明：下面与 write_data_layer_pth 比对的路径都走 unified_python_root()，
    # 两边必须用同一套归一化，否则会出现"写进去的串和比对的串不同"的假失败。

    preset = repo / "agent.cordis.yml"
    if preset.is_file():
        notes.append(f"对话模式(preset): {preset}")
        # 注意：这里必须看**文件当前状态**，不能先过一遍 activate_preset ——
        # 那会把所有行强制改成启用，于是永远查不出"该启用但没启用"。
        for row_id in PRESET_PACKAGES:
            block = next((b for b in re.split(r"(?=^- )", read_text(preset), flags=re.MULTILINE)
                          if re.match(rf"- id: {re.escape(row_id)}[ \t]*\r?$", b, re.MULTILINE)), None)
            if block is None:
                problems.append(f"preset 缺少 {row_id} 行")
            elif re.search(r"^  disabled: true", block, re.MULTILINE):
                problems.append(f"preset 行 {row_id} 仍是 disabled: true（安装器未启用它）")
    else:
        problems.append(f"找不到 preset 组合文件：{preset}")

    for plugin, package in PACKAGE_NAMES.items():
        if plugin in ("datasource", "core"):  # 库不装进 profiles，走统一 python 目录
            continue
        manifest = dsh_home / "profiles" / "web" / "node_modules" / package / "package.json"
        if manifest.is_file():
            try:
                version = json.loads(read_text(manifest)).get("version", "?")
            except ValueError:
                version = "?"
            notes.append(f"插件 {plugin}: {version}")
        else:
            problems.append(f"插件 {plugin} 未安装（缺 {manifest}）")

    root = unified_python_root(dsh_home)
    for plugin in ("datasource", "fin-data", "core"):
        expect = LIBRARY_MARKERS[plugin]
        if (root / plugin / expect).exists():
            notes.append(f"统一数据层 {plugin}: {root / plugin}")
        else:
            problems.append(f"统一数据层缺少 {plugin}（{root / plugin / expect}）")

    site = site_packages_dir(dsh_home)
    if site is None:
        problems.append(f"找不到交易 venv 的 site-packages（{dsh_home / 'trading-venv'}）")
    else:
        pth = site / DATA_LAYER_PTH_NAME
        lines, missing = data_layer_pth_lines(dsh_home)
        expected = "".join(line + "\n" for line in lines)
        if not pth.is_file():
            problems.append(f"未写入 .pth：{pth}（跑 `link` 动作）")
        elif read_text(pth) != expected:
            problems.append(f".pth 内容不对：期望 {expected.strip()}，实际 {read_text(pth).strip()}")
        elif missing:
            problems.append(f".pth 缺少库行：{', '.join(missing)}（重跑 `install` 解出）")

    print("安装自检：")
    for line in notes:
        print(f"  · {line}")
    if problems:
        print("\n发现问题：")
        for line in problems:
            print(f"  ✗ {line}")
        print("\n修复：python scripts/install_plugins.py install "
              f"--repo {repo} --dsh-home {dsh_home}"
              "\n（顺序很重要：install 会重新解出统一数据层并写 .pth）")
        return 1
    print("\n✅ 安装完整。")
    return 0


def install_plugins(repo, dsh_home):
    repo, dsh_home = Path(repo).resolve(), Path(dsh_home).expanduser().resolve()
    commands = {}
    for name in ("npm", "pnpm", "npx"):
        commands[name] = shutil.which(name)
        if not commands[name]:
            raise FileNotFoundError(f"Required command {name!r} not found; install it and rerun the installer.")
    # dsh may be globally installed or npx-only. A bare npx spec resolves to
    # the latest published dsh, so plugin installs bind to whatever version
    # the host launches, matching the npx-only workflow.
    dsh = shutil.which("dsh")
    commands["dsh"] = [dsh] if dsh else [commands["npx"], "--yes", "@deepseek-ai/dsh"]
    preset = repo / "agent.cordis.yml"
    original = read_text(preset)
    enabled = activate_preset(original)
    workbench = repo / "plugins" / "workbench"
    manifest = json.loads(read_text(workbench / "package.json"))
    bundle = manifest.get("dsh", {}).get("bundle", {}).get("patch")
    if not isinstance(bundle, str) or not (workbench / bundle).is_file():
        raise ValueError("Workbench must ship a dsh.bundle.patch to load its Host in the web profile root.")
    env = {**os.environ, "DSH_HOME": str(dsh_home)}
    archives = dsh_home / "trading-plugin-packages"
    archives.mkdir(parents=True, exist_ok=True)
    # pnpm records file: sources in package.json/lockfiles. Keep immutable,
    # content-addressed archives; deleting them breaks later plugin updates.
    with tempfile.TemporaryDirectory(dir=repo, prefix=".install-pack-") as staging:
        packed_archives = {}
        for plugin in PLUGINS + LIBRARIES:
            result = subprocess.run(
                [commands["npm"], "pack", "--json", "--pack-destination", staging],
                cwd=repo / "plugins" / plugin, env=env, check=True,
                stdout=subprocess.PIPE, text=True,
            )
            packed = json.loads(result.stdout)
            if not isinstance(packed, list) or len(packed) != 1:
                raise ValueError("npm pack must return exactly one tarball for " + plugin)
            filename = packed[0].get("filename")
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError("npm pack returned an invalid tarball filename for " + plugin)
            source = Path(staging) / filename
            content = source.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            archive = archives / (plugin + "-" + digest + ".tgz")
            if archive.exists():
                if archive.read_bytes() != content:
                    raise ValueError("Existing plugin archive differs: " + str(archive))
            else:
                with archive.open("xb") as output:
                    output.write(content)
            packed_archives[plugin] = archive
        for plugin in PLUGINS:
            subprocess.run(commands["dsh"] + ["plugin", "--profile", "web", "add",
                                              str(packed_archives[plugin])],
                           cwd=repo, env=env, check=True)
        # 统一数据层：解到 <DSH>/trading-python/，各插件在运行时按同一路径定位
        for plugin in UNIFIED_PYTHON:
            extract_python(packed_archives[plugin], plugin, dsh_home,
                           LIBRARY_MARKERS[plugin])
    if read_text(preset) != original:
        raise ValueError("Preset changed during installation; refusing to overwrite concurrent edits.")
    if enabled != original:
        write_text(preset, enabled)
    if write_data_layer_pth(dsh_home) is None:
        print("NOTE: trading venv not found yet; run `install_plugins.py link` after creating it "
              "so plugins can import trading_datasource.")
    write_repo_marker(repo, dsh_home)
    print("Installed web profile Host and plugins; fin-data/trading-engine enabled. "
          "Restart dsh web (npx @deepseek-ai/dsh web).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "update", "link", "check"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dsh-home", type=Path,
                        default=Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))))
    args = parser.parse_args()
    try:
        if args.action == "update":
            update_checkout(args.repo)
            write_repo_marker(args.repo, args.dsh_home)
        elif args.action == "check":
            return check_install(args.repo, args.dsh_home)
        elif args.action == "link":
            result = write_data_layer_pth(args.dsh_home)
            if result is None:
                print("Data layer not linked: run `install` first, and make sure the trading venv exists.",
                      file=sys.stderr)
                return 1
            site, lines = result
            print(f"Linked data layer into {site} -> {', '.join(lines)}")
        else:
            install_plugins(args.repo, args.dsh_home)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
