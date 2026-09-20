#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step 3 加固层测试：预算 / 循环检测 / 重试 / 故障注入 / 后置校验 / 中间件。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402
from app.domain.services.tools.base import BaseTool, tool  # noqa: E402

from lab.faults.injector import FaultInjector  # noqa: E402
from lab.faults.kinds import FaultKind, FaultRule  # noqa: E402
from lab.guard.budget import Budget, BudgetMode, BudgetPolicy  # noqa: E402
from lab.guard.loop_guard import LoopGuard, signature  # noqa: E402
from lab.guard.postcondition import check_postcondition  # noqa: E402
from lab.guard.retry import compute_backoff, decide_retry, is_idempotent  # noqa: E402
from lab.infra.local_sandbox import LocalSandbox  # noqa: E402
from lab.middleware import GuardedTool, ToolGuard  # noqa: E402
from lab.trace.span import TraceRecorder  # noqa: E402
from lab.usage import Usage  # noqa: E402


# ==================== 测试替身：可编程工具 ====================

class FakeTool(BaseTool):
    """可编程工具（注册两个工具名，以便测试幂等性差异）：

        read_file  -> 幂等（重试安全）
        write_file -> 非幂等（重试会产生重复副作用）

    为什么不用真 FileTool：本文件测的是**中间件**，
    用真工具会把"中间件对不对"和"沙箱对不对"耦在一起（沙箱已有自己的测试）。
    """

    name: str = "fake"

    def __init__(self, results=None, *, error: Exception = None) -> None:
        super().__init__()
        self.calls: list = []
        self._results = list(results or [])
        self._error = error

    async def _run(self, value: str = "") -> ToolResult:
        self.calls.append({"value": value})
        if self._error is not None:
            raise self._error
        if self._results:
            item = self._results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ToolResult(success=True, message="ok", data={"value": value})

    @tool(
        name="read_file",
        description="假的只读工具（幂等）",
        parameters={"value": {"type": "string", "description": "v"}},
        required=[],
    )
    async def read_file(self, value: str = "") -> ToolResult:
        return await self._run(value)

    @tool(
        name="write_file",
        description="假的写入工具（非幂等）",
        parameters={"value": {"type": "string", "description": "v"}},
        required=[],
    )
    async def write_file(self, value: str = "") -> ToolResult:
        return await self._run(value)

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in {"read_file", "write_file"}


# ==================== 1. 预算（observe 模式） ====================

def test_budget_observe_never_intervenes():
    """observe 模式必须**只记录不干预**：它不能改变任何控制流。"""
    usage = Usage()
    policy = BudgetPolicy(mode=BudgetMode.OBSERVE, max_tokens=100, max_tool_calls=1)
    budget = Budget(usage, model_name="deepseek-chat", policy=policy)

    budget.note_tool_call()
    budget.note_tool_call()  # 已经超了 max_tool_calls=1

    violations = budget.check()
    assert [v.metric for v in violations] == ["tool_calls"]
    report = budget.report()
    assert report.mode == "observe"
    # observe 下"本会中止/收尾"只是预演，实际并没停
    assert report.violated_metrics == ["tool_calls"]


def test_budget_violation_is_recorded_only_once():
    """同一维度只在**首次**越界时记录。

    否则"越界次数"就变成"检查次数"的别名，这个指标就没意义了。
    """
    budget = Budget(Usage(), policy=BudgetPolicy(max_tool_calls=1, max_tokens=None))
    budget.note_tool_call()
    budget.note_tool_call()  # 阈值是 1，第 2 次才真正越界
    first = budget.check()
    second = budget.check()
    third = budget.check()

    assert len(first) == 1
    assert second == [] and third == []
    assert len(budget.report().violations) == 1
    assert budget.report().checks == 3


def test_budget_hard_limit_is_multiplier_of_soft():
    """硬上限 = 软阈值 × 倍数，不是手工设的另一个数字。"""
    policy = BudgetPolicy(max_tokens=1000, enforce_multiplier=2.5)
    assert policy.hard_limits()[list(policy.soft_limits())[0]] == 2500

    usage = Usage()
    usage.add_llm_call({"prompt_tokens": 3000, "completion_tokens": 0, "total_tokens": 3000}, 0)
    budget = Budget(usage, model_name="deepseek-chat", policy=policy)
    violations = budget.check()

    assert violations[0].exceeded_hard is True  # 3000 > 2500
    assert budget.report().would_stop is True  # enforce 模式下本会中止
    assert budget.report().hard_exceeded is True


def test_budget_policy_from_env(monkeypatch):
    """阈值可通过环境变量覆盖（CI 里换阈值不需要改代码）。"""
    monkeypatch.setenv("LAB_BUDGET_TOKENS", "500")
    monkeypatch.setenv("LAB_BUDGET_MODE", "enforce")
    monkeypatch.setenv("LAB_BUDGET_STEPS", "off")

    policy = BudgetPolicy.from_env()
    assert policy.max_tokens == 500
    assert policy.mode == BudgetMode.ENFORCE
    assert policy.max_steps is None  # 显式关闭


