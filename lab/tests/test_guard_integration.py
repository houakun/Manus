#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step 3 端到端验收：中间件真的被接进了 SUT 的工具调用路径。

== 这个文件存在的意义 ==
`lab/tests/test_guard.py` 测的是中间件**自身**的逻辑（预算/重试/校验各自对不对）；
本文件测的是**接线**：SUT 真跑一遍 flow 时，工具调用有没有真的经过中间件。

两者必须分开：如果只有前者，中间件写得再对，只要没接上就一点用都没有 ——
而"没接上"这种错在单测里是完全看不出来的（单测直接调 `guard.call_tool`）。
"""

from __future__ import annotations

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from lab.faults.injector import FaultInjector  # noqa: E402
from lab.faults.kinds import FaultKind, FaultRule  # noqa: E402
from lab.guard.budget import BudgetMode, BudgetPolicy  # noqa: E402
from lab.tests.fakes import planner_react_script  # noqa: E402
from lab.tests.test_trace_integration import _build_sut  # noqa: E402
from lab.trace.span import SpanKind, TraceRecorder  # noqa: E402


async def test_tool_calls_go_through_middleware(tmp_path):
    """接线验收：工具 span 上出现了只有中间件才会写的属性。"""
    recorder = TraceRecorder("t-guard-wired")
    sut, _ = _build_sut(tmp_path, planner_react_script(), recorder)

    result = await sut.run("把 hello 写入 /home/ubuntu/a.txt")

    assert result.ok is True
    # 1.任务结果里带上了加固层报告
    assert result.guard["budget"]["mode"] == "observe"
    assert result.guard["loop"]["tool_calls"] >= 1
    assert "retries" in result.guard

    # 2.工具 span 上有中间件写的属性（这是"真的接上了"的铁证）
    tool_spans = [s for s in recorder.spans if s.kind == SpanKind.TOOL]
    assert tool_spans
    attrs = tool_spans[0].attrs
    assert "idempotent" in attrs
    assert "action_diversity" in attrs
    assert "repeat_consecutive" in attrs
    # write_file 必须被识别为非幂等 —— D2 防线的基础
    assert attrs["idempotent"] is False


async def test_middleware_does_not_duplicate_tool_spans(tmp_path):
    """中间件只**补注**已有 span，不能新建 —— 否则所有工具类指标都会翻倍。"""
    recorder = TraceRecorder("t-no-dup")
    sut, _ = _build_sut(tmp_path, planner_react_script(), recorder)

    result = await sut.run("写文件")

    tool_spans = [s for s in recorder.spans if s.kind == SpanKind.TOOL]
    # 脚本里只调了一次 write_file → 只能有一个 tool span
    assert len(tool_spans) == 1
    assert result.guard["loop"]["tool_calls"] == 1.0


async def test_partial_write_is_caught_by_postcondition_end_to_end(tmp_path):
    """注入 partial_write：文件真的只写一半，后置校验在真实流程里抓住它。

    这是 Step 3 的核心论点的端到端证据：
    **重试救不了静默失败，只有后置校验能。**
    """
    injector = FaultInjector([FaultRule(kind=FaultKind.PARTIAL_WRITE, tool="write_file")])
    recorder = TraceRecorder("t-partial")
    script = planner_react_script(
        tool_args={"filepath": "/home/ubuntu/half.txt", "content": "z" * 200}
    )
    sut, _ = _build_sut(tmp_path, script, recorder, injector=injector)

    result = await sut.run("写入一个 200 字符的文件")

    # 1.工具自己说成功（静默失败的定义）
    tool_span = next(s for s in recorder.spans if s.kind == SpanKind.TOOL)
    assert tool_span.status == "ok"
    # 2.但中间件记录了两件事：注入了什么故障、校验发现了什么
    assert tool_span.attrs["faults_injected"] == "partial_write"
    assert "不一致" in tool_span.attrs["postcondition_warning"]
    # 3.回归到任务级报告
    assert result.guard["postconditions"]["warnings"]
    assert result.guard["faults"]["injections"] == 1
    # 4.文件里确实只有一半 —— 故障是"真的"发生了，不是伪造的报告
    written = (tmp_path / "workspace" / "home" / "ubuntu" / "half.txt").read_text(encoding="utf-8")
    assert len(written) == 100


async def test_observe_mode_does_not_change_task_outcome(tmp_path):
    """observe 模式的**定义**：阈值越界不改变任务结果。

    用极低的预算阈值（token=1、工具=1）让所有维度都越界，
    然后断言任务依然成功 —— 这就是"只记录不干预"的验收。
    """
    recorder = TraceRecorder("t-observe")
    # 故意把所有阈值都设成"必定越界"（包括负数）——
    # 本条测试只关心"越界是否被记录、是否不干预"，不关心阈值本身是否合理。
    policy = BudgetPolicy(
        mode=BudgetMode.OBSERVE,
        max_tokens=1,
        max_cost_usd=1e-9,
        max_seconds=-1.0,
        max_steps=0,
        max_tool_calls=0,
    )
    sut, _ = _build_sut(tmp_path, planner_react_script(), recorder, budget_policy=policy)

    result = await sut.run("写文件")

    assert result.ok is True  # 没有因为超预算被砍掉
    budget = result.guard["budget"]
    assert budget["mode"] == "observe"
    # 5 个维度全部越界都被记下来了
    assert set(budget["violated_metrics"]) == {"tokens", "cost_usd", "elapsed_s", "steps", "tool_calls"}
    assert budget["would_stop"] is True  # 并且预演了"enforce 模式下本会中止"


async def test_llm_level_budget_check_is_wired(tmp_path):
    """预算必须在**每次 LLM 调用后**也检查。

    否则一个"只思考、不调工具"的失控 Agent 会完全绕过预算
    （token 和成本是在 LLM 调用时涨的，不是在工具调用时）。
    """
    recorder = TraceRecorder("t-llm-budget")
    policy = BudgetPolicy(max_tokens=1)
    sut, _ = _build_sut(tmp_path, planner_react_script(), recorder, budget_policy=policy)

    result = await sut.run("写文件")

    budget = result.guard["budget"]
    assert "tokens" in budget["violated_metrics"]
    # 检查次数必须明显多于工具调用次数（说明 LLM 调用后也在查）
    assert budget["checks"] > result.guard["loop"]["tool_calls"]
