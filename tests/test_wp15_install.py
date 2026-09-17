"""WP15 任务 4：值班研究员（L3）唤醒脚本与定时器交付的契约测试。

测的不是「文件存在」本身，而是**可执行的契约**：
  * 脚本真的能跑（bash -n 语法、参数拼装、健康探测、日志落盘）；
  * 唤醒提示里带齐领取/回报工具名与**禁用端点点名清单**（防提示漂移——
    skills/research-institute/SKILL.md 值班模式边界第 2 条）；
  * 失败路径不静默成功：dsh 缺失、服务不可达、超时三种情形各自非零退出 + 可读指引；
  * 定时器单元与脚本接线一致，`OnCalendar` 与文档里写明的时刻依据同源。

全部离线：本机 loopback 起一个只答 /healthz 的假服务，另配假 dsh 记录入参。
"""
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "research_duty.sh"
SERVICE = ROOT / "install" / "research-duty.service"
TIMER = ROOT / "install" / "research-duty.timer"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
HANDOVER = ROOT / "docs" / "HANDOVER.md"
SKILL = ROOT / "skills" / "research-institute" / "SKILL.md"
#: 测试会故意清空 PATH（验「找不到 dsh」分支），因此 bash 用绝对路径定位一次。
BASH = shutil.which("bash") or "/bin/bash"

# 技能里点名的禁用写端点（值班执行体一个都不许调）——脚本提示词必须逐条重申。
FORBIDDEN_ENDPOINTS = ("trade_place", "trade_modify", "trade_cancel", "plan_execute",
                       "switch_mode", "auto_pipeline", "rules-decide", "confirm-decide")


def _read(path):
    return path.read_text(encoding="utf-8")


class _HealthzHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 —— http.server 的接口名
        if self.path == "/healthz":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *_args):  # 测试输出保持干净
        return