def test_budget_defaults_match_user_baseline():
    """默认阈值必须与用户给的 n=5 基线一致（这条测试防止有人随手改数字）。"""
    policy = BudgetPolicy()
    assert policy.max_tokens == 150_000
    assert policy.max_cost_usd == 0.10
    assert policy.max_seconds == 60.0
    assert policy.max_steps == 15
    assert policy.max_tool_calls == 15
    assert policy.max_llm_calls is None  # 只观察，不设阈值
    assert policy.mode == BudgetMode.OBSERVE


# ==================== 2. 循环检测 ====================

def test_loop_guard_detects_consecutive_repeats():
    guard = LoopGuard(repeat_threshold=3)
    for _ in range(3):
        hit = guard.observe("shell_execute", {"command": "ls"})

    assert hit.consecutive == 3
    assert hit.would_trip is True
    assert len(guard.trips) == 1


def test_loop_guard_does_not_flag_same_tool_with_different_args():
    """关键区分：同一工具**不同参数**不算循环。

    只按工具名去重会把"依次写 5 个文件"这种正常行为误判成卡死，
    然后熔断掉一个本来能成功的任务 —— 这比不熔断更糟。
    """
    guard = LoopGuard(repeat_threshold=3)
    for index in range(5):
        hit = guard.observe("write_file", {"filepath": f"/f{index}.txt"})

    assert hit.consecutive == 1
    assert hit.would_trip is False
    assert guard.trips == []
    assert hit.diversity == 1.0  # 5 次调用 5 个不同指纹


def test_loop_guard_alternating_is_not_consecutive():
    """A B A B A B：累计 3 次但不连续 → 更像探索，不熔断。"""
    guard = LoopGuard(repeat_threshold=3)
    for _ in range(3):
        guard.observe("read_file", {"filepath": "/a"})
        hit = guard.observe("read_file", {"filepath": "/b"})

    assert hit.consecutive == 1
    assert hit.would_trip is False
    assert hit.total == 3  # 累计确实是 3


def test_loop_guard_reset_after_step():
    """跨步骤的"连续"没有意义，进入新步骤要重置。"""
    guard = LoopGuard(repeat_threshold=2)
    guard.observe("read_file", {"filepath": "/a"})
    guard.reset_after_step()
    hit = guard.observe("read_file", {"filepath": "/a"})

    assert hit.consecutive == 1
    assert hit.would_trip is False


def test_signature_is_order_insensitive():
    """参数顺序不同、内容相同的调用要算同一个指纹。"""
    assert signature("f", {"a": 1, "b": 2}) == signature("f", {"b": 2, "a": 1})


# ==================== 3. 重试（幂等性感知 —— D2 的正解） ====================

def test_non_idempotent_tools_are_never_auto_retried():
    """D2 的核心验收：非幂等工具即使遇到暂态失败也不自动重试。"""
    decision = decide_retry(
        function_name="write_file",
        result=ToolResult(success=False, message="超时", error_type="timeout", retryable=True),
        error=None,
        attempt=1,
        max_attempts=3,
    )
    assert decision.should_retry is False
    assert decision.reason == "skipped_non_idempotent"


def test_idempotent_tool_is_retried_on_transient_failure():
    decision = decide_retry(
        function_name="read_file",
        result=ToolResult(success=False, message="超时", error_type="timeout", retryable=True),
        error=None,
        attempt=1,
        max_attempts=3,
    )
    assert decision.should_retry is True
    assert decision.delay > 0


def test_fatal_error_types_are_never_retried():
    """确定性错误（工具不存在/路径越界/参数非法）重试必然同样失败。"""
    decision = decide_retry(
        function_name="read_file",
        result=ToolResult(success=False, message="不存在", error_type="file_not_found", retryable=True),
        error=None,
        attempt=1,
        max_attempts=3,
    )
    assert decision.should_retry is False
    assert decision.reason == "fatal_error_type:file_not_found"


def test_unknown_tools_default_to_non_idempotent():
    """fail-safe：不知道的工具按危险处理（MCP 动态工具就属于这一类）。"""
    assert is_idempotent("some_mcp_tool_we_never_saw") is False
    assert is_idempotent("read_file") is True
    assert is_idempotent("write_file") is False


def test_backoff_is_bounded_and_deterministic_with_seed():
    import random

    rng_a, rng_b = random.Random(7), random.Random(7)
    delays_a = [compute_backoff(i, rng=rng_a) for i in range(1, 6)]
    delays_b = [compute_backoff(i, rng=rng_b) for i in range(1, 6)]

    assert delays_a == delays_b  # 同 seed → 可复现
    assert all(0 < d <= 8.0 for d in delays_a)  # 有上限
    assert delays_a[0] < delays_a[2]  # 指数增长


