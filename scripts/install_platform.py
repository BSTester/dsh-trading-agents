#!/usr/bin/env python3
"""量化平台安装器（WP7 任务 5）：幂等、可分层跳过、每步输出一行 JSON 摘要。

用法：
    python scripts/install_platform.py --home <DSH_HOME> \
        [--skip-venv] [--skip-deps] [--skip-web] [--skip-service] \
        [--skip-all] [--dry-run]

步骤（每步幂等；Harness 解析形如 {"step","ok","detail"} 的摘要行）：
    venv     <home>/trading-venv 不存在才创建（已存在 → already-exists，不重建）
    deps     <venv-python> -m pip install -r platform/requirements.txt（pip 天然幂等）
    web      npm install --prefix platform/web + build（dist 比 src 新 → 跳过重建）
    service  不自动常驻（WP7 决策）：打印启动命令与 ~/.dsh/trading-platform.json
             配置样例；端口已有服务 → already-running
    verify   服务在跑则 GET /healthz 并提示 mode/tools；失败仅 warning，不阻塞安装

设计约定：
    * 纯函数尽量与副作用分离（venv_python/web_needs_build/build_plan/service_config），
      测试（tests/test_core_wp7_install.py）全部离线，执行路径靠注入 runner/probe/health。
    * main(argv, runner, probe, health)：runner(argv)->exit code、probe(port)->bool、
      health(url)->dict 皆可注入——生产默认 subprocess.run / socket / urllib。
    * 退出码：任一步 ok=False → 1；warning（verify 不健康）不算失败。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8397
# 仓库标记文件：platform-autostart 插件据此定位仓库；与 install_plugins.py 双保险。
REPO_MARKER_NAME = "trading-platform-repo"


# ── 纯函数（离线可测） ────────────────────────────────────────────────────────

def venv_dir(home):
    """venv 根目录：<home>/trading-venv。"""
    return Path(home) / "trading-venv"


def venv_python(home, is_windows=None):
    """venv 解释器路径。Windows 分支用纯参数注入，Linux 上也能测（任务书要求）。"""
    if is_windows is None:
        is_windows = os.name == "nt"
    leaf = ("Scripts", "python.exe") if is_windows else ("bin", "python")
    return venv_dir(home).joinpath(*leaf)


def requirements_path(repo_root=REPO_ROOT):
    return Path(repo_root) / "platform" / "requirements.txt"


def web_root(repo_root=REPO_ROOT):
    return Path(repo_root) / "platform" / "web"


def web_needs_build(web):
    """dist/index.html 缺失，或 src/ 下任一文件比它新 → 需要重建（幂等判断）。"""
    dist_entry = Path(web) / "dist" / "index.html"
    if not dist_entry.exists():
        return True
    dist_mtime = dist_entry.stat().st_mtime
    src = Path(web) / "src"
    if not src.exists():
        return False
    return any(p.is_file() and p.stat().st_mtime > dist_mtime for p in src.rglob("*"))


def port_in_use(port, host="127.0.0.1"):
    """loopback 端口探测（只读，不产生副作用）。"""
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, int(port))) == 0


def service_config(home):
    """读取 <home>/trading-platform.json 的 service 节（语义对齐 platform/server/config.py
    的最小子集；非法值回落默认，不抛错——安装器只用来打印提示与探测端口）。"""
    merged = {"port": DEFAULT_PORT, "host": "127.0.0.1", "token": None}
    try:
        raw = json.loads((Path(home) / "trading-platform.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    service = raw.get("service") if isinstance(raw, dict) else None
    if isinstance(service, dict):
        port = service.get("port")
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            merged["port"] = port
        host = service.get("host")
        if isinstance(host, str) and host:
            merged["host"] = host
        token = service.get("token")
        if isinstance(token, str) and token:
            merged["token"] = token
    return merged


def describe_health(payload):
    """/healthz 响应 → 人读提示。健康端点只有 mode/scheduler，没有 tools 数——
    工具面只能提示（mcp__quantwb__* 在 Harness 新会话加载 quant-platform-mcp 行后可见），
    不编造数字。"""
    mode = payload.get("mode") if isinstance(payload, dict) else None
    if not isinstance(mode, str) or not mode:
        mode = "unknown"
    return (f"mode={mode}; tools=mcp__quantwb__*"
            "（工具面需在 Harness 新会话加载 quant-platform-mcp 行后可见）")


def build_plan(home, repo_root=REPO_ROOT, *, skip_venv=False, skip_deps=False,
               skip_web=False, skip_service=False):
    """返回固定顺序的步骤计划（纯函数）：[{step, skip, commands, why}]。
    commands 是 argv 列表；service/verify 永不自动启动，故无 commands。"""
    py = str(venv_python(home))
    web = str(web_root(repo_root))
    return [
        {"step": "venv", "skip": skip_venv,
         "commands": [] if skip_venv else [[sys.executable, "-m", "venv", str(venv_dir(home))]],
         "why": "创建 <home>/trading-venv（已存在则跳过，不重建）"},
        {"step": "deps", "skip": skip_deps,
         "commands": [] if skip_deps else
         [[py, "-m", "pip", "install", "-r", str(requirements_path(repo_root))]],
         "why": "平台服务依赖（fastapi/uvicorn/mcp/httpx，pip 幂等）"},
        {"step": "web", "skip": skip_web,
         "commands": [] if skip_web else [
             ["npm", "install", "--prefix", web],
             ["npm", "--prefix", web, "run", "build"]],
         "why": "工作台前端；dist 比 src 新则跳过重建"},
        {"step": "service", "skip": skip_service,
         "commands": [],
         "why": "不自动常驻：打印启动命令与配置样例，探测端口是否已在跑"},
        {"step": "verify", "skip": False,
         "commands": [],
         "why": "服务在跑则 GET /healthz；失败仅 warning，不阻塞安装"},
    ]


# ── 副作用入口（可注入替身） ─────────────────────────────────────────────────

def _default_runner(argv):
    return subprocess.run(list(argv)).returncode


def _fetch_health(url):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback 健康探针
        return json.loads(resp.read().decode("utf-8"))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="install_platform.py",
        description="量化平台一键安装器（幂等、可分层跳过；每步输出 JSON 摘要行）")
    parser.add_argument("--home", required=True,
                        help="DSH_HOME，如 ~/.dsh（venv 与 trading-platform.json 都落在这里）")
    parser.add_argument("--skip-venv", action="store_true", help="跳过 venv 创建")
    parser.add_argument("--skip-deps", action="store_true", help="跳过 pip 依赖安装")
    parser.add_argument("--skip-web", action="store_true", help="跳过 npm install/build")
    parser.add_argument("--skip-service", action="store_true", help="跳过服务提示与端口探测")
    parser.add_argument("--skip-all", action="store_true", help="等价于以上四个 --skip-*")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将执行的命令列表，不执行任何副作用")
    args = parser.parse_args(argv)
    if args.skip_all:  # --skip-all 在解析期展开，parse_args 的返回值即完整意图
        args.skip_venv = args.skip_deps = args.skip_web = args.skip_service = True
    return args


def main(argv=None, runner=None, probe=None, health=None):
    args = parse_args(argv)
    runner = runner or _default_runner
    probe = probe or port_in_use
    health = health or _fetch_health

    home = Path(os.path.abspath(os.path.expanduser(args.home)))
    plan = build_plan(home, REPO_ROOT, skip_venv=args.skip_venv, skip_deps=args.skip_deps,
                      skip_web=args.skip_web, skip_service=args.skip_service)
    config = service_config(home)

    def emit(step, ok, detail):
        print(json.dumps({"step": step, "ok": bool(ok), "detail": detail}, ensure_ascii=False))
        return bool(ok)

    ok_all = True
    if args.dry_run:
        print("安装计划（dry-run：只打印，不执行任何副作用）")
        for step in plan:
            if step["skip"]:
                print(f"  [dry-run] {step['step']}: skipped")
                ok_all &= emit(step["step"], True, "dry-run: skipped")
                continue
            for cmd in step["commands"]:
                print(f"  [dry-run] {' '.join(cmd)}")
            note = f"dry-run: {len(step['commands'])} command(s) planned" if step["commands"] \
                else "dry-run: 提示/探测步，无命令"
            ok_all &= emit(step["step"], True, note)
        return 0 if ok_all else 1

    for step in plan:
        name = step["step"]
        if step["skip"]:
            ok_all &= emit(name, True, "skipped")
            continue

        if name == "venv":
            if venv_python(home).exists():
                print(f"==> venv 已存在：{venv_dir(home)}")
                ok_all &= emit(name, True, "already-exists（幂等：不重建）")
                continue
            print(f"==> 创建 venv：{venv_dir(home)}")
            code = runner(step["commands"][0])
            ok_all &= emit(name, code == 0,
                           "venv created" if code == 0 else f"venv 创建失败（exit {code}）")

        elif name == "deps":
            if not venv_python(home).exists():
                ok_all &= emit(name, False, "venv-missing：先不带 --skip-venv 创建 venv 再装依赖")
                continue
            print(f"==> 安装平台依赖：{requirements_path(REPO_ROOT)}（pip 幂等，已装即快）")
            code = runner(step["commands"][0])
            ok_all &= emit(name, code == 0,
                           "pip install -r platform/requirements.txt" if code == 0
                           else f"pip 安装失败（exit {code}；离线时先配网或手动安装）")

        elif name == "web":
            print(f"==> 前端依赖与构建：{web_root(REPO_ROOT)}")
            code = runner(step["commands"][0])
            if code != 0:
                ok_all &= emit(name, False, f"npm install 失败（exit {code}）")
                continue
            if web_needs_build(web_root(REPO_ROOT)):
                code = runner(step["commands"][1])
                ok_all &= emit(name, code == 0,
                               "npm install + build" if code == 0
                               else f"npm run build 失败（exit {code}）")
            else:
                ok_all &= emit(name, True, "npm install ok；dist 比 src 新，跳过重建（幂等）")

        elif name == "service":
            venv_py = venv_python(home)
            print("==> 服务不自动常驻。启动命令（建议放独立终端或 systemd，见 docs/RUNBOOK.md）：")
            print(f"  cd {REPO_ROOT / 'platform'} && {venv_py} -m server.run")
            token = json.dumps(config["token"])
            print(f"配置样例 {home / 'trading-platform.json'}（可选；文件缺失即默认 loopback:8397）：")
            print(f'  {{ "service": {{ "port": {config["port"]}, '
                  f'"host": "{config["host"]}", "token": {token} }} }}')
            # 仓库标记文件（覆盖写）：会话启动的 platform-autostart 插件据此定位仓库并
            # 自动拉起 platform/server/run.py；内容解析口径见插件 resolvePaths（首行去空白）。
            (home / REPO_MARKER_NAME).write_text(str(REPO_ROOT) + "\n", encoding="utf-8")
            if probe(config["port"]):
                ok_all &= emit(name, True,
                               f"already-running（{config['host']}:{config['port']} 已有服务）")
            else:
                ok_all &= emit(name, True,
                               f"not-running：未自动启动（设计如此），用上方启动命令后重跑本脚本验证")

        elif name == "verify":
            if not probe(config["port"]):
                ok_all &= emit(name, True,
                               "warning: 服务未运行，跳过 /healthz（启动后重跑 "
                               "--skip-venv --skip-deps --skip-web 可单独验证）")
                continue
            try:
                payload = health(f"http://{config['host']}:{config['port']}/healthz")
                ok_all &= emit(name, True, describe_health(payload))
            except Exception as error:  # 健康检查失败不阻塞安装（任务书明确）
                ok_all &= emit(name, True, f"warning: /healthz 校验失败（不阻塞安装）：{error}")

    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