class _FakeService:
    """只答 /healthz 的 loopback 假服务（脚本探测用）。"""

    def __enter__(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _HealthzHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _closed_port_url():
    """拿一个刚关闭的端口——连接必然被拒，用于「服务不可达」分支。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class ResearchDutyScriptTest(unittest.TestCase):
    def test_script_is_valid_bash_and_wired(self):
        text = _read(SCRIPT)
        result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # 唤醒入口：DSH 无头单次运行（不是常驻、不是自建循环）
        self.assertIn("--profile", text)
        self.assertIn("headless", text)
        # 单次运行总时长上限
        self.assertIn("timeout", text)
        self.assertIn("DSH_DUTY_TIMEOUT", text)
        # 领取→处理→回报循环与队列空即收工
        self.assertIn("research_tasks_claim", text)
        self.assertIn("research_tasks_report", text)
        self.assertIn("task = null", text)
        # 提示词必须重申禁用端点（防漂移）
        for endpoint in FORBIDDEN_ENDPOINTS:
            self.assertIn(endpoint, text, f"唤醒提示缺少禁用端点 {endpoint}")
        # 指向技能手册与排障文档
        self.assertIn("research-institute", text)
        self.assertIn("RUNBOOK", text)

    def test_skill_boundary_and_script_agree(self):
        skill = _read(SKILL)
        for endpoint in FORBIDDEN_ENDPOINTS:
            self.assertIn(endpoint, skill, f"技能边界未点名 {endpoint}")

    def test_units_exist_and_are_wired_to_script(self):
        service, timer = _read(SERVICE), _read(TIMER)
        self.assertIn("ExecStart=", service)
        self.assertIn("research_duty.sh", service)
        self.assertIn("Type=oneshot", service)
        # service **不带** [Install]：只有 timer 能拉起它——否则 enable service 会让值班
        # 在每次登录时跑一次（L3 是定时/手动动作，不是登录动作）。
        self.assertNotIn("WantedBy=default.target", service)
        self.assertIn("OnCalendar=", timer)
        self.assertIn("research-duty.service", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("Persistent=true", timer)
        # 时刻依据必须写在单元里（改时刻的人得知道为什么是这个点）：唤醒**晚于**
        # 基础链入队（19:05），而入队又晚于对账写 digest（19:00）——审查 A-2 的时序链
        self.assertIn("OnCalendar=Mon..Fri 19:20", timer)
        self.assertIn("enqueue_research", timer)
        self.assertIn("19:05", timer)
        self.assertIn("19:00", timer)
        self.assertIn("20 19 * * 1-5", timer, "cron 等价行必须与新时刻一致")

    def test_docs_document_enable_and_troubleshooting(self):
        runbook, handover = _read(RUNBOOK), _read(HANDOVER)
        for token in ("research-duty.timer", "research_duty.sh", "值班"):
            self.assertIn(token, runbook)
        self.assertIn("值班", handover)


class ResearchDutyRunTest(unittest.TestCase):
    """真跑脚本：失败路径必须非零退出 + 有可读指引。"""

    def _env(self, **overrides):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
            "DSH_DUTY_PYTHON": sys.executable,
        }
        env.update(overrides)
        return env

    def test_missing_dsh_fails_with_guidance(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [BASH, str(SCRIPT)],
                capture_output=True, text=True, timeout=60,
                env=self._env(PATH="", DSH_HOME=home, DSH_BIN=""))
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("dsh", combined)
        self.assertIn("RUNBOOK", combined)

    def test_service_unreachable_fails_with_guidance(self):
        with tempfile.TemporaryDirectory() as home:
            fake_dsh = Path(home) / "fake-dsh"
            fake_dsh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_dsh.chmod(0o755)
            result = subprocess.run(
                [BASH, str(SCRIPT)],
                capture_output=True, text=True, timeout=60,
                env=self._env(DSH_HOME=home, DSH_BIN=str(fake_dsh),
                              DSH_DUTY_BASE_URL=_closed_port_url()))
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("服务", combined)
        self.assertIn("RUNBOOK", combined)

    def test_happy_path_invokes_headless_with_contract_prompt(self):
        with _FakeService() as base_url, tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            args_file = home_path / "dsh-args.txt"
            fake_dsh = home_path / "fake-dsh"
            fake_dsh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_DSH_ARGS\"\n"
                "echo 'fake-dsh done'\n"
                "exit 0\n", encoding="utf-8")
            fake_dsh.chmod(0o755)
            result = subprocess.run(
                [BASH, str(SCRIPT)],
                capture_output=True, text=True, timeout=60,
                env=self._env(DSH_HOME=home, DSH_BIN=str(fake_dsh),
                              DSH_DUTY_BASE_URL=base_url,
                              FAKE_DSH_ARGS=str(args_file)))
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = args_file.read_text(encoding="utf-8").splitlines()
            self.assertIn("--profile", argv)
            self.assertIn("headless", argv)
            prompt = "\n".join(argv)
            for token in ("research_tasks_claim", "research_tasks_report", "task = null"):
                self.assertIn(token, prompt)
            for endpoint in FORBIDDEN_ENDPOINTS:
                self.assertIn(endpoint, prompt)
            # 日志落盘（运维要能事后看这次值班发生了什么）
            logs = list((home_path / "logs").glob("research-duty-*.log"))
            self.assertEqual(len(logs), 1, logs)
            self.assertIn("fake-dsh done", logs[0].read_text(encoding="utf-8"))

    def test_timeout_aborts_nonzero(self):
        with _FakeService() as base_url, tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            fake_dsh = home_path / "fake-dsh"
            fake_dsh.write_text("#!/usr/bin/env bash\nsleep 30\n", encoding="utf-8")
            fake_dsh.chmod(0o755)
            result = subprocess.run(
                [BASH, str(SCRIPT)],
                capture_output=True, text=True, timeout=90,
                env=self._env(DSH_HOME=home, DSH_BIN=str(fake_dsh),
                              DSH_DUTY_BASE_URL=base_url, DSH_DUTY_TIMEOUT="1"))
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("上限", combined)
        self.assertIn("队列", combined)  # 明确未完成任务留在队列


if __name__ == "__main__":
    unittest.main()