def test_backoff_jitter_spreads_retries():
    """抖动存在的意义：避免并行评测时所有任务同时重试形成重试风暴。"""
    import random

    values = {round(compute_backoff(1, rng=random.Random(s)), 6) for s in range(20)}
    assert len(values) > 10  # 不同 seed 得到不同延迟


# ==================== 4. 故障注入 ====================

def _injector(*rules):
    return FaultInjector(list(rules), seed=0)


def test_injection_is_reproducible_with_fixed_seed():
    """故障注入必须可复现，否则"加固后成功率提升"无法归因。"""
    run_a = [FaultInjector([FaultRule(kind=FaultKind.FLAKY, tool="*", rate=0.5)], seed=1).decide("f")
             for _ in range(1)]
    del run_a

    first = FaultInjector([FaultRule(kind=FaultKind.FLAKY, rate=0.5)], seed=1)
    second = FaultInjector([FaultRule(kind=FaultKind.FLAKY, rate=0.5)], seed=1)
    seq_a = [bool(first.decide("f")) for _ in range(30)]
    seq_b = [bool(second.decide("f")) for _ in range(30)]
    assert seq_a == seq_b
    assert 0 < sum(seq_a) < 30  # 既不是全中也不是全不中


def test_rule_matching_supports_glob_and_start_after():
    injector = _injector(FaultRule(kind=FaultKind.PERMANENT_ERROR, tool="shell_*", start_after=2))

    assert injector.decide("read_file") is None  # 名字不匹配
    assert injector.decide("shell_execute") is None  # 第 1 次（index=0 < 2）
    assert injector.decide("shell_execute") is None  # 第 2 次（index=1 < 2）
    assert injector.decide("shell_execute") is not None  # 第 3 次开始注入


def test_transient_error_is_sequence_based_not_probabilistic():
    """transient_error 必须是"前 N 次失败、之后正常"。

    如果实现成"每次按概率失败"，重试也会同样概率失败，
    你就无法确认重试机制到底有没有生效 —— 这是本设计的关键点。
    """
    injector = _injector(FaultRule(kind=FaultKind.TRANSIENT_ERROR, fail_times=1))

    assert injector.decide("read_file") is not None  # 第 1 次失败
    assert injector.decide("read_file") is None  # 第 2 次（重试）成功
    assert injector.decide("read_file") is None


# ==================== 4. 故障注入（续） ====================


def test_max_injections_caps_total_faults():
    injector = _injector(FaultRule(kind=FaultKind.PERMANENT_ERROR, max_injections=2))
    hits = [injector.decide("f") is not None for _ in range(5)]
    assert sum(hits) == 2


def test_all_ten_fault_kinds_have_a_distinct_behavior():
    """10 类故障都要能真正产生各自的特征（防止"写了枚举但没实现"）。"""
    # 1-3 抛异常类
    for kind in (FaultKind.TIMEOUT, FaultKind.TRANSIENT_ERROR, FaultKind.PERMANENT_ERROR, FaultKind.FLAKY):
        injector = _injector(FaultRule(kind=kind))
        decision = injector.decide("f")
        assert injector.maybe_raise(decision, "f") is not None, kind

    # 4-7 静默失败类：结果被改动但 success 仍为 True
    base = ToolResult(success=True, message="ok", data={"filepath": "/a", "content": "0123456789abcdef"})
    for kind in (FaultKind.EMPTY_RESULT, FaultKind.MALFORMED_RESULT,
                 FaultKind.TRUNCATED_RESULT, FaultKind.SILENT_WRONG_RESULT):
        injector = _injector(FaultRule(kind=kind))
        decision = injector.decide("read_file")
        mutated = injector.mutate_result(decision, "read_file", base)
        assert mutated.success is True, kind  # 静默 = 不报错
        assert mutated.data != base.data, kind

    # 8 参数篡改类：真的只写一半
    injector = _injector(FaultRule(kind=FaultKind.PARTIAL_WRITE))
    decision = injector.decide("write_file")
    mutated = injector.mutate_args(decision, {"content": "x" * 100})
    assert len(mutated["content"]) == 50

    # 9 延迟类：不改结果，只加时间
    injector = _injector(FaultRule(kind=FaultKind.LATENCY_SPIKE, latency_s=0.01))
    decision = injector.decide("f")
    assert injector.maybe_raise(decision, "f") is None
    assert injector.mutate_result(decision, "f", base).data == base.data

    # 10 已覆盖（FLAKY 在上面）


async def test_latency_spike_does_not_change_result_semantics():
    """延迟类故障不能顺手把结果也改了（否则就分不清是哪类故障在起作用）。"""
    injector = _injector(FaultRule(kind=FaultKind.LATENCY_SPIKE, latency_s=0.01))
    decision = injector.decide("read_file")
    base = ToolResult(success=True, message="ok", data={"content": "unchanged"})
    assert injector.mutate_result(decision, "read_file", base) is base


