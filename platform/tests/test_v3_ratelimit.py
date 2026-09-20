"""``server/v3_ratelimit.py`` 契约测试（富途限流治理：限速 / 单飞 / 退避 / 冷却 / 统一错误码）。

**全部离线**：注入假时钟 + 假 sleep（同步面沿用它，异步面因不是协程也直接可用）、
假 ``rand``（去掉抖动的不确定性），并发用真线程 + ``Event`` 同步，因此「N 次并发只发
1 次真实请求」「并发上限 2」这类断言都是确定性的（轮询 ``stats()`` 到稳定态再放行）。

覆盖面（逐条对任务书）：
  * 限速等待（令牌桶）+ ``throttle_wait_ms`` 累计；
  * 并发上限（``max_concurrency``）与排队（``queued``）；
  * 单飞合并（同步 5 并发 → 1 次真实调用；异步 5 并发 → 1 次真实调用）；
  * 限流触发 → 指数退避 → 下一次成功；
  * 重试耗尽 → 统一信封（``futu/rate-limited``，含 upstream/retries/retry_after_ms）；
  * 冷却：期内**不发请求**、``retry_after_ms`` 随时钟递减、到期自动恢复；
  * 非限流错误原样透传（异常照抛、业务错误信封一字不改）；
  * ``QUANT_FUTU_RATELIMIT=0`` 直通（不计数、不等待）；
  * ``stats()`` 字段齐全 + ``metrics_view()`` 的 camelCase 视图；
  * 识别矩阵（-12006 / -12009 / errcode=439 / 裸 403 不算）与接入层闸门（写端点永不进集合）。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ratelimit -v``
"""
import asyncio
import os
import sys
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_ratelimit as rl  # noqa: E402

#: 上游限流的两种真实写法（实测原文）。
UPSTREAM_403 = "非预期响应（HTTP 403）：b'{\"code\":-12006,\"message\":\"请求过于频繁\"}'"
UPSTREAM_439 = "positions 取数失败：富途业务错误（errcode=439）：请求频率超限"


def envelope_12006():
    return {"ok": False, "error": {"code": "trading/futu-error",
                                   "message": "富途业务错误（errcode=-12006）",
                                   "details": {"errcode": -12006}}}


def envelope_439():
    return {"ok": False, "error": {"code": "v3/tool-failed", "message": UPSTREAM_439}}


