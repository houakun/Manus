#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""trace 子系统的单元测试（recorder / store / view / LLM 代理）。"""

from __future__ import annotations

import json

import pytest

from lab.infra.counting_llm import CountingLLM
from lab.tests.fakes import ScriptedLLM, content
from lab.trace.span import Span, SpanKind, TraceRecorder
from lab.trace.store import SpanStore
from lab.trace.view import render_summary, render_tree, summarize
from lab.usage import Usage


# ==================== recorder：span 树结构 ====================

def test_task_span_is_root_and_open():
    """recorder 创建后应立即有一个 task 根 span，且处于打开状态。"""
    recorder = TraceRecorder("t1", sut_name="fake-sut", goal="do something")

    assert recorder.task_span.kind == SpanKind.TASK
    assert recorder.task_span.parent_id is None
    assert recorder.task_span.is_open is True
    assert recorder.task_span.name == "fake-sut"


def test_step_and_tool_nest_under_current_step():
    """核心不变量：llm / tool 挂在**当前 step** 下面，而不是平铺在 task 下。

    这决定了 trace 能不能回答"这一步花了多少 token / 调了哪些工具"。
    """
    recorder = TraceRecorder("t1")

    step = recorder.open_step("第一步")
    llm = recorder.begin_span("llm", "model")
    tool = recorder.open_tool("write_file", "file", {"filepath": "/a"})
    tool.end(success=True)
    llm.end(prompt_tokens=10)

    # 还在 step 内部
    assert llm.span.parent_id == step.span_id
    assert tool.span.parent_id == step.span_id

    step.end(success=True)

    # step 关闭后，后续 span 应该回到 task 层（规划阶段的 LLM 属于这一类）
    after = recorder.begin_span("llm", "model")
    after.end()
    assert after.span.parent_id == recorder.task_span.span_id


def test_llm_span_does_not_push_stack():
    """llm span 不压栈 —— 否则"决定调工具的 LLM"会变成"工具执行"的父节点，语义就反了。"""
    recorder = TraceRecorder("t1")

    llm = recorder.begin_span("llm", "model")
    llm.end()
    tool = recorder.open_tool("write_file")
    tool.end()

    assert tool.span.parent_id == recorder.task_span.span_id  # 而不是 llm.span_id


def test_nested_tool_calls_close_out_of_order_safely():
    """栈可能乱序结束（异常路径），必须保证不会把剩下的 span 弄丢或搞乱父节点。"""
    recorder = TraceRecorder("t1")
    outer = recorder.open_tool("outer")
    inner = recorder.open_tool("inner")
    assert inner.span.parent_id == outer.span_id

    # 先关外层（模拟异常冒泡）
    outer.end(status="error", error="boom")
    inner.end()

    assert outer.span.status == "error"
    assert len(recorder.spans) == 3  # task + outer + inner，一个都不能少


def test_duration_is_measured_and_monotonic():
    recorder = TraceRecorder("t1")
    handle = recorder.begin_span("llm", "m")
    handle.end()

    span = handle.span
    assert span.duration_ms is not None
    assert span.duration_ms >= 0
    assert span.end_ms >= span.start_ms


def test_end_is_idempotent():
    """重复 end 不能把时间戳覆盖掉（否则超时重试路径会算出错的耗时）。"""
    recorder = TraceRecorder("t1")
    handle = recorder.begin_span("llm", "m")
    first = handle.end()
    second = handle.end()

    assert first.end_ms == second.end_ms
    assert first.duration_ms == second.duration_ms


def test_close_open_spans_marks_leftovers_as_error():
    """超时/异常跳出的残留 span 必须被标成 error 并补上耗时。

    这是"崩溃时的 trace 是唯一一手材料"这个论断的技术保障：
    duration=None 的幽灵 span 会让树看起来像断了。
    """
    recorder = TraceRecorder("t1")
    step = recorder.open_step("卡住的一步")
    recorder.begin_span("llm", "m").end()

    recorder.finish_task(ok=False, error="task_timeout", error_type="task_timeout")

    assert step.span.status == "error"
    assert step.span.duration_ms is not None
    assert recorder.task_span.status == "error"
    assert recorder.task_span.attrs["error_type"] == "task_timeout"


def test_tool_args_are_truncated():
    """工具参数要截断：trace 是给人看的，不能塞进整篇文件内容。"""
    recorder = TraceRecorder("t1")
    handle = recorder.open_tool("write_file", "file", {"filepath": "/a.txt", "content": "x" * 5000})
    handle.end()

    recorded = handle.span.attrs["args"]["content"]
    assert "截断" in recorded
    assert len(recorded) < 600


# ==================== LLM 代理：span + 用量采集 ====================