def test_silent_faults_never_corrupt_write_results():
    """静默篡改只能作用于**只读**工具。

    篡改 write_file 的返回值等于"伪造成功"，
    会让 workspace 的实际内容和 trace 记录不一致 —— 那是最坏的一种实验污染。
    """
    injector = _injector(FaultRule(kind=FaultKind.SILENT_WRONG_RESULT))
    decision = injector.decide("write_file")
    original = ToolResult(success=True, message="ok", data={"filepath": "/a", "bytes_written": 5})
    assert injector.mutate_result(decision, "write_file", original).data == original.data


def test_silent_faults_are_classified_as_silent():
    assert FaultKind.SILENT_WRONG_RESULT.is_silent is True
    assert FaultKind.PARTIAL_WRITE.is_silent is True
    assert FaultKind.TIMEOUT.is_silent is False
    assert FaultKind.TIMEOUT.is_retryable_class is True


def test_failed_results_are_not_mutated():
    """已经失败的结果不需要再篡改（篡改只用来制造"假成功"）。"""
    injector = _injector(FaultRule(kind=FaultKind.EMPTY_RESULT))
    decision = injector.decide("f")
    failed = ToolResult(success=False, message="boom", error_type="tool_error")
    assert injector.mutate_result(decision, "f", failed) is failed


# ==================== 5. 后置校验 ====================

async def test_postcondition_detects_missing_fields():
    """malformed_result：声称成功但字段缺失。"""
    warning = await check_postcondition(
        function_name="read_file",
        args={"filepath": "/a"},
        result=ToolResult(success=True, message="ok", data={"status": "ok"}),
    )
    assert warning and "缺少字段" in warning


async def test_postcondition_detects_empty_data():
    warning = await check_postcondition(
        function_name="read_file",
        args={"filepath": "/a"},
        result=ToolResult(success=True, message="ok", data=None),
    )
    assert warning and "空数据" in warning


async def test_postcondition_detects_partial_write_via_readback(tmp_path: Path):
    """partial_write：回读文件比对内容 —— 这是最强的后置校验。

    注意比较基准是**调用方的原始参数**（它想要写什么），
    而不是工具声称写了什么。否则工具自说自话就永远校验通过。
    """
    sandbox = LocalSandbox(tmp_path / "ws")
    # 文件里实际只有一半内容
    await sandbox.write_file("/home/ubuntu/a.txt", "x" * 50)

    warning = await check_postcondition(
        function_name="write_file",
        args={"filepath": "/home/ubuntu/a.txt", "content": "x" * 100},
        result=ToolResult(success=True, message="ok", data={"filepath": "/home/ubuntu/a.txt", "bytes_written": 100}),
        sandbox=sandbox,
    )
    assert warning and "不一致" in warning


async def test_postcondition_passes_on_correct_write(tmp_path: Path):
    sandbox = LocalSandbox(tmp_path / "ws")
    content = "hello\nworld"
    await sandbox.write_file("/home/ubuntu/b.txt", content)

    warning = await check_postcondition(
        function_name="write_file",
        args={"filepath": "/home/ubuntu/b.txt", "content": content},
        result=ToolResult(
            success=True, message="ok",
            data={"filepath": "/home/ubuntu/b.txt", "bytes_written": len(content.encode())},
        ),
        sandbox=sandbox,
    )
    assert warning is None


async def test_postcondition_accounts_for_newline_options(tmp_path: Path):
    """带 leading/trailing newline 的写入也要能正确复算出期望内容。"""
    sandbox = LocalSandbox(tmp_path / "ws")
    # 注意：content="line" + trailing_newline=True → 文件内容应是 "line\n"（5 字符）
    await sandbox.write_file("/home/ubuntu/c.txt", "line", trailing_newline=True)

    warning = await check_postcondition(
        function_name="write_file",
        args={"filepath": "/home/ubuntu/c.txt", "content": "line", "trailing_newline": True},
        result=ToolResult(success=True, message="ok", data={"filepath": "/home/ubuntu/c.txt", "bytes_written": 5}),
        sandbox=sandbox,
    )
    assert warning is None


async def test_postcondition_skips_append_content_check(tmp_path: Path):
    """追加模式无法用"内容相等"判断，必须**明确降级**而不是假装校验过了。"""
    sandbox = LocalSandbox(tmp_path / "ws")
    await sandbox.write_file("/home/ubuntu/d.txt", "old\n")
    await sandbox.write_file("/home/ubuntu/d.txt", "new\n", append=True)

    warning = await check_postcondition(
        function_name="write_file",
        args={"filepath": "/home/ubuntu/d.txt", "content": "new\n", "append": True},
        result=ToolResult(success=True, message="ok", data={"filepath": "/home/ubuntu/d.txt", "bytes_written": 4}),
        sandbox=sandbox,
    )
    assert warning is None  # 不误报


async def test_postcondition_does_not_check_failed_results():
    warning = await check_postcondition(
        function_name="read_file",
        args={"filepath": "/a"},
        result=ToolResult(success=False, message="boom", error_type="tool_error", data=None),
    )
    assert warning is None


# ==================== 6. 中间件（把上面全部串起来） ====================