class FakeClock:
    """假时钟 + 假 sleep：``sleep(s)`` 记录等待并推进时间（同步面与异步面共用）。"""

    def __init__(self, start=1000.0):
        self.now = start
        self.waits = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def wait_until(predicate, timeout=5.0, interval=0.002):
    """轮询到谓词为真（并发用例的确定性同步点；超时即失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def make_limiter(clock=None, **overrides):
    """假时钟 + 无抖动（``rand=1.0``，退避取全量）+ 高速率（除显式覆盖）的 limiter。"""
    clock = clock or FakeClock()
    params = {"rate_per_sec": 1000.0, "burst": 100, "sleep": clock.sleep,
              "clock": clock.clock, "rand": lambda: 1.0}
    params.update(overrides)
    return rl.FutuLimiter(**params), clock


# ---------------------------------------------------------------------------
# 识别 / 信封（纯函数）
# ---------------------------------------------------------------------------
class RecognitionTests(unittest.TestCase):
    """``is_rate_limit_error`` / ``upstream_code_of``：错误码映射表逐行钉住。"""

    def test_recognises_the_real_upstream_writings(self):
        for error in (UPSTREAM_403, UPSTREAM_439, envelope_12006(), envelope_439(),
                      {"ok": False, "error": {"code": "x", "message": "请求过于频繁"}},
                      "-12009", "rate limit exceeded", Exception("超出频率限制，请稍后")):
            self.assertTrue(rl.is_rate_limit_error(error), error)

    def test_plain_403_is_not_rate_limiting(self):
        self.assertFalse(rl.is_rate_limit_error("HTTP 403 Forbidden（无权访问该接口）"),
                         "裸 403 不算限流（403 只在同时含 12006 时由 12006 分支命中）")

    def test_successful_envelope_is_never_a_rate_limit_error(self):
        """实测教训：deals_today 成功（ok=true）但 value 里带某账户分组的「请求过于频繁」。"""
        payload = {"ok": True, "value": {"source": "futu/sim_trade_order_list(derived)",
                                         "groups": [{"acc_id": "6683018", "market": "10",
                                                     "rows": [],
                                                     "reason": "请求过于频繁"}]}}
        self.assertFalse(rl.is_rate_limit_error(payload))
        self.assertFalse(rl.is_rate_limit_error({"ok": True, "value": "HTTP 403 {-12006}"}))

    def test_unrelated_and_empty_errors_are_not_rate_limiting(self):
        for error in (None, "", "market/no-universe：没有可用的市场池",
                      {"ok": False, "error": {"code": "trading/futu-error",
                                              "message": "errcode=-7 invalid symbol"}},
                      4390, "订单号 120060"):
            self.assertFalse(rl.is_rate_limit_error(error), error)

    def test_upstream_code_mapping(self):
        table = [
            (UPSTREAM_403, "-12006"),
            ("HTTP 403：{\"code\":-12006}", "-12006"),
            ("-12009 请求过于频繁", "-12009"),
            ("富途业务错误（errcode=439）", "439"),
            ("[439] too many requests", "439"),
            ("请求过于频繁", "text"),
            # 本模块自己的信封：带得出原始上游码就报原始码；只有「冷却中」这种无码信封
            # 才记成 futu/rate-limited（内层已限流这一事实本身）。
            ({"ok": False, "error": {"code": "futu/rate-limited",
                                     "message": "富途接口限流（上游 -12006），已退避重试 2 次仍失败"}},
             "-12006"),
            ({"ok": False, "error": {"code": "futu/rate-limited",
                                     "message": "富途接口限流冷却中（上游 text），请约 4.5s 后重试"}},
             "futu/rate-limited"),
            ("something else", "unknown"),
            (None, "unknown"),
        ]
        for error, expected in table:
            self.assertEqual(rl.upstream_code_of(error), expected, error)

    def test_rate_limited_envelope_matches_the_contract(self):
        envelope = rl.rate_limited_envelope(UPSTREAM_403, 5200, retries=2, cooldown=True)
        self.assertFalse(envelope["ok"])
        error = envelope["error"]
        self.assertEqual(error["code"], "futu/rate-limited")
        self.assertEqual(error["message"],
                         "富途接口限流（上游 -12006），已退避重试 2 次仍失败，请稍后重试")
        self.assertEqual(error["retry_after_ms"], 5200)
        self.assertNotIn("retry_after_ms", envelope, "契约里 retry_after_ms 在 error 内（样例同形）")
        self.assertEqual(error["detail"],
                         {"upstream": "-12006", "retry_after_ms": 5200, "retries": 2,
                          "cooldown": True, "reason": UPSTREAM_403})

    def test_envelope_without_retries_is_a_hint_not_a_rewrite(self):
        envelope = rl.rate_limited_envelope(UPSTREAM_439, 0)
        self.assertEqual(envelope["error"]["message"], "富途接口限流（上游 439），请稍后重试")
        self.assertEqual(envelope["error"]["detail"]["retries"], 0)
        self.assertFalse(envelope["error"]["detail"]["cooldown"])
        self.assertIn("errcode=439", envelope["error"]["detail"]["reason"])

    def test_reason_falls_back_to_the_raw_text_when_there_is_no_error_message(self):
        """触发文本藏在工具 value 的嵌套字段里（没有 error.message）时，reason 仍要给出原文。"""
        value = {"ok": True, "value": {"channels": [{"name": "futu", "error": "请求过于频繁"}]}}
        envelope = rl.rate_limited_envelope(value, 0, retries=2)
        self.assertEqual(envelope["error"]["detail"]["upstream"], "text")
        self.assertIn("请求过于频繁", envelope["error"]["detail"]["reason"])

    def test_cooldown_envelope_says_how_long_to_wait(self):
        envelope = rl.rate_limited_envelope(None, 3000, cooldown=True, upstream="-12006")
        self.assertIn("冷却中", envelope["error"]["message"])
        self.assertIn("3.0s", envelope["error"]["message"])
        self.assertEqual(envelope["error"]["detail"]["upstream"], "-12006")

    def test_stable_key_is_order_insensitive_and_payload_sensitive(self):
        self.assertEqual(rl.stable_key("series", {"ticker": "SH.600519", "limit": 20}),
                         rl.stable_key("series", {"limit": 20, "ticker": "SH.600519"}))
        self.assertNotEqual(rl.stable_key("series", {"limit": 20}),
                            rl.stable_key("series", {"limit": 21}))
        self.assertNotEqual(rl.stable_key("series", {}), rl.stable_key("positions", {}))

    def test_only_read_only_futu_tools_are_in_the_guard_set(self):
        for name in ("series", "positions", "deals_today", "f10_detail", "orders_history",
                     "quote_history_kline_v2", "watchlist_list"):
            self.assertTrue(rl.is_futu_tool(name), name)
        for name in ("switch_mode", "plan_execute", "trade_place", "trade_modify",
                     "trade_cancel", "modify_user_security", "confirm-decide",
                     "plan", "equity", "audit", "schedule", "rules", "snapshot-x"):
            self.assertFalse(rl.is_futu_tool(name),
                             f"{name} 不该进只读限流集合（写/交易或本地台账）")


# ---------------------------------------------------------------------------
# 限速 / 并发上限
# ---------------------------------------------------------------------------
class ThrottleTests(unittest.TestCase):
    def test_token_bucket_waits_and_counts(self):
        limiter, clock = make_limiter(rate_per_sec=2.0, burst=2)
        calls = []
        for index in range(4):
            value = limiter.run_sync(f"k{index}",
                                     lambda index=index: calls.append(index) or {"ok": True})
            self.assertTrue(value["ok"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(clock.waits, [0.5, 0.5], "前 2 个吃突发额度，后 2 个各等 1/2 秒")
        stats = limiter.stats()
        self.assertEqual(stats["calls"], 4)
        self.assertEqual(stats["throttle_wait_ms"], 1000.0)

    def test_burst_within_capacity_does_not_wait(self):
        limiter, clock = make_limiter(rate_per_sec=1.0, burst=3)
        for index in range(3):
            limiter.run_sync(f"k{index}", lambda: {"ok": True})
        self.assertEqual(clock.waits, [])
        self.assertEqual(limiter.stats()["throttle_wait_ms"], 0)

    def test_max_concurrency_caps_real_requests(self):
        limiter, _clock = make_limiter(max_concurrency=2)
        release = threading.Event()
        guard = threading.Lock()
        state = {"active": 0, "peak": 0, "calls": 0}

        def call():
            with guard:
                state["active"] += 1
                state["calls"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                release.wait(5)
            finally:
                with guard:
                    state["active"] -= 1
            return {"ok": True}

        threads = [threading.Thread(target=limiter.run_sync, args=(f"slot-{i}", call))
                   for i in range(4)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(wait_until(lambda: limiter.stats()["in_flight"] == 2),
                            "应有两个真实请求在飞")
            self.assertTrue(wait_until(lambda: limiter.stats()["queued"] == 2),
                            "另两个应在并发槽队列里等待")
            self.assertEqual(state["calls"], 2, "排队者不该发请求")
        finally:
            release.set()
            for thread in threads:
                thread.join(5)
        self.assertEqual(state["peak"], 2)
        self.assertEqual(state["calls"], 4)
        stats = limiter.stats()
        self.assertEqual((stats["in_flight"], stats["queued"]), (0, 0))


# ---------------------------------------------------------------------------
# 单飞
# ---------------------------------------------------------------------------
class SingleFlightTests(unittest.TestCase):
    def test_sync_single_flight_shares_one_real_call(self):
        limiter, _clock = make_limiter()
        entered = threading.Event()
        release = threading.Event()
        state = {"calls": 0}
        results = []

        def call():
            state["calls"] += 1
            entered.set()
            release.wait(5)
            return {"ok": True, "value": "shared"}

        def run():
            results.append(limiter.run_sync("series|same", call))

        leader = threading.Thread(target=run)
        leader.start()
        self.assertTrue(entered.wait(5), "首个调用应进入真实请求")
        followers = [threading.Thread(target=run) for _ in range(4)]
        for thread in followers:
            thread.start()
        try:
            self.assertTrue(wait_until(lambda: limiter.stats()["coalesced"] == 4),
                            "后 4 个调用应合并到同一次真实请求")
            self.assertEqual(state["calls"], 1)
            self.assertTrue(wait_until(lambda: limiter.stats()["queued"] == 4),
                            "复用者计入 queued")
        finally:
            release.set()
            leader.join(5)
            for thread in followers:
                thread.join(5)
        self.assertEqual(state["calls"], 1, "并发 N 次只发 1 次真实调用")
        self.assertEqual(len(results), 5)
        self.assertEqual(results, [{"ok": True, "value": "shared"}] * 5,
                         "5 个调用拿到同一份结果（谁先返回不影响内容）")
        stats = limiter.stats()
        self.assertEqual((stats["calls"], stats["coalesced"]), (5, 4))

    def test_nested_calls_inside_one_guarded_call_do_not_deadlock(self):
        """外层受管调用内部又走一次同 limiter（并发槽只有 1 个）：内层必须透传，不能自锁。"""
        limiter, _clock = make_limiter(max_concurrency=1)
        state = {"outer": 0, "inner": 0}

        def inner():
            state["inner"] += 1
            return {"ok": True, "value": "inner"}

        def outer():
            state["outer"] += 1
            nested = limiter.run_sync("series|nested", inner)
            return {"ok": True, "value": nested}

        result = limiter.run_sync("series|outer", outer)
        self.assertEqual(result, {"ok": True, "value": {"ok": True, "value": "inner"}})
        self.assertEqual((state["outer"], state["inner"]), (1, 1))
        self.assertEqual(limiter.stats()["calls"], 1, "内层透传不重复计数")
        self.assertEqual(limiter.stats()["in_flight"], 0)

    def test_different_keys_are_not_coalesced(self):
        limiter, _clock = make_limiter()
        state = {"calls": 0}

        def call():
            state["calls"] += 1
            return {"ok": True}

        limiter.run_sync("a|{}", call)
        limiter.run_sync("b|{}", call)
        self.assertEqual(state["calls"], 2)
        self.assertEqual(limiter.stats()["coalesced"], 0)

    def test_async_single_flight_shares_one_real_call(self):
        limiter, _clock = make_limiter()
        gate = asyncio.Event()
        state = {"calls": 0}

        async def call():
            state["calls"] += 1
            await gate.wait()
            return {"ok": True, "value": "async-shared"}

        async def main():
            tasks = [asyncio.create_task(limiter.run("series|same", call)) for _ in range(5)]
            for _ in range(200):
                if limiter.stats()["coalesced"] == 4:
                    break
                await asyncio.sleep(0)
            self.assertEqual(state["calls"], 1)
            self.assertEqual(limiter.stats()["coalesced"], 4)
            gate.set()
            return await asyncio.gather(*tasks)

        results = asyncio.run(main())
        self.assertEqual(state["calls"], 1)
        self.assertEqual(results, [{"ok": True, "value": "async-shared"}] * 5)
        self.assertEqual(limiter.stats()["calls"], 5)

    def test_leader_failure_is_shared_and_not_retried_by_everyone(self):
        limiter, _clock = make_limiter()
        release = threading.Event()
        state = {"calls": 0}
        results = []

        def call():
            state["calls"] += 1
            release.wait(5)
            return {"ok": False, "error": {"code": "market/no-universe",
                                           "message": "没有可用的市场池"}}

        def run():
            results.append(limiter.run_sync("positions|{}", call))

        leader = threading.Thread(target=run)
        leader.start()
        release.wait(0.05)
        followers = [threading.Thread(target=run) for _ in range(2)]
        for thread in followers:
            thread.start()
        self.assertTrue(wait_until(lambda: limiter.stats()["coalesced"] == 2))
        release.set()
        leader.join(5)
        for thread in followers:
            thread.join(5)
        self.assertEqual(state["calls"], 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["error"]["code"], "market/no-universe")


# ---------------------------------------------------------------------------
# 退避 / 重试 / 冷却
# ---------------------------------------------------------------------------
class BackoffTests(unittest.TestCase):
    def test_rate_limit_then_success_backs_off_once(self):
        limiter, clock = make_limiter(base_backoff_ms=400, max_backoff_ms=8000)
        queue = [envelope_12006(), {"ok": True, "value": "recovered"}]
        value = limiter.run_sync("series|{}", lambda: queue.pop(0))
        self.assertEqual(value, {"ok": True, "value": "recovered"})
        self.assertEqual(clock.waits, [0.4], "第一次退避 = base 400ms")
        stats = limiter.stats()
        self.assertEqual(stats["retries"], 1)
        self.assertEqual(stats["rate_limited"], 1)

    def test_backoff_grows_exponentially_and_is_capped(self):
        limiter, clock = make_limiter(base_backoff_ms=400, max_backoff_ms=1000, retries=3)
        queue = [envelope_12006(), envelope_12006(), envelope_12006(), {"ok": True}]
        limiter.run_sync("series|{}", lambda: queue.pop(0))
        self.assertEqual(clock.waits, [0.4, 0.8, 1.0], "指数增长 + max 封顶")
        self.assertEqual(limiter.stats()["retries"], 3)

    def test_retries_exhausted_returns_the_unified_envelope(self):
        limiter, _clock = make_limiter(cooldown_after=10)
        state = {"calls": 0}

        def call():
            state["calls"] += 1
            return envelope_12006()

        result = limiter.run_sync("series|{}", call)
        self.assertEqual(state["calls"], 3, "1 次原始 + 2 次重试")
        self.assertEqual(result["error"]["code"], "futu/rate-limited")
        self.assertEqual(result["error"]["detail"]["upstream"], "-12006")
        self.assertEqual(result["error"]["detail"]["retries"], 2)
        self.assertFalse(result["error"]["detail"]["cooldown"])
        self.assertEqual(result["error"]["retry_after_ms"], 1600,
                         "非冷却时的建议等待 = 下一次退避（400×2²）")
        self.assertNotIn("futu-error", str(result))
        stats = limiter.stats()
        self.assertEqual((stats["calls"], stats["retries"], stats["rate_limited"]), (1, 2, 3))

    def test_exhausted_retries_inside_cooldown_after_matches_the_doc_sample(self):
        # 默认 cooldown_after=3：一次逻辑调用的 3 次限流错误（原始 + 2 重试）正好触发冷却，
        # 信封与任务书样例同形（cooldown=true、retries=2、retry_after_ms=剩余冷却 5000ms）。
        limiter, _clock = make_limiter(cooldown_ms=5000)
        result = limiter.run_sync("series|{}", envelope_12006)
        detail = result["error"]["detail"]
        self.assertEqual((detail["retries"], detail["cooldown"],
                          result["error"]["retry_after_ms"]), (2, True, 5000))
        self.assertGreater(limiter.blocking_cooldown_ms(), 0)

    def test_exception_rate_limit_is_converted_to_envelope(self):
        limiter, clock = make_limiter(retries=1, cooldown_after=10)

        def call():
            raise RuntimeError(UPSTREAM_439)

        result = limiter.run_sync("deals_today|{}", call)
        self.assertEqual(result["error"]["code"], "futu/rate-limited")
        self.assertEqual(result["error"]["detail"]["upstream"], "439")
        self.assertEqual(result["error"]["detail"]["retries"], 1)
        self.assertEqual(clock.waits, [0.4])


class CooldownTests(unittest.TestCase):
    def setup_limiter(self, **overrides):
        params = {"retries": 0, "cooldown_after": 3, "cooldown_ms": 5000}
        params.update(overrides)
        limiter, clock = make_limiter(**params)
        state = {"calls": 0}

        def failing():
            state["calls"] += 1
            return envelope_439()

        return limiter, clock, state, failing

    def test_cooldown_starts_after_threshold_and_stops_requests(self):
        limiter, clock, state, failing = self.setup_limiter()
        first = limiter.run_sync("a|{}", failing)
        second = limiter.run_sync("b|{}", failing)
        third = limiter.run_sync("c|{}", failing)
        self.assertEqual(state["calls"], 3)
        self.assertFalse(first["error"]["detail"]["cooldown"])
        self.assertFalse(second["error"]["detail"]["cooldown"])
        self.assertTrue(third["error"]["detail"]["cooldown"], "第 3 次连续限流进入冷却")
        self.assertEqual(third["error"]["retry_after_ms"], 5000)
        self.assertEqual(third["error"]["detail"]["upstream"], "439")

        # 冷却期内：不发请求（state["calls"] 不变），retry_after_ms 随假时钟递减
        blocked = limiter.run_sync("d|{}", failing)
        self.assertEqual(state["calls"], 3, "冷却期内不得发请求")
        self.assertEqual(blocked["error"]["code"], "futu/rate-limited")
        self.assertTrue(blocked["error"]["detail"]["cooldown"])
        self.assertEqual(blocked["error"]["retry_after_ms"], 5000)
        clock.advance(2.0)
        self.assertEqual(limiter.blocking_cooldown_ms(), 3000)
        later = limiter.run_sync("e|{}", failing)
        self.assertEqual(later["error"]["retry_after_ms"], 3000)
        self.assertEqual(state["calls"], 3)
        clock.advance(3.0)
        self.assertEqual(limiter.blocking_cooldown_ms(), 0, "冷却结束自动恢复")

    def test_recovery_after_cooldown_resets_the_streak(self):
        limiter, clock, state, failing = self.setup_limiter()
        for key in ("a|{}", "b|{}", "c|{}"):
            limiter.run_sync(key, failing)
        self.assertGreater(limiter.blocking_cooldown_ms(), 0)
        clock.advance(5.0)
        good = limiter.run_sync("ok|{}", lambda: {"ok": True, "value": "back"})
        self.assertEqual(good, {"ok": True, "value": "back"})
        self.assertEqual(state["calls"], 3)
        self.assertEqual(limiter.blocking_cooldown_ms(), 0)
        # 冷却后首个成功清零连续计数：再失败两次仍不冷却
        limiter.run_sync("d|{}", failing)
        second = limiter.run_sync("e|{}", failing)
        self.assertFalse(second["error"]["detail"]["cooldown"])

    def test_cooldown_does_not_count_or_block_other_keys_only_itself(self):
        limiter, clock, state, failing = self.setup_limiter()
        for key in ("a|{}", "b|{}", "c|{}"):
            limiter.run_sync(key, failing)
        before = limiter.stats()["calls"]
        limiter.run_sync("other|{}", failing)
        after = limiter.stats()
        self.assertEqual(after["calls"], before + 1, "冷却拒绝仍计入 calls（它确实进来了）")
        self.assertEqual(state["calls"], 3)


# ---------------------------------------------------------------------------
# 透传 / 关闭 / 统计
# ---------------------------------------------------------------------------
class PassthroughTests(unittest.TestCase):
    def test_successful_envelope_with_nested_rate_limit_note_is_passed_through(self):
        """成功信封不被改写、不重试：即便 value 里嵌着「请求过于频繁」也照原样返回。"""
        limiter, _clock = make_limiter()
        payload = {"ok": True, "value": {"groups": [{"acc_id": "A", "reason": "请求过于频繁"}]}}
        result = limiter.run_sync("deals_today|{}", lambda: payload)
        self.assertIs(result, payload, "成功的数据必须原样返回（不改写、不丢）")
        stats = limiter.stats()
        self.assertEqual((stats["retries"], stats["rate_limited"]), (0, 0))

    def test_business_error_envelope_is_returned_untouched(self):
        limiter, _clock = make_limiter()
        payload = {"ok": False, "error": {"code": "market/no-universe",
                                          "message": "没有可用的市场池",
                                          "detail": {"reason": "positions 为空"}}}
        result = limiter.run_sync("positions|{}", lambda: payload)
        self.assertIs(result, payload, "非限流错误必须原样透传（不复制不改写）")
        self.assertEqual(limiter.stats()["retries"], 0)

    def test_unrelated_exception_propagates_unchanged(self):
        limiter, _clock = make_limiter()

        def call():
            raise ValueError("坏参数：ticker 非法")

        with self.assertRaises(ValueError) as caught:
            limiter.run_sync("series|{}", call)
        self.assertEqual(str(caught.exception), "坏参数：ticker 非法")
        stats = limiter.stats()
        self.assertEqual((stats["retries"], stats["rate_limited"]), (0, 0))
        self.assertEqual(stats["in_flight"], 0, "异常路径也必须还槽")

    def test_disabled_limiter_passes_through(self):
        with unittest.mock.patch.dict(os.environ, {rl.ENABLE_ENV: "0"}, clear=False):
            limiter = rl.limiter_from_env()
        self.assertFalse(limiter.enabled)
        value = limiter.run_sync("series|{}", lambda: {"ok": True})
        self.assertEqual(value, {"ok": True})
        stats = limiter.stats()
        self.assertEqual(stats["calls"], 0, "整体关闭时不计数")
        self.assertEqual(limiter.blocking_cooldown_ms(), 0)

    def test_futu_run_gates_by_tool_name(self):
        limiter, clock = make_limiter(rate_per_sec=1.0, burst=1)
        local = []
        futu = []
        rl.futu_run(limiter, "plan", {}, lambda: local.append(1) or {"ok": True})
        rl.futu_run(limiter, "equity", {}, lambda: local.append(2) or {"ok": True})
        self.assertEqual(limiter.stats()["calls"], 0, "本地台账工具直通、不进限流器")
        self.assertEqual(clock.waits, [])
        wrapped = rl.wrap_futu_call(limiter, "series")
        wrapped({}, lambda: futu.append(1) or {"ok": True})
        wrapped({}, lambda: futu.append(2) or {"ok": True})
        self.assertEqual(limiter.stats()["calls"], 2)
        self.assertEqual(clock.waits, [1.0], "第二个富途调用等令牌")
        self.assertEqual((len(local), len(futu)), (2, 2))

    def test_disabled_limiter_gate_passes_everything(self):
        with unittest.mock.patch.dict(os.environ, {rl.ENABLE_ENV: "0"}, clear=False):
            limiter = rl.limiter_from_env()
        self.assertEqual(rl.futu_run(limiter, "series", {}, lambda: {"ok": True}),
                         {"ok": True})
        self.assertEqual(limiter.stats()["calls"], 0)


class StatsAndConfigTests(unittest.TestCase):
    def test_stats_fields_are_complete(self):
        limiter, _clock = make_limiter()
        limiter.run_sync("series|{}", lambda: {"ok": True, "value": 1})
        stats = limiter.stats()
        self.assertEqual(set(stats), {"calls", "coalesced", "retries", "rate_limited",
                                      "throttle_wait_ms", "cooldown_until", "in_flight",
                                      "queued"})
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["cooldown_until"], 0.0)

    def test_metrics_view_camel_case_and_cooldown_epoch(self):
        limiter, clock = make_limiter(retries=0, cooldown_after=1, cooldown_ms=5000)
        limiter.run_sync("a|{}", envelope_12006)
        view = rl.metrics_view(limiter)
        self.assertTrue(view["enabled"])
        for key in ("calls", "coalesced", "retries", "rateLimited", "throttleWaitMs",
                    "cooldownUntil", "inFlight", "queued"):
            self.assertIn(key, view)
        self.assertEqual(view["rateLimited"], 1)
        self.assertEqual(view["cooldownRemainingMs"], 5000)
        self.assertGreater(view["cooldownUntil"], int(time.time() * 1000) - 1000)
        self.assertEqual(view["calls"], 1)

    def test_metrics_view_without_cooldown_reports_zero_until(self):
        limiter, _clock = make_limiter()
        view = rl.metrics_view(limiter)
        self.assertEqual(view["cooldownUntil"], 0)
        self.assertEqual((view["calls"], view["inFlight"], view["queued"]), (0, 0, 0))

    def test_env_defaults_and_overrides(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            limiter = rl.limiter_from_env()
            self.assertEqual((limiter.rate_per_sec, limiter.burst, limiter.max_concurrency),
                             (3.0, 3, 2))
            self.assertEqual((limiter.base_backoff_ms, limiter.max_backoff_ms), (400, 8000))
            self.assertEqual((limiter.cooldown_ms, limiter.cooldown_after, limiter.retries),
                             (5000, 3, 2))
            self.assertTrue(limiter.enabled)
        env = {"QUANT_FUTU_RATE_PER_SEC": "7.5", "QUANT_FUTU_BURST": "9",
               "QUANT_FUTU_MAX_CONCURRENCY": "4", "QUANT_FUTU_BACKOFF_BASE_MS": "100",
               "QUANT_FUTU_BACKOFF_MAX_MS": "2000", "QUANT_FUTU_COOLDOWN_MS": "1500",
               "QUANT_FUTU_COOLDOWN_AFTER": "5", "QUANT_FUTU_RETRY": "1"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            limiter = rl.limiter_from_env()
            self.assertEqual((limiter.rate_per_sec, limiter.burst, limiter.max_concurrency),
                             (7.5, 9, 4))
            self.assertEqual((limiter.base_backoff_ms, limiter.max_backoff_ms), (100, 2000))
            self.assertEqual((limiter.cooldown_ms, limiter.cooldown_after, limiter.retries),
                             (1500, 5, 1))

    def test_env_invalid_values_fall_back_to_defaults(self):
        env = {"QUANT_FUTU_RATE_PER_SEC": "abc", "QUANT_FUTU_BURST": "0",
               "QUANT_FUTU_MAX_CONCURRENCY": "-3", "QUANT_FUTU_RETRY": ""}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            limiter = rl.limiter_from_env()
        self.assertEqual((limiter.rate_per_sec, limiter.burst, limiter.max_concurrency,
                          limiter.retries), (3.0, 3, 2, 2))

    def test_extra_tools_env_extends_the_guard_set(self):
        with unittest.mock.patch.dict(os.environ, {rl.EXTRA_TOOLS_ENV: " my_tool , other "},
                                      clear=False):
            self.assertTrue(rl.is_futu_tool("my_tool"))
            self.assertTrue(rl.is_futu_tool("other"))
        self.assertFalse(rl.is_futu_tool("my_tool"))

    def test_process_singleton_is_shared_and_resettable(self):
        original = rl.get_limiter()
        try:
            marker = rl.reset_limiter(make_limiter()[0])
            self.assertIs(rl.get_limiter(), marker)
            self.assertEqual(rl.stats()["calls"], 0)
            self.assertEqual(rl.blocking_cooldown_ms(), 0)
            self.assertEqual(rl.metrics_view()["calls"], 0)
        finally:
            rl.reset_limiter(original)


if __name__ == "__main__":
    unittest.main()