async def test_counting_llm_reports_usage_and_span():
    """代理要同时做三件事：转发、累计用量、产出 llm span。"""
    recorder = TraceRecorder("t1")
    usage = Usage()
    llm = CountingLLM(ScriptedLLM([content("hi")]), usage, sink=recorder)

    await llm.invoke(messages=[{"role": "user", "content": "hello"}])

    # 1.用量累计
    assert usage.llm_calls == 1
    assert usage.prompt_tokens == 100
    assert usage.total_tokens == 120

    # 2.span 属性
    llm_spans = [s for s in recorder.spans if s.kind == SpanKind.LLM]
    assert len(llm_spans) == 1
    attrs = llm_spans[0].attrs
    assert attrs["prompt_tokens"] == 100
    assert attrs["model"] == "fake-model"
    assert attrs["messages"] == 1
    assert attrs["context_chars"] > 0
    assert len(attrs["prompt_digest"]) == 12


async def test_private_telemetry_keys_are_stripped():
    """采集后必须把 `_usage` 等私有键摘掉 —— 否则会渗进 SUT 的记忆，白烧 token。"""
    llm = CountingLLM(ScriptedLLM([content("hi")]), Usage())

    result = await llm.invoke(messages=[{"role": "user", "content": "x"}])

    assert "_usage" not in result
    assert "_latency_ms" not in result
    assert "_model" not in result
    assert result["content"] == "hi"


async def test_prompt_digest_identifies_version_while_context_digest_tracks_context():
    """两个指纹的分工：

    - prompt_digest（system prompt + 工具描述）在同一版本下必须**稳定**，
      否则它就当不了体检第 9 项要的"prompt 版本标识"（踩过这个坑：
      一开始把全量 messages 算进去，结果每次调用指纹都变，完全没法用来比对版本）；
    - context_digest（全量 messages）必须随上下文变化，用来定位"这次到底喂了什么"。
    """
    recorder = TraceRecorder("t1")
    tools = [{"type": "function", "function": {"name": "write_file", "description": "写文件"}}]
    system = {"role": "system", "content": "你是助手"}
    fake = ScriptedLLM([content("a"), content("b")])
    llm = CountingLLM(fake, Usage(), sink=recorder)

    await llm.invoke(messages=[system, {"role": "user", "content": "第一句"}], tools=tools)
    await llm.invoke(messages=[system, {"role": "user", "content": "第二句"}], tools=tools)

    spans = [s for s in recorder.spans if s.kind == SpanKind.LLM]
    assert spans[0].attrs["prompt_digest"] == spans[1].attrs["prompt_digest"]  # 版本未变 → 稳定
    assert spans[0].attrs["context_digest"] != spans[1].attrs["context_digest"]  # 上下文变了 → 必变

    # 换掉工具描述（即"改了工具集版本"）→ prompt 指纹必须变
    recorder2 = TraceRecorder("t2")
    llm2 = CountingLLM(ScriptedLLM([content("a")]), Usage(), sink=recorder2)
    await llm2.invoke(
        messages=[system, {"role": "user", "content": "第一句"}],
        tools=[{"type": "function", "function": {"name": "write_file", "description": "改过的描述"}}],
    )
    assert recorder2.spans[1].attrs["prompt_digest"] != spans[0].attrs["prompt_digest"]


async def test_failed_llm_call_marks_span_error_and_counts():
    """LLM 失败要同时体现在用量计数和 span 状态上（重试率是核心可靠性指标）。"""
    recorder = TraceRecorder("t1")
    usage = Usage()
    llm = CountingLLM(ScriptedLLM([RuntimeError("boom")]), usage, sink=recorder)

    with pytest.raises(RuntimeError):
        await llm.invoke(messages=[{"role": "user", "content": "x"}])

    assert usage.llm_errors == 1
    span = [s for s in recorder.spans if s.kind == SpanKind.LLM][0]
    assert span.status == "error"
    assert "boom" in (span.error or "")


# ==================== store：落盘与统计 ====================

def _fake_result(task_id: str, spans):
    """构造一个最小可用的 TaskResult（store 只读属性，不依赖 SUT）。"""
    from lab.sut.base import StepStats, TaskResult

    usage = Usage()
    usage.add_llm_call({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}, 15)
    return TaskResult(
        task_id=task_id,
        sut_name="fake-sut",
        ok=True,
        answer="ok",
        plan_title="标题",
        steps=StepStats(total=1, done=1, succeeded=1),
        tool_sequence=["write_file"],
        llm_usage=usage,
        cost_usd=0.0001,
        elapsed_ms=123,
        workspace="/tmp/ws",
    )


def test_store_roundtrip_preserves_tree(tmp_path):
    """落盘再读回，父子关系与属性必须完全一致（trace 的价值全在因果结构上）。"""
    recorder = TraceRecorder("t-round", sut_name="fake-sut")
    step = recorder.open_step("一步")
    llm = recorder.begin_span("llm", "m", attrs={"prompt_digest": "abc123"})
    llm.end(prompt_tokens=100)
    step.end(success=True)
    recorder.finish_task(ok=True)

    store = SpanStore(tmp_path / "traces.db")
    result = _fake_result("t-round", recorder.spans)
    store.save_run(result, recorder.spans, goal="测试目标")

    loaded = store.load_spans("t-round")
    assert len(loaded) == len(recorder.spans)

    by_id = {s.span_id: s for s in loaded}
    loaded_step = by_id[step.span_id]
    loaded_llm = by_id[llm.span_id]
    assert loaded_llm.parent_id == loaded_step.span_id
    assert loaded_llm.attrs["prompt_digest"] == "abc123"
    assert loaded_step.parent_id == recorder.task_span.span_id

    task = store.load_task("t-round")
    assert task["goal"] == "测试目标"
    assert task["ok"] == 1
    assert json.loads(task["tool_sequence"]) == ["write_file"]