async def test_middleware_retries_idempotent_tool_on_injected_transient_fault():
    """端到端验证"故障注入 → 重试 → 最终成功"这条链路。

    用 read_file（幂等）+ transient_error。第一次注入失败，重试后成功。
    """
    tool_impl = FakeTool()
    injector = FaultInjector([FaultRule(kind=FaultKind.TRANSIENT_ERROR, tool="read_file", fail_times=1)])
    guard = ToolGuard(injector=injector)

    result = await guard.call_tool(tool_impl, "read_file", {"value": "v"})

    assert result.success is True
    # 注意语义：注入的故障发生在**真实调用之前**（模拟网络层失败），
    # 所以第 1 次尝试根本没碰到工具，工具只被调用了 1 次 ——
    # 换句话说，**重试真的补上了那次被故障吞掉的调用**。
    assert len(tool_impl.calls) == 1
    assert result.attempts == 2
    assert guard.fault_counts.get("transient_error") == 1


async def test_middleware_does_not_retry_non_idempotent_tool_on_transient_fault():
    """D2 的端到端验收：**同样的暂态故障**发生在非幂等工具上时，不能自动重试。

    这是同一类故障、同一个中间件，只有工具名不同 —— 结果必须相反。
    """
    tool_impl = FakeTool()
    injector = FaultInjector([FaultRule(kind=FaultKind.TRANSIENT_ERROR, tool="write_file", fail_times=1)])
    guard = ToolGuard(injector=injector)

    result = await guard.call_tool(tool_impl, "write_file", {"value": "v"})

    assert result.success is False
    assert len(tool_impl.calls) == 0  # 注入器直接抛错，真工具一次都没被调用
    assert "skipped_non_idempotent" in guard.retry_stats or result.attempts == 1
    assert result.attempts == 1


async def test_middleware_never_raises_and_returns_structured_failure():
    """中间件永不抛异常 —— 这是"SUT 的重试退化为兜底、不会双重试"的前提。"""
    tool_impl = FakeTool(error=RuntimeError("boom"))
    guard = ToolGuard()  # 无注入，纯真实异常

    result = await guard.call_tool(tool_impl, "read_file", {})

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error_type == "tool_error"
    assert result.retryable is False


async def test_middleware_records_attempts_on_returned_result():
    """attempts 必须如实反映尝试次数，否则 D2 类问题看不见。"""
    tool_impl = FakeTool(error=ConnectionError("flaky"))
    guard = ToolGuard(max_attempts=3)

    result = await guard.call_tool(tool_impl, "read_file", {})  # 幂等 → 会重试到上限

    assert result.attempts == 3  # 幂等工具 → 重试到上限
    assert result.error_type == "connection_error"


async def test_middleware_annotates_current_span():
    """中间件的结论要写进**已经存在的**工具 span，而不是新建一个。

    否则一次工具调用会出现两个 span，所有工具类指标都会翻倍。
    """
    recorder = TraceRecorder("t1")
    tool_span = recorder.open_tool("read_file", "fake", {})

    tool_impl = FakeTool()
    guard = ToolGuard(recorder=recorder, loop_guard=LoopGuard(repeat_threshold=2))
    await guard.call_tool(tool_impl, "read_file", {})
    await guard.call_tool(tool_impl, "read_file", {})  # 第 2 次 → 触发熔断候选

    span = tool_span.span
    assert span.attrs["idempotent"] is True
    assert span.attrs["repeat_consecutive"] == 2
    # span 数量没变（中间件没新建 span）
    assert len([s for s in recorder.spans if s.kind.value == "tool"]) == 1


async def test_middleware_detects_partial_write_end_to_end(tmp_path: Path):
    """完整链路：注入 partial_write → 真的只写一半 → 后置校验抓住它。

    这条测试是整个 Step 3 的核心论点：
    **重试救不了静默失败，只有后置校验能。**
    """
    sandbox = LocalSandbox(tmp_path / "ws")
    tool_impl = FakeTool()  # 用真工具更好，这里用假工具模拟"写入后返回成功"

    # 让假工具真的走一次沙箱写入，以便回读校验有意义
    class WriteFake(BaseTool):
        name: str = "file"

        async def invoke(self, tool_name: str, **kwargs):
            written = await sandbox.write_file(kwargs["filepath"], kwargs["content"])
            return written

        def get_tools(self):
            return []

        def has_tool(self, tool_name: str) -> bool:
            return tool_name == "write_file"

    injector = FaultInjector([FaultRule(kind=FaultKind.PARTIAL_WRITE, tool="write_file")])
    guard = ToolGuard(injector=injector, sandbox=sandbox)

    result = await guard.call_tool(
        WriteFake(), "write_file", {"filepath": "/home/ubuntu/p.txt", "content": "y" * 100}
    )

    # 工具自己说成功（静默失败的定义）
    assert result.success is True
    # 但后置校验发现了真相
    assert guard.postcondition_warnings
    assert "不一致" in guard.postcondition_warnings[0]
    # 文件里确实只有一半 —— 故障是"真的"发生了，不是伪造的报告
    assert len((tmp_path / "ws" / "home" / "ubuntu" / "p.txt").read_text()) == 50


