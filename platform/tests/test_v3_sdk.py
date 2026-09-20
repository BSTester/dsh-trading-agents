"""``server/v3_sdk.py`` 契约测试（V3 FR-GATEWAY-002 SDK JSON-RPC 会话客户端）。

全部离线：**没有真机 dsh 子进程**。会话侧用一个基于 ``os.pipe()`` 的脚本化假进程
（``FakeDsh``），它按真实协议回 ``initialize`` / ``session/prompt`` / ``shutdown`` 的响应，
并流式发出 ``session.event`` / ``session.status`` 通知——所以分帧、半包、粘包、超大行、
握手超时、异常退出、事件解析这些都是**在真字节流上**跑出来的，不是对 mock 断言 mock。

覆盖面（逐条对任务书）：

  * 帧解析：一条一帧 / 半包 / 粘包 / 空行 / 畸形行 / 超大行（丢弃并计数，不撑爆内存）；
  * 握手：成功时逐字比对 ``deepseek-harness-sdk-runtime``；**标识不符时如实报不符**；
    超时如实报超时（带 elapsed / 观测到的 stderr / exit code），绝不当成就绪；
  * 进程异常退出：``exit_code`` + ``stderr`` 原文 + 拒发提示词；
  * 口令缺失拒绝：HTTP 层 fail-closed、**不启进程**、**拒绝也落审计**；
  * 白名单确实排除交易工具：策略函数 + 平台真实工具目录审计 + **与 profile 插件
    ``tool-whitelist/index.js`` 的拒绝集逐字一致**（漂移即红）；
  * 事件解析：``session.event`` → 归一化（assistant 文本、turn/end 原因、raw 截断）；
  * HTTP 三端点：``GET /api/v3/sdk/status``、``GET /api/v3/sdk/sessions``、
    ``POST /api/v3/sdk/prompt``（口令 + 审计 + 真实回执）。

真机测试（要真起 ``dsh --profile quant-sdk``）**默认 skip**：它需要独立 ``DSH_HOME``、
已配置的模型路由与官方 SDK 包。设 ``QUANT_SDK_REAL=1`` 且 ``QUANT_SDK_REAL_HOME=<dir>``
时才跑——skip 的理由写在用例里，绝不假装跑过。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_sdk -v``
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import mcp_tools, v3_sdk  # noqa: E402

REPO = ROOT.parent
PLUGIN_JS = REPO / "platform" / "install" / "quant-sdk" / "tool-whitelist" / "index.js"
WHITELIST_JSON = REPO / "platform" / "install" / "quant-sdk" / "tool-whitelist.json"
PATCH_YML = REPO / "platform" / "install" / "quant-sdk" / "cordis.patch.yml"
PROFILE_JSON = REPO / "platform" / "install" / "quant-sdk" / "package.json"

#: 任务书点名的「必须排除」清单（逐条断言，不用模式推断代替）。
REQUIRED_EXCLUDED = (
    "trade_place", "trade_modify", "trade_cancel", "trade_max_qty",
    "sim_trade_order_list", "sim_trade_history_order_list", "sim_trade_max_buy_sell",
    "plan-execute", "plan_execute",
    "confirm-decide", "confirm_decide",
    "switch-mode", "switch_mode",
    "v3_credentials", "v3_oms_sync",
)


# ---------------------------------------------------------------------------
# 假 dsh 子进程（真 os.pipe 字节流 + 真线程，按协议应答）
# ---------------------------------------------------------------------------

class FakeDsh:
    """既是 PopenLike（有 stdin/stdout/stderr/pid/poll/wait/terminate/kill），

    也是 spawn 回调（``spawn(argv, env, cwd)`` 返回自己）。``respond=False`` 时只收不回，
    用来测握手超时。
    """

    def __init__(self, server_name=v3_sdk.SPEC_SERVER_NAME, respond=True,
                 stderr_on_exit="", version="0.0.1", prompt_error=None):
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()
        err_r, err_w = os.pipe()
        self._to_w = to_w
        self._err_w = err_w
        self.stdout = os.fdopen(to_r, "rb")
        self.stderr = os.fdopen(err_r, "rb")
        self.stdin = os.fdopen(from_w, "wb")
        self._client_reader = os.fdopen(from_r, "rb")
        self.pid = 990001
        self.returncode = None
        self.respond = respond
        self.server_name = server_name
        self.version = version
        #: 非 None 时对 session/prompt 回这个错误（测 wire 错误如实上报）
        self.prompt_error = prompt_error
        self.stderr_on_exit = stderr_on_exit
        self.frames = []
        self.prompt_count = 0
        self.argv = None
        self.env = None
        self.cwd = None
        self._lock = threading.Lock()
        self._serve = threading.Thread(target=self._serve_loop, name="fake-dsh", daemon=True)
        self._serve.start()

    # ── spawn 回调 ──────────────────────────────────────────────────────────
    def spawn(self, argv, env, cwd):
        self.argv = list(argv)
        self.env = dict(env or {})
        self.cwd = cwd
        return self

    # ── PopenLike ───────────────────────────────────────────────────────────
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.time() + float(timeout or 0)
        while self.returncode is None and time.time() < deadline:
            time.sleep(0.01)
        return self.returncode

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)

    # ── 脚本控制 ────────────────────────────────────────────────────────────
    def send(self, obj):
        os.write(self._to_w, (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def feed_raw(self, data):
        os.write(self._to_w, data if isinstance(data, bytes) else data.encode("utf-8"))

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def emit_stderr(self, text):
        os.write(self._err_w, text.encode("utf-8"))

    def finish(self, code=0):
        """收尾：写 stderr 原文 + 记 exit code + 关 stdout（EOF）。"""
        if self.returncode is not None:
            return
        if self.stderr_on_exit:
            self.emit_stderr(self.stderr_on_exit)
        self.returncode = code
        # 让 stderr 线程先把原文收进环里，再关 stdout（否则上报的 stderr 可能晚一拍）。
        time.sleep(0.08)
        try:
            os.close(self._err_w)
        except OSError:
            pass
        try:
            os.close(self._to_w)
        except OSError:
            pass

    def close(self):
        """关掉所有管道（tearDown 调用；重复关闭安全）。

        **顺序是关键**：先关客户端→服务端的**写端**（解开服务线程阻塞中的 ``readline``），
        再关服务端→客户端的**写端**（让客户端读线程拿到 EOF），最后 join 两个线程再关读端。
        反过来先关读端会与阻塞中的读线程抢 ``BufferedReader`` 的锁而挂死（踩过）。
        """
        try:
            self.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        for fd in (self._to_w, self._err_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._serve.join(timeout=1.0)
        time.sleep(0.05)
        for stream in (self.stdout, self.stderr, self._client_reader):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def received(self, method=None):
        with self._lock:
            items = list(self.frames)
        if method is None:
            return items
        return [item for item in items if item.get("method") == method]

    # ── 协议应答 ────────────────────────────────────────────────────────────
    def _serve_loop(self):
        while True:
            try:
                line = self._client_reader.readline()
            except (ValueError, OSError):
                return
            if not line:
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._lock:
                self.frames.append(message)
            if not self.respond:
                continue
            method = message.get("method")
            if method == "initialize":
                self.send({"jsonrpc": "2.0", "id": message.get("id"),
                           "result": {"serverInfo": {"name": self.server_name,
                                                     "version": self.version}}})
            elif method == "session/prompt":
                self._answer_prompt(message)
            elif method == "shutdown":
                self.send({"jsonrpc": "2.0", "id": message.get("id"), "result": {}})
                self.finish(0)
                return

    def _answer_prompt(self, message):
        session_id = message["params"]["sessionId"]
        text = message["params"]["contentBlocks"][0]["text"]
        if self.prompt_error is not None:
            self.send({"jsonrpc": "2.0", "id": message.get("id"),
                       "error": self.prompt_error})
            return
        self.prompt_count += 1
        self.send({"jsonrpc": "2.0", "id": message.get("id"),
                   "result": {"messageId": f"msg-{self.prompt_count}"}})
        self.notify("session.event", {"sessionId": session_id, "event": {
            "type": "user/message", "seq": 0, "time": 1,
            "data": {"role": "user", "content": [{"type": "text", "text": text}]}}})
        self.notify("session.status", {"sessionId": session_id, "status": "running"})
        self.notify("session.event", {"sessionId": session_id, "event": {
            "type": "assistant/message", "seq": 1, "time": 2,
            "data": {"message": {"role": "assistant",
                                 "content": [{"type": "reasoning", "text": "想"},
                                             {"type": "text", "text": "OK"}]}}}})
        self.notify("session.event", {"sessionId": session_id, "event": {
            "type": "turn/end", "seq": 2, "time": 3,
            "data": {"turn": 1, "reason": {"kind": "completed"}}}})
        self.notify("session.status", {"sessionId": session_id, "status": "idle"})


class SdkTestCase(unittest.TestCase):
    """公共装置：临时 home + 环境变量固定（免被宿主环境串味）+ 注册表复位。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="v3-sdk-test-")
        self.home = self._tmp.name
        self.iso = os.path.join(self.home, "iso")
        os.makedirs(self.iso, exist_ok=True)
        self._env = unittest.mock.patch.dict(os.environ, {
            "QUANT_SDK_PROFILE": "quant-sdk",
            "QUANT_SDK_DSH_HOME": self.iso,
            "QUANT_SDK_PROVIDER": "deepseek-official",
            "QUANT_SDK_MODEL": "deepseek-flash",
        })
        self._env.start()
        v3_sdk.reset_registry()
        self._runtimes = []
        self._fakes = []

    def tearDown(self):
        for runtime in self._runtimes:
            try:
                if runtime.running:
                    runtime.stop(timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
        for fake in self._fakes:
            fake.close()
        v3_sdk.reset_registry()
        self._env.stop()
        self._tmp.cleanup()

    def make_runtime(self, fake, **kwargs):
        self._fakes.append(fake)
        options = dict(profile="quant-sdk", dsh_home=self.iso, cwd=str(REPO),
                       handshake_timeout=5.0, prompt_timeout=5.0, stop_timeout=3.0)
        options.update(kwargs)
        runtime = v3_sdk.SdkRuntime(spawn=fake.spawn, **options)
        self._runtimes.append(runtime)
        return runtime

    def make_app(self, fake):
        self._fakes.append(fake)
        v3_sdk.get_runtime(self.home, spawn=fake.spawn)
        app = FastAPI()
        v3_sdk.register(app, None, self.home)
        return app


# ---------------------------------------------------------------------------
# 1. 帧解析：分帧 / 半包 / 粘包 / 空行 / 畸形行 / 超大行
# ---------------------------------------------------------------------------

class FramerTest(unittest.TestCase):

    def test_single_frame(self):
        framer = v3_sdk.JsonRpcFramer()
        self.assertEqual(framer.feed(b'{"id":1}\n'), [{"id": 1}])
        self.assertEqual(framer.frames, 1)
        self.assertEqual(framer.pending_bytes, 0)

    def test_half_packet_is_buffered_until_newline(self):
        framer = v3_sdk.JsonRpcFramer()
        self.assertEqual(framer.feed(b'{"jsonrpc":"2.0","id":"req_1",'), [])
        self.assertEqual(framer.pending_bytes, 30)   # 半包留在缓冲里，不吐假帧
        self.assertEqual(framer.frames, 0)
        out = framer.feed(b'"result":{"a":1}}\n')
        self.assertEqual(out, [{"jsonrpc": "2.0", "id": "req_1", "result": {"a": 1}}])
        self.assertEqual(framer.pending_bytes, 0)

    def test_glued_packets_split_into_frames(self):
        framer = v3_sdk.JsonRpcFramer()
        out = framer.feed(b'{"id":1}\n{"id":2}\n{"id":3}')
        self.assertEqual([item["id"] for item in out], [1, 2])
        self.assertEqual(framer.frames, 2)          # 第三条还没换行，不算帧
        self.assertEqual(framer.feed(b"\n"), [{"id": 3}])
        self.assertEqual(framer.frames, 3)

    def test_byte_at_a_time_reassembles(self):
        framer = v3_sdk.JsonRpcFramer()
        payload = b'{"jsonrpc":"2.0","method":"session.status","params":{"status":"idle"}}\n'
        out = []
        for index in range(len(payload)):
            out.extend(framer.feed(payload[index:index + 1]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["params"]["status"], "idle")

    def test_crlf_and_blank_lines_and_whitespace(self):
        framer = v3_sdk.JsonRpcFramer()
        out = framer.feed(b'{"id":1}\r\n\n   \n{"id":2}\n')
        self.assertEqual([item["id"] for item in out], [1, 2])
        self.assertEqual(framer.malformed, 0)

    def test_malformed_lines_are_ignored_and_counted(self):
        framer = v3_sdk.JsonRpcFramer()
        out = framer.feed(b'not json\n{"id":1}\n[1,2,3]\n{"id":2}\n')
        self.assertEqual([item["id"] for item in out], [1, 2])
        self.assertEqual(framer.malformed, 2)       # 非法 JSON + 非对象

    def test_oversized_line_is_dropped_and_counted_without_buffering(self):
        framer = v3_sdk.JsonRpcFramer(max_line_bytes=64)
        framer.feed(b"x" * 100)                     # 半包即超限 → 整段丢弃
        self.assertEqual(framer.pending_bytes, 0)   # 缓冲被清空（不会撑爆内存）
        self.assertEqual(framer.overflows, 1)
        framer.feed(b"y" * 50)                      # 还在丢状态：这段要到换行才结束
        self.assertEqual(framer.overflows, 1)
        out = framer.feed(b'zzz\n{"id":1}\n')
        self.assertEqual([item["id"] for item in out], [1])
        self.assertEqual(framer.overflows, 1)       # 超限行只计一次
        self.assertEqual(framer.frames, 1)

    def test_oversized_complete_line_is_dropped(self):
        framer = v3_sdk.JsonRpcFramer(max_line_bytes=16)
        big = ("x" * 40).encode()
        out = framer.feed(big + b'\n{"id":7}\n')
        self.assertEqual([item["id"] for item in out], [7])
        self.assertEqual(framer.overflows, 1)

    def test_str_input_accepted(self):
        framer = v3_sdk.JsonRpcFramer()
        self.assertEqual(framer.feed('{"id":"a"}\n'), [{"id": "a"}])


# ---------------------------------------------------------------------------
# 2. 握手：成功 / 标识不符 / 超时
# ---------------------------------------------------------------------------

class HandshakeTest(SdkTestCase):

    def test_handshake_matches_spec_identity(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        result = runtime.handshake(timeout=5.0)
        self.assertEqual(result["state"], "ok")
        self.assertTrue(result["protocol_match"])
        self.assertEqual(result["observed"], "deepseek-harness-sdk-runtime")
        self.assertEqual(result["expected"], v3_sdk.SPEC_SERVER_NAME)
        self.assertEqual(result["version"], "0.0.1")
        self.assertTrue(runtime.handshake_ok())
        # 真发出去了 initialize，且参数是显式的 provider/model/cwd
        sent = fake.received("initialize")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["params"]["provider"], "deepseek-official")
        self.assertEqual(sent[0]["params"]["model"], "deepseek-flash")
        self.assertEqual(sent[0]["params"]["cwd"], str(REPO))

    def test_handshake_reports_mismatch_verbatim(self):
        fake = FakeDsh(server_name="some-other-runtime")
        runtime = self.make_runtime(fake)
        runtime.start()
        result = runtime.handshake(timeout=5.0)
        # 如实报不符：不抛异常伪装成功，也不把 observed 改写成期望值
        self.assertEqual(result["state"], "protocol-mismatch")
        self.assertFalse(result["protocol_match"])
        self.assertEqual(result["observed"], "some-other-runtime")
        self.assertEqual(result["expected"], "deepseek-harness-sdk-runtime")
        self.assertFalse(runtime.handshake_ok())

    def test_mismatched_handshake_refuses_to_send_prompt(self):
        fake = FakeDsh(server_name="not-the-spec-name")
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        with self.assertRaises(v3_sdk.SdkProtocolMismatch) as caught:
            runtime.enqueue_prompt("s-1", "只回复 OK")
        self.assertIn("拒发", str(caught.exception))
        self.assertEqual(fake.received("session/prompt"), [])

    def test_handshake_reports_missing_result_shape(self):
        fake = FakeDsh(server_name=None)   # 返回 {"name": null}
        runtime = self.make_runtime(fake)
        runtime.start()
        result = runtime.handshake(timeout=5.0)
        self.assertFalse(result["protocol_match"])
        self.assertIsNone(result["observed"])

    def test_handshake_timeout_is_reported_as_timeout(self):
        fake = FakeDsh(respond=False)
        runtime = self.make_runtime(fake)
        runtime.start()
        started = time.time()
        with self.assertRaises(v3_sdk.SdkHandshakeTimeout) as caught:
            runtime.handshake(timeout=0.4)
        elapsed = time.time() - started
        self.assertLess(elapsed, 5.0)
        error = caught.exception.to_dict()
        self.assertEqual(error["code"], "v3-sdk/handshake-timeout")
        self.assertIn("不得视为就绪", error["message"])
        self.assertFalse(runtime.handshake_ok())
        self.assertEqual(runtime.status()["handshake"]["state"], "timeout")

    def test_prompt_without_handshake_is_refused(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        with self.assertRaises(v3_sdk.SdkProtocolMismatch):
            runtime.enqueue_prompt("s-1", "hi")
        self.assertEqual(fake.received("session/prompt"), [])

    def test_prompt_on_stopped_runtime_is_refused(self):
        runtime = self.make_runtime(FakeDsh())
        with self.assertRaises(v3_sdk.SdkNotRunning):
            runtime.handshake()
        with self.assertRaises(v3_sdk.SdkProcessExited):
            runtime.enqueue_prompt("s-1", "hi")

    def test_rpc_error_frame_becomes_reported_error(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        # 假进程对 initialize 回一个错误帧（-32603）
        fake.respond = False
        waiter = threading.Thread(target=lambda: (
            time.sleep(0.15),
            fake.send({"jsonrpc": "2.0", "id": fake.received("initialize")[0]["id"],
                       "error": {"code": -32603, "message": "adapter unavailable"}})), daemon=True)
        waiter.start()
        with self.assertRaises(v3_sdk.SdkRpcError) as caught:
            runtime.handshake(timeout=5.0)
        self.assertEqual(caught.exception.detail["rpc_code"], -32603)
        self.assertIn("adapter unavailable", str(caught.exception))


# ---------------------------------------------------------------------------
# 3. 会话：下发回执 / 事件流 / 状态转换 / 异常退出
# ---------------------------------------------------------------------------

class SessionTest(SdkTestCase):

    def wait_prompts_done(self, runtime, session_id, count, timeout=5.0):
        """等到该会话恰好 ``count`` 条提示词都进入终态。

        为什么需要它：假进程回答极快，``enqueue_prompt`` 返回后事件流可能还没走完
        （2026-09-20 修：原先直接断言 ``state == "done"`` 属真实竞态、会偶发红）。
        **只等，不放宽任何断言口径。**
        """
        deadline = time.time() + timeout
        while True:
            view = runtime.sessions_view(session_id)[0]
            states = [item["state"] for item in view["prompts"]]
            if len(states) == count and all(s == "done" for s in states):
                return view
            if time.time() >= deadline:
                return view
            time.sleep(0.01)

    def test_prompt_returns_server_receipt_and_streams_events(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        receipt = runtime.enqueue_prompt("quant-001", "只回复 OK")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["session_id"], "quant-001")
        # 回执状态是**真实观测**：假进程回得极快，所以可能是 sent（刚发出）或 done（轮次已结束）
        self.assertIn(receipt["state"], ("sent", "done"))
        self.assertEqual(receipt["message_id"], "msg-1")   # 服务器的入队回执，非执行结果
        view = self.wait_prompts_done(runtime, "quant-001", 1)
        self.assertEqual(view["status"], "idle")
        self.assertEqual(view["prompts"][-1]["state"], "done")
        types = [event["type"] for event in view["events"]]
        self.assertEqual(types, ["user/message", "assistant/message", "turn/end"])
        statuses = [item["status"] for item in view["status_history"]]
        self.assertEqual(statuses, ["running", "idle"])

    def test_prompt_queue_is_serial_per_session(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        first = runtime.enqueue_prompt("quant-002", "第一条")
        second = runtime.enqueue_prompt("quant-002", "第二条")
        self.assertEqual(first["session_id"], second["session_id"])
        view = self.wait_prompts_done(runtime, "quant-002", 2)
        states = [item["state"] for item in view["prompts"]]
        self.assertEqual(states, ["done", "done"])
        self.assertEqual(len(fake.received("session/prompt")), 2)
        # 串行：第二条的 turn 基线必须晚于第一条的 turn/end
        self.assertEqual([item["_turn_baseline"] for item in runtime.session("quant-002").prompts],
                         [0, 1])

    def test_session_already_exists_error_keeps_wire_text_and_adds_hint(self):
        """真机实测的协议行为：同一个 session_id 跨进程会撞已存在的会话。"""
        fake = FakeDsh(prompt_error={
            "code": -32603, "message": 'session "quant-001" already exists'})
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        runtime.enqueue_prompt("quant-001", "hi", wait_receipt=1.0)
        view = runtime.sessions_view("quant-001")[0]
        item = view["prompts"][-1]
        self.assertEqual(item["state"], "failed")
        # wire 原文一字不改地留着（-32603 的 message）
        self.assertIn('already exists', item["error"]["message"])
        self.assertEqual(item["error"]["rpc_code"], -32603)
        self.assertTrue(item["error"]["session_exists"])
        self.assertIn("换一个 session_id", item["error"]["hint"])

    def test_empty_and_oversized_prompts_are_refused(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        with self.assertRaises(v3_sdk.SdkError):
            runtime.enqueue_prompt("s", "   ")
        with self.assertRaises(v3_sdk.SdkError):
            runtime.enqueue_prompt("s", "x" * (v3_sdk.MAX_PROMPT_CHARS + 1))
        self.assertEqual(fake.received("session/prompt"), [])

    def test_process_exit_reports_exit_code_and_stderr_verbatim(self):
        fake = FakeDsh(stderr_on_exit="fatal: no model route for provider deepseek-official")
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        fake.finish(3)                     # 非零退出 + stderr 原文
        deadline = time.time() + 5
        while time.time() < deadline and runtime.status()["state"] == "running":
            time.sleep(0.02)
        status = runtime.status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exit_code"], 3)
        self.assertIn("no model route", status["last_error"]["stderr"])
        self.assertEqual(status["last_error"]["exit_code"], 3)
        # 已经死掉的 runtime 不得再被当成「可以下发」
        with self.assertRaises(v3_sdk.SdkProcessExited) as caught:
            runtime.enqueue_prompt("s-1", "hi")
        self.assertEqual(caught.exception.detail["exit_code"], 3)
        self.assertIn("no model route", caught.exception.detail["stderr"])

    def test_clean_exit_is_reported_as_exited_not_failed(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        fake.finish(0)
        deadline = time.time() + 5
        while time.time() < deadline and runtime.status()["state"] == "running":
            time.sleep(0.02)
        status = runtime.status()
        self.assertEqual(status["state"], "exited")
        self.assertEqual(status["exit_code"], 0)
        self.assertIn("exit_code=0", status["last_error"]["message"])

    def test_shutdown_round_trip_reports_real_exit_code(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        outcome = runtime.stop()
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertFalse(outcome["forced"])
        self.assertEqual(len(fake.received("shutdown")), 1)

    def test_handshake_timeout_then_process_death_blocks_further_prompts(self):
        fake = FakeDsh(respond=False, stderr_on_exit="boom: agent factory failed")
        runtime = self.make_runtime(fake)
        runtime.start()
        with self.assertRaises(v3_sdk.SdkHandshakeTimeout):
            runtime.handshake(timeout=0.3)          # 未就绪：如实报超时
        # 手动把握手标成成功，验证「握手后进程死掉」这条独立路径
        runtime._handshake = {"state": "ok", "protocol_match": True,
                              "observed": v3_sdk.SPEC_SERVER_NAME}
        fake.finish(2)
        deadline = time.time() + 5
        while time.time() < deadline and runtime.status()["state"] == "running":
            time.sleep(0.02)
        self.assertEqual(runtime.status()["state"], "failed")
        with self.assertRaises(v3_sdk.SdkProcessExited) as caught:
            runtime.enqueue_prompt("s-1", "hi", wait_receipt=0.2)
        self.assertEqual(caught.exception.detail["exit_code"], 2)
        self.assertIn("boom: agent factory failed", caught.exception.detail["stderr"])
        # 会话没建成（拒绝发生在建会话之前）→ 列表如实为空，不编造会话
        self.assertEqual(runtime.sessions_view("s-1"), [])
        status = runtime.status()
        self.assertEqual(status["last_error"]["exit_code"], 2)
        self.assertIn("boom: agent factory failed", status["last_error"]["stderr"])

    def test_server_request_gets_method_not_found(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        runtime.handshake(timeout=5.0)
        # 协议声明服务端从不发请求；真收到就按 JSON-RPC 规范回 -32601（不静默吞掉）
        fake.send({"jsonrpc": "2.0", "id": "srv_1", "method": "approval/request",
                   "params": {}})
        deadline = time.time() + 5
        answers = []
        while time.time() < deadline and not answers:
            answers = [item for item in fake.received()
                       if item.get("id") == "srv_1" and "error" in item]
            time.sleep(0.02)
        self.assertTrue(answers, "没有对服务端请求回 -32601")
        self.assertEqual(answers[0]["error"]["code"], -32601)
        self.assertEqual(runtime.status()["counters"]["server_requests"], 1)

    def test_malformed_line_does_not_break_the_stream(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.start()
        fake.feed_raw(b"<<< not json >>>\n")
        runtime.handshake(timeout=5.0)
        self.assertTrue(runtime.handshake_ok())
        self.assertGreaterEqual(runtime.status()["framer"]["malformed_lines"], 1)


# ---------------------------------------------------------------------------
# 4. 事件解析
# ---------------------------------------------------------------------------

class EventParsingTest(unittest.TestCase):

    def test_assistant_text_extracted_reasoning_not(self):
        summary = v3_sdk.summarize_event({
            "type": "assistant/message", "seq": 15, "time": 1789912548914,
            "data": {"message": {"role": "assistant", "content": [
                {"type": "reasoning", "text": "内部思考"},
                {"type": "text", "text": "结论：OK"}]}}})
        self.assertEqual(summary["type"], "assistant/message")
        self.assertEqual(summary["seq"], 15)
        self.assertEqual(summary["text"], "结论：OK")
        self.assertNotIn("内部思考", summary["text"])

    def test_user_message_text(self):
        summary = v3_sdk.summarize_event({
            "type": "user/message", "seq": 8, "time": 1,
            "data": {"role": "user", "content": [{"type": "text", "text": "分析当前持仓风险"}]}})
        self.assertEqual(summary["text"], "分析当前持仓风险")

    def test_turn_end_carries_reason(self):
        summary = v3_sdk.summarize_event({
            "type": "turn/end", "seq": 17, "time": 1,
            "data": {"turn": 1, "reason": {"kind": "completed"}}})
        self.assertIn("completed", summary["text"])

    def test_unknown_event_keeps_raw_and_flags_truncation(self):
        summary = v3_sdk.summarize_event({
            "type": "request/header", "seq": 12, "time": 1,
            "data": {"header": {"tools": [{"name": "x" * 6000}]}}})
        self.assertEqual(summary["type"], "request/header")
        self.assertEqual(summary["text"], "")
        self.assertTrue(summary["raw_truncated"])
        self.assertLessEqual(len(summary["raw"]), 4000)

    def test_non_dict_event_is_reported_not_crashed(self):
        summary = v3_sdk.summarize_event(None)
        self.assertEqual(summary["type"], "unknown")
        self.assertIn("null", summary["raw"])

    def test_session_state_cursor_advances(self):
        session = v3_sdk.SdkSessionState("s-1", event_buffer=2)
        for index in range(5):
            session.add_event({"type": "step/start", "seq": index, "time": index, "data": {}})
        self.assertEqual(session.cursor, 5)
        view = session.view(since=3, limit=10)
        self.assertEqual([item["cursor"] for item in view["events"]], [4, 5])
        self.assertEqual(len(session.events), 2)     # 环有界


# ---------------------------------------------------------------------------
# 5. 白名单：策略 + 平台目录审计 + 与 profile 插件逐字一致
# ---------------------------------------------------------------------------

class WhitelistTest(unittest.TestCase):

    def test_required_excluded_names_are_all_write_tools(self):
        for name in REQUIRED_EXCLUDED:
            with self.subTest(name=name):
                self.assertTrue(v3_sdk.is_write_tool(name), f"{name} 必须在白名单之外")

    def test_namespaced_mcp_form_is_normalized(self):
        self.assertTrue(v3_sdk.is_write_tool("mcp__quantwb__trade_place"))
        self.assertTrue(v3_sdk.is_write_tool("mcp__quantwb__plan-execute"))
        self.assertTrue(v3_sdk.is_write_tool("mcp__quantwb__switch-mode"))
        self.assertEqual(v3_sdk.normalize_tool_name("mcp__quantwb__plan-execute"),
                         "plan_execute")

    def test_read_only_tools_stay(self):
        for name in ("positions", "snapshot", "plan", "confirmation", "orders_open",
                     "risk", "factors", "audit", "mcp__quantwb__v3_audit",
                     "v3_market", "v3_sdk_status", "read", "grep", "skill"):
            # v3_sdk_status 是**本模块自己**的路由：有意整族拒绝，见下一个用例的说明
            if name == "v3_sdk_status":
                continue
            with self.subTest(name=name):
                self.assertFalse(v3_sdk.is_write_tool(name), f"{name} 不该被误伤")

    def test_sim_trade_family_and_lookalikes(self):
        self.assertTrue(v3_sdk.is_write_tool("sim_trade_order_list"))
        self.assertTrue(v3_sdk.is_write_tool("sim_trade_max_buy_sell"))
        # 注意 v3_sdk_* 是本模块自己的端点（含写端点 prompt），必须排除
        self.assertTrue(v3_sdk.is_write_tool("v3_sdk_prompt"))
        self.assertTrue(v3_sdk.is_write_tool("v3_sdk_sessions"))
        self.assertTrue(v3_sdk.is_write_tool(""))
        self.assertTrue(v3_sdk.is_write_tool(None))

    def test_whitelist_helper_is_consistent_with_predicate(self):
        names = ["positions", "trade_place", "plan", "plan-execute", "confirm-decide",
                 "switch-mode", "v3_credentials", "v3_oms_sync", "v3_audit"]
        allowed = v3_sdk.whitelist(names)
        denied = v3_sdk.denied_from(names)
        self.assertEqual(sorted(allowed + denied), sorted(names))
        self.assertEqual(allowed, ["positions", "plan", "v3_audit"])
        self.assertFalse([name for name in allowed if v3_sdk.is_write_tool(name)])

    def test_reason_names_the_matching_tier(self):
        self.assertIn("前缀 trade_", v3_sdk.write_tool_reason("trade_place"))
        self.assertIn("精确名", v3_sdk.write_tool_reason("plan_execute"))
        self.assertIn("前缀 v3_credentials", v3_sdk.write_tool_reason("v3_credentials_status"))
        self.assertIn("子串", v3_sdk.write_tool_reason("mcp__quantwb__some_credentials_viewer"))
        self.assertIsNone(v3_sdk.write_tool_reason("positions"))

    def test_audit_over_real_platform_catalog_excludes_trading_tools(self):
        app = FastAPI()
        audit = v3_sdk.whitelist_audit(app)
        # 工具清单来自真实导入的 mcp_tools.TOOLS（不是本测试写的常量）
        self.assertGreater(audit["catalog_total"], 50)
        self.assertEqual(audit["violations"], [])
        denied_names = {item["name"] for item in audit["denied"]}
        for name in ("switch_mode", "plan_execute", "trade_place", "trade_modify",
                     "trade_cancel", "trade_max_qty", "push_subscribe", "push_unsubscribe",
                     "research_tasks_claim", "research_tasks_report",
                     "admin_cancel_run", "admin_prune_runs"):
            with self.subTest(name=name):
                self.assertIn(name, denied_names)
        allowed_names = set(audit["allowed"])
        for name in ("plan", "confirmation", "positions", "orders_open", "snapshot"):
            with self.subTest(name=name):
                self.assertIn(name, allowed_names)

    def test_audit_reuses_platform_channel_classification_for_bridge_tools(self):
        app = FastAPI()
        app.state.v3_mcp_bridge = SimpleNamespace(definitions=[
            SimpleNamespace(name="v3_credentials", endpoint="/api/v3/credentials"),
            SimpleNamespace(name="v3_oms_sync", endpoint="/api/v3/oms/sync"),
            SimpleNamespace(name="v3_strategy_run", endpoint="/api/v3/strategy/run"),
            SimpleNamespace(name="v3_audit", endpoint="/api/v3/audit"),
            SimpleNamespace(name="v3_sdk_prompt", endpoint="/api/v3/sdk/prompt"),
        ])
        audit = v3_sdk.whitelist_audit(app)
        denied = {item["name"] for item in audit["denied"]}
        allowed = set(audit["allowed"])
        self.assertLessEqual({"v3_credentials", "v3_oms_sync", "v3_strategy_run",
                              "v3_sdk_prompt"}, denied)
        self.assertIn("v3_audit", allowed)
        self.assertEqual(audit["violations"], [])

    def test_platform_catalog_denies_via_non_readonly_paths(self):
        """平台自己的通道分级常量必须真的出现在审计里（不是本模块自说自话）。"""
        from server import v3_mcp
        app = FastAPI()
        app.state.v3_mcp_bridge = SimpleNamespace(definitions=[
            SimpleNamespace(name="v3_readonly_route", endpoint="/api/v3/whatever")])
        # 造一个「名字层放行、但平台把 endpoint 标成写」的用例，验证第二道分类真的生效
        app.state.v3_mcp_bridge.definitions.append(
            SimpleNamespace(name="v3_innocent_name", endpoint="/api/v3/oms/sync"))
        audit = v3_sdk.whitelist_audit(app)
        denied = {item["name"]: item for item in audit["denied"]}
        self.assertIn("v3_innocent_name", denied)
        self.assertIn(v3_mcp.NON_READONLY_PATHS, [set(v3_mcp.NON_READONLY_PATHS)])
        self.assertIn("平台通道分级", denied["v3_innocent_name"]["reason"])

    def test_policy_matches_profile_plugin_verbatim(self):
        """profile 插件（真边界）与本模块（审计/文档）的拒绝集必须逐字一致。"""
        self.assertTrue(PLUGIN_JS.exists(), f"缺少 {PLUGIN_JS}")
        source = PLUGIN_JS.read_text(encoding="utf-8")

        def literals(const_name):
            match = re.search(rf"const {const_name} = \[(.*?)\]", source, re.S)
            self.assertIsNotNone(match, f"{const_name} 未在插件里找到")
            return re.findall(r"'([^']*)'", match.group(1))

        self.assertEqual(sorted(literals("DENY_NAMES")), sorted(v3_sdk.WRITE_TOOL_NAMES))
        self.assertEqual(literals("DENY_PREFIXES"), list(v3_sdk.WRITE_TOOL_PREFIXES))
        self.assertEqual(literals("DENY_SUBSTRINGS"), list(v3_sdk.WRITE_TOOL_SUBSTRINGS))
        # 插件确实用了两个真机制，且没有把 stdout 写脏
        self.assertIn("ctx.tools.guard(", source)
        self.assertIn("tools.restrict({ deny: hits })", source)
        self.assertIn("console.error(", source)
        self.assertNotIn("console.log(", source)

    def test_policy_covers_every_platform_declared_write_tool(self):
        """平台自己认定的写/交易工具（三处既有常量）必须全部被静态策略拒绝。

        这是「静态名单漂移」的告警线：平台新增一件写工具而这里没跟上时，``policy_gaps``
        立刻非空 → 本用例变红（与 quant-headless 的 tool-whitelist.json 校验同一思路）。
        """
        declared = v3_sdk.platform_write_tool_names()
        self.assertGreaterEqual(len(declared), 20)
        gaps = sorted(name for name in declared if not v3_sdk.is_write_tool(name))
        self.assertEqual(gaps, [], f"静态策略漏掉了平台认定的写工具：{gaps}")
        audit = v3_sdk.whitelist_audit(FastAPI())
        self.assertEqual(audit["policy_gaps"], [])
        self.assertEqual(audit["platform_write_tools"], sorted(declared))

    def test_policy_matches_profile_whitelist_json_verbatim(self):
        """``tool-whitelist.json``（仓库素材的单一事实源）↔ 本模块 ↔ 插件 JS 三方一致。"""
        self.assertTrue(WHITELIST_JSON.exists(), f"缺少 {WHITELIST_JSON}")
        data = json.loads(WHITELIST_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["profile"], "quant-sdk")
        self.assertEqual(data["denyRowId"], "quant-sdk-tool-whitelist")
        self.assertEqual(data["mcpServerName"], "quantwb")
        self.assertEqual(sorted(data["denyNames"]), sorted(v3_sdk.WRITE_TOOL_NAMES))
        self.assertEqual(list(data["denyPrefixes"]), list(v3_sdk.WRITE_TOOL_PREFIXES))
        self.assertEqual(list(data["denySubstrings"]), list(v3_sdk.WRITE_TOOL_SUBSTRINGS))
        self.assertEqual(sorted(data["readOnlyExcluded"]),
                         sorted(v3_sdk.READ_ONLY_EXCLUDED_ENDPOINTS))
        # 平台常量推导出的写工具名必须逐字落在文件里
        self.assertEqual(sorted(data["denyTools"]), sorted(v3_sdk.platform_write_tool_names()))
        self.assertFalse([name for name in data["denyTools"]
                          if not v3_sdk.is_write_tool(name)])
        # 插件 JS 的档位也要与 JSON 对上（防止只改了 JSON 忘了插件）
        source = PLUGIN_JS.read_text(encoding="utf-8")

        def literals(const_name):
            match = re.search(rf"const {const_name} = \[(.*?)\]", source, re.S)
            return re.findall(r"'([^']*)'", match.group(1))

        self.assertEqual(sorted(literals("DENY_NAMES")), sorted(data["denyNames"]))
        self.assertEqual(literals("DENY_PREFIXES"), list(data["denyPrefixes"]))
        self.assertEqual(literals("DENY_SUBSTRINGS"), list(data["denySubstrings"]))
        # row 的 disabledRows 与 patch 里的关停行一一对应
        patch = PATCH_YML.read_text(encoding="utf-8")
        for row in data["disabledRows"]:
            with self.subTest(row=row):
                self.assertIn(f"- id: {row}\n  disabled: true", patch)

    def test_profile_material_is_complete_and_consistent(self):
        self.assertTrue(PROFILE_JSON.exists())
        self.assertTrue(PATCH_YML.exists())
        profile = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(profile["dsh"]["profile"]["bundles"],
                         ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-sdk-app"])
        patch = PATCH_YML.read_text(encoding="utf-8")
        # §6.3 的两块必须都在：JSON-RPC 服务行 + 平台 MCP 客户端行
        self.assertIn("sdk-jsonrpc-server", patch)
        self.assertIn("maxTokensAsSuccess: false", patch)
        self.assertIn("quant-platform-mcp", patch)
        # 只读面（2026-09-21）：决策 profile 的 MCP 入口必须是 /mcp/ro（服务侧按注册表
        # annotations 拒绝写类内层转发），且不得再出现指向裸 /mcp 的 url 行。
        self.assertIn("url: http://127.0.0.1:8397/mcp/ro", patch)
        self.assertNotIn("url: http://127.0.0.1:8397/mcp\n", patch)
        self.assertIn("quant-tool-whitelist", patch)
        # 白名单做法（照抄 quant-headless）：本地写面按行关停
        for row in ("tool-bash", "tool-pwsh", "tool-subagent", "tool-workflow", "tool-ralph",
                    "tool-goal", "tool-jobs", "tool-web", "web-fetch-http"):
            with self.subTest(row=row):
                self.assertIn(f"- id: {row}\n  disabled: true", patch)
        # 本模块默认 profile 名必须与素材目录名一致
        self.assertEqual(v3_sdk.DEFAULT_PROFILE, "quant-sdk")
        self.assertIn("quant-sdk", patch)


# ---------------------------------------------------------------------------
# 6. 审计与口令
# ---------------------------------------------------------------------------

class PassphraseAndAuditTest(SdkTestCase):

    def test_check_passphrase(self):
        self.assertTrue(v3_sdk.check_passphrase("确认下发"))
        self.assertTrue(v3_sdk.check_passphrase("  确认下发  "))
        for bad in (None, "", "确认执行", "确认下发!", "queued", "确认下"):
            with self.subTest(bad=bad):
                self.assertFalse(v3_sdk.check_passphrase(bad))

    def test_audit_append_and_read(self):
        result = v3_sdk.append_audit(self.home, {"action": "prompt", "accepted": True})
        self.assertTrue(result["ok"])
        self.assertEqual(os.stat(v3_sdk.audit_path(self.home)).st_mode & 0o777, 0o600)
        records = v3_sdk.read_audit(self.home)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["accepted"])
        self.assertIn("at", records[0])

    def test_read_audit_on_missing_file_is_empty_not_fabricated(self):
        self.assertEqual(v3_sdk.read_audit(os.path.join(self.home, "nope")), [])

    def test_audit_survives_corrupt_line(self):
        with open(v3_sdk.audit_path(self.home), "w", encoding="utf-8") as handle:
            handle.write("{ not json }\n")
        v3_sdk.append_audit(self.home, {"action": "prompt", "accepted": False})
        records = v3_sdk.read_audit(self.home)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["accepted"])


# ---------------------------------------------------------------------------
# 7. HTTP：/api/v3/sdk/{status,sessions,prompt}
# ---------------------------------------------------------------------------

class MetricsViewTest(SdkTestCase):
    """``metrics_view()``：observability 的 §8.3 监控规则唯一读取口（**只读、无副作用**）。"""

    def test_without_any_runtime_it_is_null_not_zero(self):
        """没有运行时对象 → ``activeSessions=None``（「没有事实来源」≠「0 个会话」）。"""
        view = v3_sdk.metrics_view()
        self.assertIsNone(view["activeSessions"])
        self.assertEqual(view["runtimes"], 0)
        self.assertEqual(view["runningProcesses"], 0)
        self.assertEqual(view["sessions"], {})
        self.assertIn("只读", view["note"])

    def test_it_reads_existing_sessions_without_creating_a_runtime(self):
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.session("s-1").note_status("idle")
        runtime.session("s-2").note_status("running")
        # 刻意**不**放进 _REGISTRY：metrics_view 只读注册表，绝不自己造 runtime
        v3_sdk.reset_registry()
        self.assertIsNone(v3_sdk.metrics_view()["activeSessions"])
        v3_sdk._REGISTRY[("abc", "quant-sdk")] = runtime
        view = v3_sdk.metrics_view()
        self.assertEqual(view["activeSessions"], 2)
        self.assertEqual(view["runtimes"], 1)
        self.assertEqual(view["sessions"], {"s-1": "idle", "s-2": "running"})
        self.assertNotIn((str(self.home), "quant-sdk"), v3_sdk._REGISTRY,
                         "metrics_view 不得创建 runtime（不许出现新 key）")
        self.assertIsNone(fake.argv, "只读视图绝不 spawn 子进程")

    def test_a_runtime_with_zero_sessions_is_a_real_zero(self):
        """有运行时但会话为空 → 真读数 0（与「没有运行时」的 null 必须可区分）。"""
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        v3_sdk.reset_registry()
        v3_sdk._REGISTRY[("/home/z", "quant-sdk")] = runtime
        view = v3_sdk.metrics_view()
        self.assertEqual(view["activeSessions"], 0)
        self.assertEqual(view["runtimes"], 1)

    def test_observability_reports_source_missing_without_a_runtime(self):
        """没有运行时 → observability 走「没有事实来源」分支（导出 *_source_missing）。"""
        from server import observability
        active, source = observability.sdk_active_sessions(self.home)
        self.assertIsNone(active)
        self.assertIn("没有 SDK 会话事实来源", source)

    def test_home_filter_and_duplicate_session_ids(self):
        first, second = FakeDsh(), FakeDsh()
        one = self.make_runtime(first)
        two = self.make_runtime(second)
        one.session("same").note_status("idle")
        two.session("same").note_status("running")
        two.session("other").note_status("idle")
        v3_sdk.reset_registry()
        v3_sdk._REGISTRY[("/home/a", "quant-sdk")] = one
        v3_sdk._REGISTRY[("/home/b", "quant-sdk")] = two
        self.assertEqual(v3_sdk.metrics_view("/home/a")["activeSessions"], 1)
        self.assertEqual(v3_sdk.metrics_view("/home/b")["activeSessions"], 2)
        self.assertEqual(v3_sdk.metrics_view()["activeSessions"], 2,
                         "同一 session_id 跨 runtime 去重")

    def test_observability_consumes_it_without_starting_anything(self):
        from server import observability
        fake = FakeDsh()
        runtime = self.make_runtime(fake)
        runtime.session("s-9").note_status("idle")
        v3_sdk.reset_registry()
        v3_sdk._REGISTRY[(str(self.home), "quant-sdk")] = runtime
        active, source = observability.sdk_active_sessions(self.home)
        self.assertEqual(active, 1.0)
        self.assertEqual(source, "v3_sdk.metrics_view()")
        self.assertIsNone(fake.argv)


class HttpEndpointsTest(SdkTestCase):

    def test_status_is_read_only_and_does_not_spawn(self):
        fake = FakeDsh()
        app = self.make_app(fake)
        with TestClient(app) as client:
            response = client.get("/api/v3/sdk/status")
            self.assertEqual(response.status_code, 200)
            body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "stopped")     # GET 不起进程
        self.assertIsNone(fake.argv)                   # 真的没有 spawn
        self.assertFalse(body["protocol_match"])
        self.assertEqual(body["spec_server_name"], "deepseek-harness-sdk-runtime")
        self.assertEqual(body["profile"], "quant-sdk")
        self.assertEqual(body["whitelist"]["violations"], [])
        self.assertIn("audit_path", body)
        self.assertIn("stderr_tail", body)

    def test_prompt_requires_passphrase_and_records_refusal(self):
        fake = FakeDsh()
        app = self.make_app(fake)
        with TestClient(app) as client:
            for body in ({"prompt": "分析当前持仓风险", "session_id": "q-1"},
                         {"prompt": "分析当前持仓风险", "session_id": "q-1",
                          "confirmation": "确认执行"},
                         {"prompt": "分析当前持仓风险", "session_id": "q-1",
                          "confirmation": ""}):
                with self.subTest(confirmation=body.get("confirmation")):
                    response = client.post("/api/v3/sdk/prompt", json=body)
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["error"]["code"],
                                     "v3-sdk/passphrase-required")
                    self.assertIn("确认下发", payload["error"]["message"])
            # 口令缺失 → 绝不启进程、绝不发提示词
            self.assertIsNone(fake.argv)
            self.assertEqual(fake.received("session/prompt"), [])
        records = v3_sdk.read_audit(self.home)
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertFalse(record["accepted"])
            self.assertFalse(record["passphrase_ok"])
            self.assertEqual(record["code"], "v3-sdk/passphrase-required")
        # 审计里存的是哈希与长度，不是提示词原文
        self.assertEqual(records[0]["prompt_sha256"],
                         __import__("hashlib").sha256("分析当前持仓风险".encode()).hexdigest())

    def test_prompt_with_passphrase_starts_handshakes_and_audits(self):
        fake = FakeDsh()
        app = self.make_app(fake)
        with TestClient(app) as client:
            response = client.post("/api/v3/sdk/prompt", json={
                "prompt": "只回复 OK，不要调用任何工具", "session_id": "q-2",
                "confirmation": v3_sdk.PROMPT_PASSPHRASE})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertTrue(body["ok"], body)
            self.assertEqual(body["session_id"], "q-2")
            self.assertTrue(body["handshake"]["protocol_match"])
            self.assertIn(body["receipt"]["state"], ("sent", "done"))
            self.assertEqual(body["receipt"]["message_id"], "msg-1")
            steps = [step["step"] for step in body["steps"]]
            self.assertEqual(steps, ["spawn", "initialize", "enqueue"])

            sessions = client.get("/api/v3/sdk/sessions").json()
            self.assertEqual(sessions["count"], 1)
            session = sessions["sessions"][0]
            self.assertEqual(session["session_id"], "q-2")
            self.assertEqual([event["type"] for event in session["events"]],
                             ["user/message", "assistant/message", "turn/end"])
            self.assertEqual(session["events"][1]["text"], "OK")
            self.assertEqual(session["prompts"][-1]["state"], "done")
            # 事件回放：since 之后的游标
            replay = client.get("/api/v3/sdk/sessions?session=q-2&since=1&limit=1").json()
            self.assertEqual(replay["count"], 1)
            self.assertEqual([item["cursor"] for item in replay["sessions"][0]["events"]], [3])
            # 全量视图里不含提示词原文
            self.assertNotIn("_text", json.dumps(sessions, ensure_ascii=False))

            status = client.get("/api/v3/sdk/status").json()
            self.assertTrue(status["protocol_match"])
            self.assertEqual(status["counters"]["session_events"], 3)

        records = v3_sdk.read_audit(self.home)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["accepted"])
        self.assertTrue(records[0]["passphrase_ok"])
        self.assertEqual(records[0]["message_id"], "msg-1")
        self.assertEqual(records[0]["profile"], "quant-sdk")
        self.assertEqual(records[0]["handshake_state"], "ok")
        self.assertEqual(records[0]["prompt_preview"], "只回复 OK，不要调用任何工具")

    def test_start_action_handshakes_without_prompting(self):
        fake = FakeDsh()
        app = self.make_app(fake)
        with TestClient(app) as client:
            body = client.post("/api/v3/sdk/prompt", json={
                "action": "start", "session_id": "q-3",
                "confirmation": v3_sdk.PROMPT_PASSPHRASE}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["action"], "start")
        self.assertTrue(body["handshake"]["protocol_match"])
        self.assertEqual(fake.received("session/prompt"), [])

    def test_protocol_mismatch_refuses_prompt_over_http(self):
        fake = FakeDsh(server_name="not-the-spec")
        app = self.make_app(fake)
        with TestClient(app) as client:
            body = client.post("/api/v3/sdk/prompt", json={
                "prompt": "hi", "session_id": "q-4",
                "confirmation": v3_sdk.PROMPT_PASSPHRASE}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "v3-sdk/protocol-mismatch")
        self.assertEqual(body["error"]["observed"], "not-the-spec")
        self.assertEqual(body["error"]["expected"], "deepseek-harness-sdk-runtime")
        self.assertEqual(fake.received("session/prompt"), [])

    def test_unknown_action_is_refused(self):
        fake = FakeDsh()
        app = self.make_app(fake)
        with TestClient(app) as client:
            body = client.post("/api/v3/sdk/prompt", json={
                "action": "trade", "confirmation": v3_sdk.PROMPT_PASSPHRASE}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "v3-sdk/unknown-action")

    def test_bad_json_body_is_refused(self):
        fake = FakeDsh()
        app = self.make_app(fake)
        with TestClient(app) as client:
            response = client.post("/api/v3/sdk/prompt", content=b"not json",
                                   headers={"content-type": "application/json"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["error"]["code"], "v3-sdk/bad-json")
            response = client.post("/api/v3/sdk/prompt", json=["list"],
                                   headers={"content-type": "application/json"})
            self.assertEqual(response.json()["error"]["code"], "v3-sdk/bad-json")

    def test_status_reports_process_death_verbatim(self):
        fake = FakeDsh(stderr_on_exit="Error: adapter deepseek-official unavailable")
        app = self.make_app(fake)
        with TestClient(app) as client:
            client.post("/api/v3/sdk/prompt", json={
                "action": "start", "confirmation": v3_sdk.PROMPT_PASSPHRASE})
            fake.finish(7)
            deadline = time.time() + 5
            body = {}
            while time.time() < deadline:
                body = client.get("/api/v3/sdk/status").json()
                if body["state"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["exit_code"], 7)
        self.assertIn("adapter deepseek-official unavailable", body["stderr_tail"])
        self.assertIn("adapter deepseek-official unavailable", body["last_error"]["stderr"])

    def test_sessions_endpoint_on_stopped_runtime_reports_empty_not_fake(self):
        fake = FakeDsh()
        app = self.make_app(fake)
        with TestClient(app) as client:
            body = client.get("/api/v3/sdk/sessions").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "stopped")
        self.assertEqual(body["sessions"], [])
        self.assertEqual(body["count"], 0)

    def test_register_accepts_app_py_call_shape(self):
        """app.py 用位置参数 `_register(app, v3_run, home)`，签名必须接得住。"""
        import inspect
        parameters = list(inspect.signature(v3_sdk.register).parameters)
        self.assertEqual(parameters[:3], ["app", "v3_run", "home"])
        app = FastAPI()
        self.assertIs(v3_sdk.register(app, lambda *a, **k: {"ok": True}, self.home), app)


# ---------------------------------------------------------------------------
# 8. 真机（默认 skip，理由写明）
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.environ.get("QUANT_SDK_REAL") == "1",
                     "真机测试需要独立 DSH_HOME + 已配置的模型路由："
                     "设 QUANT_SDK_REAL=1 与 QUANT_SDK_REAL_HOME=<隔离 home> 才跑；"
                     "CI/离线环境不跑（不得伪造握手成功）。")
class RealMachineTest(unittest.TestCase):
    """真起 ``dsh --profile quant-sdk``（隔离 DSH_HOME），只做握手 + 一条最小提示。

    真机结果记录在 ``platform/install/quant-sdk/README.md``（含耗时、exit code、stderr 原文）。
    这里只保留可复现入口；**默认 skip**，因为它依赖机器上的官方 SDK 包、凭据与模型路由。
    """

    ISO = os.environ.get("QUANT_SDK_REAL_HOME", "")

    def test_real_handshake_and_minimal_prompt(self):
        if not self.ISO or not os.path.isdir(self.ISO):
            self.skipTest(f"隔离 DSH_HOME 不存在：{self.ISO!r}")
        runtime = v3_sdk.SdkRuntime(profile="quant-sdk", dsh_home=self.ISO, cwd=str(REPO),
                                    handshake_timeout=180.0, prompt_timeout=420.0)
        self.addCleanup(lambda: runtime.running and runtime.stop())
        runtime.start()
        handshake = runtime.handshake()
        self.assertTrue(handshake["protocol_match"],
                        f"协议标识不符：{handshake.get('observed')!r}")
        # 真机教训：同一个 session_id 在该 DSH_HOME 里**跨进程不可复用**
        # （会回 -32603 session "..." already exists），所以每次跑用唯一 id。
        session_id = f"real-probe-{int(time.time())}"
        runtime.enqueue_prompt(session_id, "只回复 OK，不要调用任何工具",
                               wait_receipt=30.0)
        # 一次下发完成的边界 = 观测到新的 turn/end（协议没有 per-prompt 结果）
        deadline = time.time() + 420
        view = runtime.sessions_view(session_id)[0]
        while time.time() < deadline and view["prompts"][-1]["state"] not in (
                "done", "failed", "timeout"):
            time.sleep(1.0)
            view = runtime.sessions_view(session_id)[0]
        self.assertEqual(view["prompts"][-1]["state"], "done",
                         f"未观测到 turn/end：{view['prompts'][-1]}")
        self.assertEqual(view["status"], "idle")
        self.assertTrue([event for event in view["events"]
                         if event["type"] == "turn/end"], "事件流里没有 turn/end")


if __name__ == "__main__":
    unittest.main()