def test_store_save_is_idempotent(tmp_path):
    """同一个 task_id 重复写不能产生重复行（重跑 / 补写 trace 的场景）。"""
    store = SpanStore(tmp_path / "traces.db")
    recorder = TraceRecorder("t-dup", sut_name="fake-sut")
    recorder.finish_task(ok=True)
    result = _fake_result("t-dup", recorder.spans)

    store.save_run(result, recorder.spans)
    store.save_run(result, recorder.spans)

    assert len(store.load_spans("t-dup")) == len(recorder.spans)
    assert store.stats()["runs"] == 1


def test_store_stats_aggregates_across_runs(tmp_path):
    """跨运行聚合：成功率 / tokens / 成本 / P95 耗时 / 失败归因。"""
    store = SpanStore(tmp_path / "traces.db")

    for index in range(4):
        recorder = TraceRecorder(f"t{index}", sut_name="fake-sut")
        recorder.finish_task(ok=index < 3)  # 3 成功 1 失败
        result = _fake_result(f"t{index}", recorder.spans)
        result.ok = index < 3
        result.error_type = None if result.ok else "task_timeout"
        result.elapsed_ms = 100 * (index + 1)
        store.save_run(result, recorder.spans)

    stats = store.stats(sut_name="fake-sut")
    assert stats["runs"] == 4
    assert stats["ok_runs"] == 3
    assert stats["success_rate"] == pytest.approx(0.75)
    assert stats["avg_tokens"] == 120
    assert stats["p95_elapsed_ms"] > 0
    assert {item["error_type"] for item in stats["by_error_type"]} == {"task_timeout", "(success)"}


def test_store_latest_task_id(tmp_path):
    store = SpanStore(tmp_path / "traces.db")
    for index in range(3):
        recorder = TraceRecorder(f"t{index}", sut_name="fake-sut")
        recorder.finish_task(ok=True)
        store.save_run(_fake_result(f"t{index}", recorder.spans), recorder.spans)

    assert store.latest_task_id() in {"t0", "t1", "t2"}
    assert len(store.list_tasks(limit=2)) == 2


def test_resolve_task_id_short_prefix(tmp_path):
    """短 id 只在**唯一匹配**时解析成功，歧义时宁可报错不猜。"""
    store = SpanStore(tmp_path / "traces.db")
    for task_id in ("aaaa-1111", "aabb-2222", "bbbb-3333"):
        recorder = TraceRecorder(task_id, sut_name="fake-sut")
        recorder.finish_task(ok=True)
        store.save_run(_fake_result(task_id, recorder.spans), recorder.spans)

    assert store.resolve_task_id("aaaa") == "aaaa-1111"
    assert store.resolve_task_id("bbbb") == "bbbb-3333"
    assert store.resolve_task_id("aa") is None  # 歧义 → 不猜
    assert store.resolve_task_id("zzzz") is None  # 不存在


# ==================== view：文本渲染 ====================

def test_render_tree_shows_hierarchy_and_attrs():
    recorder = TraceRecorder("t1", sut_name="fake-sut")
    step = recorder.open_step("写文件")
    tool = recorder.open_tool("write_file", "file", {"filepath": "/a.txt"})
    tool.end(success=True, attempts=2)
    step.end(success=True)
    recorder.finish_task(ok=True)

    text = render_tree(recorder.spans)

    assert "[task]" in text
    assert "[step]" in text
    assert "[tool]" in text
    assert "write_file" in text
    assert "attempts=2" in text  # D2 类问题靠这个字段被发现


def test_render_tree_handles_empty_and_broken_input():
    """渲染层不能因为数据异常就崩 —— 它是排障时唯一的窗口。"""
    assert render_tree([]) == "(空轨迹)"

    orphan = Span(task_id="t", parent_id="不存在", name="孤儿", kind=SpanKind.TOOL)
    assert "孤儿" in render_tree([orphan])


def test_summarize_reports_errors_and_slowest():
    recorder = TraceRecorder("t1")
    fast = recorder.begin_span("llm", "fast")
    fast.end(prompt_tokens=1)
    slow = recorder.begin_span("llm", "slow")
    slow.end(status="error", error="timeout", error_type="timeout")
    slow.span.duration_ms = 9999

    data = summarize(recorder.spans)

    assert data["by_kind"]["llm"]["count"] == 2
    assert data["by_kind"]["llm"]["errors"] == 1
    assert data["slowest"][0]["name"] == "slow"
    assert data["error_spans"][0]["error_type"] == "timeout"

    text = render_summary(recorder.spans)
    assert "汇总" in text
    assert "不可相加" in text  # 提醒读者别把父 span 耗时加总，避免误读指标