async def test_middleware_budget_observation_without_intervention():
    """observe 模式下预算越界不影响正常返回（只记录）。"""
    usage = Usage()
    usage.add_llm_call({"prompt_tokens": 999_999, "completion_tokens": 0, "total_tokens": 999_999}, 0)
    budget = Budget(usage, model_name="deepseek-chat", policy=BudgetPolicy())
    guard = ToolGuard(budget=budget)

    result = await guard.call_tool(FakeTool(), "read_file", {})

    assert result.success is True  # 干预被关掉了
    # 999,999 token 同时把 token 与成本两条阈值都顶穿了
    assert set(budget.report().violated_metrics) == {"tokens", "cost_usd"}
    assert budget.report().would_stop is True  # 但记录了"本会怎样"


async def test_guard_reports_are_complete():
    """报告要给 Step 4 的聚合分析提供全部字段。"""
    guard = ToolGuard(
        budget=Budget(Usage(), model_name="deepseek-chat"),
        injector=FaultInjector([FaultRule(kind=FaultKind.EMPTY_RESULT, tool="read_file")]),
        sandbox=None,
    )
    await guard.call_tool(FakeTool(), "read_file", {})

    report = guard.report()
    assert set(report) >= {"budget", "loop", "retries", "postconditions", "faults"}
    assert report["budget"]["mode"] == "observe"
    assert report["faults"]["injections"] == 1
    assert report["loop"]["tool_calls"] == 1.0


async def test_delay_injection_actually_waits():
    """latency_spike 必须真的产生延迟（否则"耗时预算"这条防线就是假的）。

    注意断言的写法：注入 0.2s、断言 >= 0.15s。
    **不要**写成"注入 0.15 就断言 >= 0.15" —— 卡在精确边界上会偶发失败
    （事件循环的计时精度、测量本身的开销都会让它偶尔差一点点），
    这种"假失败"会训练人忽略测试结果，比没有测试更糟。
    """
    injector = FaultInjector([FaultRule(kind=FaultKind.LATENCY_SPIKE, latency_s=0.2)])
    guard = ToolGuard(injector=injector)

    started = asyncio.get_event_loop().time()
    await guard.call_tool(FakeTool(), "read_file", {})
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed >= 0.15


# ==================== 7. 加固开关（Step 6：对照实验的前提） ====================

def test_guard_config_parsing():
    """spec 解析：all / none / 显式能力名；未知名字必须报错而不是静默忽略。"""
    from lab.guard.config import GuardConfig

    assert GuardConfig.from_spec("all").label == "all"
    assert GuardConfig.from_spec(None).label == "all"

    none = GuardConfig.from_spec("none")
    assert none.label == "none"
    assert not any([none.retry, none.postconditions, none.loop_guard, none.budget])

    # 显式列出 = **只开这些**（其余关闭），这样对照实验才可预测
    only_retry = GuardConfig.from_spec("retry")
    assert only_retry.retry is True
    assert only_retry.postconditions is False

    # 别名
    assert GuardConfig.from_spec("check").postconditions is True
    assert GuardConfig.from_spec("retry,postcondition").label == "retry+postconditions"

    with pytest.raises(ValueError, match="未知的加固能力"):
        GuardConfig.from_spec("retry,bogus")


async def test_guard_switch_actually_disables_retry():
    """开关必须真的改变行为 —— 否则对照实验是假的。

    注入 `transient_error`（幂等工具）：guard=all 会重试，guard=none 不会。
    """
    from lab.guard.config import GuardConfig
    from lab.middleware import ToolGuard

    def _injector():
        return FaultInjector([FaultRule(kind=FaultKind.TRANSIENT_ERROR, tool="read_file", fail_times=1)])

    with_guard = ToolGuard(injector=_injector(), config=GuardConfig.from_spec("all"))
    result_all = await with_guard.call_tool(FakeTool(), "read_file", {})
    assert result_all.attempts == 2  # 重试补上了第一次的失败
    assert result_all.success is True

    without_guard = ToolGuard(injector=_injector(), config=GuardConfig.from_spec("none"))
    result_none = await without_guard.call_tool(FakeTool(), "read_file", {})
    assert result_none.attempts == 1  # 不重试
    assert result_none.success is False


