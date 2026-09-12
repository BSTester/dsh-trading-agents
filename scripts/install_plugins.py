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


PLUGINS = ("workbench", "fin-data", "engine")
PRESET_PACKAGES = {
    "fin-data": "@bstester/dsh-fin-data",
    "trading-engine": "@bstester/dsh-trading-engine",
}


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
        for plugin in PLUGINS:
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
            subprocess.run(commands["dsh"] + ["plugin", "--profile", "web", "add", str(archive)],
                           cwd=repo, env=env, check=True)
    if read_text(preset) != original:
        raise ValueError("Preset changed during installation; refusing to overwrite concurrent edits.")
    if enabled != original:
        write_text(preset, enabled)
    print("Installed web profile Host and plugins; fin-data/trading-engine enabled. "
          "Restart dsh web (npx @deepseek-ai/dsh web).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "update"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dsh-home", type=Path,
                        default=Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))))
    args = parser.parse_args()
    try:
        if args.action == "update":
            update_checkout(args.repo)
        else:
            install_plugins(args.repo, args.dsh_home)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