async def test_guard_switch_actually_disables_postconditions(tmp_path):
    """关掉后置校验 → **静默失败就没人抓**。

    这正是 A/B 实验要展示的东西：加固的价值在"静默失败"上，
    没有它，`partial_write` 这种只写了一半的写入会被当成成功。
    """
    from lab.guard.config import GuardConfig
    from lab.middleware import ToolGuard

    class WriteFileTool(BaseTool):
        name: str = "file"

        def __init__(self, sandbox) -> None:
            super().__init__()
            self.sandbox = sandbox

        def get_tools(self):
            return []

        def has_tool(self, tool_name: str) -> bool:
            return tool_name == "write_file"

        async def invoke(self, tool_name: str, **kwargs):
            return await self.sandbox.write_file(kwargs["filepath"], kwargs["content"])

    def _injector():
        return FaultInjector([FaultRule(kind=FaultKind.PARTIAL_WRITE, tool="write_file")])

    args = {"filepath": "/home/ubuntu/a.txt", "content": "x" * 100}

    sandbox_all = LocalSandbox(tmp_path / "with")
    guard_all = ToolGuard(injector=_injector(), sandbox=sandbox_all, config=GuardConfig.from_spec("all"))
    result_all = await guard_all.call_tool(WriteFileTool(sandbox_all), "write_file", args)
    assert result_all.success is True  # 工具自己说成功（静默失败）
    assert guard_all.postcondition_warnings  # 但后置校验抓到了

    sandbox_none = LocalSandbox(tmp_path / "without")
    guard_none = ToolGuard(injector=_injector(), sandbox=sandbox_none, config=GuardConfig.from_spec("none"))
    result_none = await guard_none.call_tool(WriteFileTool(sandbox_none), "write_file", args)
    assert result_none.success is True  # 同样"成功"
    assert not guard_none.postcondition_warnings  # 但关掉校验后**没有任何人发现**

    # 两次写入都真的只写了一半（故障是真的发生的）
    assert (tmp_path / "with" / "home" / "ubuntu" / "a.txt").read_text() == "x" * 50
    assert (tmp_path / "without" / "home" / "ubuntu" / "a.txt").read_text() == "x" * 50


async def test_guard_config_is_recorded_for_attribution():
    """加固配置必须进报告 —— 没有它，两组数据落库后无法区分是谁的。"""
    from lab.guard.config import GuardConfig
    from lab.middleware import ToolGuard

    guard = ToolGuard(config=GuardConfig.from_spec("retry"))
    await guard.call_tool(FakeTool(), "read_file", {})

    report = guard.report()
    assert report["config"] == "retry"  # 标签进报告
    assert report["features"]["retry"] is True
    assert report["features"]["postconditions"] is False


async def test_postcondition_enforce_feedback_closes_the_self_healing_loop(tmp_path):
    """`enforce` 把「校验发现的问题」变成**对 LLM 可见的失败**。

    默认（observe）下，后置校验只记一条告警，而那个 ToolResult 仍以
    `success=True` 返回给模型 —— 模型于是继续在错误的事实上推理。
    `enforce=True` 才是「检测 → 回灌 → 自愈」的闭环。
    """
    from lab.guard.config import GuardConfig
    from lab.middleware import ToolGuard

    class WriteFileTool(BaseTool):
        name: str = "file"

        def __init__(self, sandbox) -> None:
            super().__init__()
            self.sandbox = sandbox

        def get_tools(self):
            return []

        def has_tool(self, tool_name: str) -> bool:
            return tool_name == "write_file"

        async def invoke(self, tool_name: str, **kwargs):
            return await self.sandbox.write_file(kwargs["filepath"], kwargs["content"])

    def _injector():
        return FaultInjector([FaultRule(kind=FaultKind.PARTIAL_WRITE, tool="write_file")])

    args = {"filepath": "/home/ubuntu/a.txt", "content": "x" * 100}

    # observe（默认）：只有告警，结果仍然"成功" → 模型看不到问题
    sandbox_observe = LocalSandbox(tmp_path / "observe")
    guard_observe = ToolGuard(injector=_injector(), sandbox=sandbox_observe,
                              config=GuardConfig.from_spec("retry,postcondition"))
    result_observe = await guard_observe.call_tool(WriteFileTool(sandbox_observe), "write_file", args)
    assert result_observe.success is True  # ← 静默失败：模型以为成功了
    assert guard_observe.postcondition_warnings

    # enforce：结果变成失败，且告警文本进 message → 模型能自己纠正
    sandbox_enforce = LocalSandbox(tmp_path / "enforce")
    guard_enforce = ToolGuard(injector=_injector(), sandbox=sandbox_enforce,
                              config=GuardConfig.from_spec("retry,postcondition,enforce"))
    result_enforce = await guard_enforce.call_tool(WriteFileTool(sandbox_enforce), "write_file", args)
    assert result_enforce.success is False
    assert result_enforce.error_type == "postcondition_failed"
    assert "不一致" in (result_enforce.message or "")  # 具体原因回灌给模型
    assert result_enforce.retryable is False  # 是否重试交给模型判断


# ==================== 8. 读路径检测面（唯一"Agent 无法自证"的故障类） ====================

def _result(content=None, **data):
    from app.domain.models.tool_result import ToolResult

    payload = {"filepath": data.pop("filepath", "/home/ubuntu/a.json")}
    if content is not None:
        payload["content"] = content
    payload.update(data)
    return ToolResult(success=True, message="ok", data=payload)


def test_content_check_catches_broken_json_on_read_path():
    """读路径的静默损坏：Agent 拿回半截 JSON 也会直接用 —— 只有内容级校验能发现。

    这是**唯一一类 Agent 自身无法发现**的故障：它没有外部真相可比，
    所以必须由独立的一层来做结构合法性判定（而 JSON 能不能解析是客观不变量）。
    """
    from lab.guard.postcondition import check_postcondition_content

    broken = _result('{"items": [1, 2, 3')  # 被截断的 JSON
    warning = check_postcondition_content(function_name="read_file", args={}, result=broken)
    assert warning and "无法解析" in warning and "截断" in warning

    healthy = _result('{"items": [1, 2, 3]}')
    assert check_postcondition_content(function_name="read_file", args={}, result=healthy) is None

    # 非 JSON 内容不该被这条规则碰（避免把普通文本误判）
    plain = _result("这是一段普通文本，不是 JSON")
    assert check_postcondition_content(function_name="read_file", args={}, result=plain) is None


def test_content_check_skips_legitimately_truncated_content():
    """误报防范：工具**按上限截内容**是正确行为，不能当成损坏。

    `read_file` 默认只读 10000 字符；一个 50KB 的 JSON 读回来就是半截，
    直接做 JSON 解析必然失败。不区分就会制造大量假告警 ——
    而**假告警会让真告警失效**（和之前那次 98 次噪声告警同一个教训）。
    """
    from lab.guard.postcondition import check_postcondition_content

    half_json = '{"items": [' + "1," * 50
    # 1.工具自己标了 truncated
    assert check_postcondition_content(
        function_name="read_file", args={}, result=_result(half_json, truncated=True)) is None
    # 2.内容长度正好等于请求的 max_length → 强提示是被上限截的
    assert check_postcondition_content(
        function_name="read_file", args={"max_length": len(half_json)},
        result=_result(half_json)) is None


async def test_read_consistency_is_audit_only_never_enforced(tmp_path):
    """读写交叉比对**只作审计信号**，绝不参与 enforce。

    理由：文件可能被 `shell_execute` 里的命令改过（sed / 重定向），
    这时读写不一致是**正常现象**。拿它去 enforce 会造成假失败 ——
    而把正确的工作判错，比漏掉一次损坏更糟。
    """
    from lab.guard.config import GuardConfig
    from lab.middleware import ToolGuard

    class FileTool(BaseTool):
        name: str = "file"

        def __init__(self, sandbox) -> None:
            super().__init__()
            self.sandbox = sandbox

        def get_tools(self):
            return []

        def has_tool(self, tool_name: str) -> bool:
            return tool_name in {"write_file", "read_file"}

        async def invoke(self, tool_name: str, **kwargs):
            if tool_name == "write_file":
                return await self.sandbox.write_file(kwargs["filepath"], kwargs["content"])
            return await self.sandbox.read_file(kwargs["filepath"])

    sandbox = LocalSandbox(tmp_path / "ws")
    guard = ToolGuard(sandbox=sandbox, config=GuardConfig.from_spec("retry,postcondition,enforce"))
    tool = FileTool(sandbox)

    await guard.call_tool(tool, "write_file",
                          {"filepath": "/home/ubuntu/a.txt", "content": "x" * 100})
    # 模拟文件被 shell 命令改短了（正常现象）
    await sandbox.write_file("/home/ubuntu/a.txt", "x" * 10)

    result = await guard.call_tool(tool, "read_file", {"filepath": "/home/ubuntu/a.txt"})

    assert guard.audit_warnings, "读写不一致没有被记成审计信号"
    assert "不一致" in guard.audit_warnings[0]
    # 关键：即使 enforce 开着，工具结果仍然成功 —— 因为它只是"可疑"，不是"确定错"
    assert result.success is True
    assert not guard.postcondition_warnings


async def test_read_consistency_passes_when_content_matches(tmp_path):
    """内容一致时不能误报（否则审计信号同样是噪声）。"""
    from lab.guard.config import GuardConfig
    from lab.middleware import ToolGuard

    class FileTool(BaseTool):
        name: str = "file"

        def __init__(self, sandbox) -> None:
            super().__init__()
            self.sandbox = sandbox

        def get_tools(self):
            return []

        def has_tool(self, tool_name: str) -> bool:
            return tool_name in {"write_file", "read_file"}

        async def invoke(self, tool_name: str, **kwargs):
            if tool_name == "write_file":
                return await self.sandbox.write_file(kwargs["filepath"], kwargs["content"])
            return await self.sandbox.read_file(kwargs["filepath"])

    sandbox = LocalSandbox(tmp_path / "ws")
    guard = ToolGuard(sandbox=sandbox, config=GuardConfig.from_spec("all"))
    tool = FileTool(sandbox)

    await guard.call_tool(tool, "write_file",
                          {"filepath": "/home/ubuntu/b.txt", "content": "hello"})
    await guard.call_tool(tool, "read_file", {"filepath": "/home/ubuntu/b.txt"})

    assert guard.audit_warnings == []
