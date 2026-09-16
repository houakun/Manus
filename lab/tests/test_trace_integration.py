#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端集成测试：用脚本化 LLM 跑通完整 flow，验证 trace 树的结构正确性。

== 这个文件为什么重要 ==
它是"零 SUT 改动拿到 span 树"这个设计主张的**唯一硬证据**：
不调用任何真实模型（不需要 API Key、零成本、确定性），
但是真真切切跑了 SUT 的 PlannerReActFlow 全部状态迁移，
然后断言 span 树的父子关系符合预期。

如果哪天有人改了 SUT 的事件顺序或 lab 的压栈逻辑，这里会立刻挂 ——
这正是我们想要的：**把"trace 结构"变成受保护的契约**。
"""

from __future__ import annotations

import pytest

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.app_config import AgentConfig  # noqa: E402

from lab.infra.counting_llm import CountingLLM  # noqa: E402
from lab.infra.local_sandbox import LocalSandbox  # noqa: E402
from lab.infra.nulls import NullBrowser, NullSearchEngine  # noqa: E402
from lab.infra.memory_uow import create_uow_factory  # noqa: E402
from lab.sut.manus_adapter import ManusSUT  # noqa: E402
from lab.tests.fakes import ScriptedLLM, planner_react_script  # noqa: E402
from lab.trace.span import SpanKind, TraceRecorder  # noqa: E402
from lab.trace.store import SpanStore  # noqa: E402
from lab.trace.view import render_tree  # noqa: E402
from lab.usage import Usage  # noqa: E402


def _build_sut(tmp_path, script, recorder, *, injector=None, budget_policy=None):
    """组装一个 fast mode 的 SUT（全部依赖都是 lab 侧替身）。"""
    from lab.faults.injector import FaultInjector
    from lab.guard.budget import Budget, BudgetPolicy
    from lab.middleware import ToolGuard

    usage = Usage()
    workspace = tmp_path / "workspace"
    sandbox = LocalSandbox(workspace)
    budget = Budget(usage, model_name="deepseek-chat", policy=budget_policy or BudgetPolicy())
    guard = ToolGuard(recorder=recorder, budget=budget, injector=injector, sandbox=sandbox)
    llm = CountingLLM(ScriptedLLM(script), usage, sink=recorder, after_call=guard.check_budget)
    sut = ManusSUT(
        llm=llm,
        # 直接构造配置，避免测试依赖 api/config.yaml 的内容
        agent_config=AgentConfig(max_iterations=10, max_retries=2, max_search_results=3),
        sandbox=sandbox,
        workspace=workspace,
        fast_mode=True,
        browser=NullBrowser(),
        search_engine=NullSearchEngine(),
        usage=usage,
        max_seconds=30,
        trace=recorder,
        guard=guard,
    )
    return sut, usage


async def test_full_flow_produces_well_formed_span_tree(tmp_path):
    """一次完整任务应产出 task -> step -> {llm, tool} 的正确嵌套。"""
    recorder = TraceRecorder("t-integration", goal="写个文件")
    sut, usage = _build_sut(tmp_path, planner_react_script(), recorder)

    result = await sut.run("把 hello 写入 /home/ubuntu/a.txt")

    # ---------- 1.任务结果本身 ----------
    assert result.ok is True
    assert result.error is None
    assert result.steps.total == 1
    assert result.steps.succeeded == 1
    assert result.tool_sequence == ["write_file"]
    assert result.plan_title == "测试任务"
    # task_id 必须来自 recorder，否则 span 表和任务表会对不上号
    assert result.task_id == "t-integration"
    assert result.llm_usage.llm_calls == 5  # 计划/执行x2/更新计划/汇总 = 5 次

    # ---------- 2.产物真的落盘了 ----------
    artifact = tmp_path / "workspace" / "home" / "ubuntu" / "a.txt"
    assert artifact.read_text(encoding="utf-8") == "hello"

    # ---------- 3.span 树结构 ----------
    spans = recorder.spans
    by_kind = {}
    for span in spans:
        by_kind.setdefault(span.kind, []).append(span)

    assert len(by_kind[SpanKind.TASK]) == 1
    assert len(by_kind[SpanKind.STEP]) == 1
    assert len(by_kind[SpanKind.TOOL]) == 1
    assert len(by_kind[SpanKind.LLM]) == 5

    task_span = by_kind[SpanKind.TASK][0]
    step_span = by_kind[SpanKind.STEP][0]
    tool_span = by_kind[SpanKind.TOOL][0]

    # step 挂在 task 下
    assert step_span.parent_id == task_span.span_id

    # 工具调用挂在 step 下（因为它发生在那一步内部）
    assert tool_span.parent_id == step_span.span_id
    assert tool_span.status == "ok"
    assert tool_span.attrs["success"] is True

    # **关键断言**：LLM 调用按"发生时机"被正确归位
    #  - 执行步骤内的 2 次（决定调工具 + 消化工具结果）→ 挂在 step 下
    #  - 开始规划 / 更新计划 / 最终汇总 3 次 → 挂在 task 下
    llm_in_step = [s for s in by_kind[SpanKind.LLM] if s.parent_id == step_span.span_id]
    llm_in_task = [s for s in by_kind[SpanKind.LLM] if s.parent_id == task_span.span_id]
    assert len(llm_in_step) == 2
    assert len(llm_in_task) == 3

    # 每次 LLM 调用都要有可用的计量与指纹
    for span in by_kind[SpanKind.LLM]:
        assert span.duration_ms is not None
        assert span.attrs["prompt_tokens"] == 100
        assert len(span.attrs["prompt_digest"]) == 12
        assert span.status == "ok"

    # 累计用量与 span 级用量必须对得上（口径一致性）
    assert sum(s.attrs["total_tokens"] for s in by_kind[SpanKind.LLM]) == usage.total_tokens

    # ---------- 4.文本渲染可用（无视觉通道下的唯一观测方式） ----------
    text = render_tree(spans)
    assert "[task]" in text and "[step]" in text and "[tool]" in text and "[llm]" in text
    assert "write_file" in text


async def test_span_tree_survives_store_roundtrip(tmp_path):
    """跑完 → 落盘 → 读回，树结构必须完好。"""
    recorder = TraceRecorder("t-roundtrip", goal="写个文件")
    sut, _ = _build_sut(tmp_path, planner_react_script(), recorder)
    result = await sut.run("写文件")

    store = SpanStore(tmp_path / "traces.db")
    store.save_run(result, recorder.spans, goal="写文件")

    loaded = store.load_spans("t-roundtrip")
    assert len(loaded) == len(recorder.spans)

    step = next(s for s in loaded if s.kind == SpanKind.STEP)
    tool = next(s for s in loaded if s.kind == SpanKind.TOOL)
    llm_in_step = [s for s in loaded if s.kind == SpanKind.LLM and s.parent_id == step.span_id]

    assert tool.parent_id == step.span_id
    assert len(llm_in_step) == 2
    assert all(s.duration_ms is not None for s in loaded)


async def test_react_llm_failure_is_reported_as_error_event(tmp_path):
    """S5 修复的验收：LLM 重试耗尽后必须是 ErrorEvent，而不是穿透异常。

    这条路径以前会抛 RuntimeError 把整条流炸掉（SSE 断流 / harness 崩溃）。
    """
    recorder = TraceRecorder("t-llm-fail")
    # 第 1 次调用（create_plan）就持续失败，重试 max_retries=2 次后耗尽
    script = [RuntimeError("mock 401 unauthorized"), RuntimeError("mock 401 unauthorized")]
    sut, _ = _build_sut(tmp_path, script, recorder)

    result = await sut.run("随便什么任务")

    assert result.ok is False
    assert result.error_type == "agent_error"
    # 根因必须是"LLM 调用失败"，而不是后面级联出来的"计划为空"
    assert "调用语言模型失败" in (result.error or "")
    # 完整错误链要保留下来，才能区分根因与后果
    assert len(result.error_chain) >= 2
    # 注意：D5 的原文是"任务计划(Plan 为空)"，不要想当然地按"计划为空"去搜
    assert any("Plan 为空" in item for item in result.error_chain)


async def test_multiple_tool_calls_in_one_step_all_nest(tmp_path):
    """一步内多次工具调用，每个都要成为独立的 tool span 并挂在该步下。"""
    from lab.tests.fakes import json_content, tool_call

    script = [
        json_content({
            "title": "两步工具",
            "goal": "g",
            "language": "中文",
            "steps": [{"description": "写两个文件"}],
            "message": "开始",
        }),
        tool_call("write_file", {"filepath": "/home/ubuntu/x.txt", "content": "1"}, "c1"),
        tool_call("write_file", {"filepath": "/home/ubuntu/y.txt", "content": "2"}, "c2"),
        json_content({"success": True, "attachments": [], "result": "都写好了"}),
        json_content({"steps": []}),
        json_content({"message": "完成", "attachments": []}),
    ]
    recorder = TraceRecorder("t-two-tools")
    sut, _ = _build_sut(tmp_path, script, recorder)

    result = await sut.run("写两个文件")

    assert result.ok is True
    assert result.tool_sequence == ["write_file", "write_file"]

    step = next(s for s in recorder.spans if s.kind == SpanKind.STEP)
    tools = [s for s in recorder.spans if s.kind == SpanKind.TOOL]
    assert len(tools) == 2
    assert all(t.parent_id == step.span_id for t in tools)
    assert all(t.status == "ok" for t in tools)
    # 两个文件确实都写出来了（用 tool_call_id 关联开始/结束，不会把 span 搞重）
    assert (tmp_path / "workspace" / "home" / "ubuntu" / "x.txt").exists()
    assert (tmp_path / "workspace" / "home" / "ubuntu" / "y.txt").exists()


async def test_failed_tool_records_error_type_in_span(tmp_path):
    """工具失败要在 span 上留下 error_type —— 这是失败归因的数据来源。"""
    from lab.tests.fakes import json_content, tool_call

    # read_file 一个不存在的文件 → LocalSandbox 返回 error_type=file_not_found
    script = [
        json_content({
            "title": "读不存在的文件", "goal": "g", "language": "中文",
            "steps": [{"description": "读取"}], "message": "开始",
        }),
        tool_call("read_file", {"filepath": "/home/ubuntu/missing.txt"}),
        json_content({"success": False, "attachments": [], "result": "文件不存在"}),
        json_content({"steps": []}),
        json_content({"message": "失败", "attachments": []}),
    ]
    recorder = TraceRecorder("t-tool-fail")
    sut, _ = _build_sut(tmp_path, script, recorder)

    await sut.run("读一个不存在的文件")

    tool_span = next(s for s in recorder.spans if s.kind == SpanKind.TOOL)
    assert tool_span.status == "error"
    assert tool_span.attrs["error_type"] == "file_not_found"
    assert tool_span.attrs["attempts"] == 1


# ==================== 组合根（api.run_task）的接线测试 ====================
def test_run_task_wires_trace_and_persists(tmp_path, monkeypatch):
    """验证 api.run_task 这个"组合根"把 recorder 正确串到了 LLM 代理、SUT 和存储上。

    用 monkeypatch 替换真实 LLM，因此不需要 API Key，也不会花钱。
    """
    from lab import api as lab_api
    from lab.tests.fakes import planner_react_script as script_factory

    monkeypatch.setenv("LAB_LLM_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(lab_api, "OpenAILLM", lambda config: ScriptedLLM(script_factory()))
    db_path = tmp_path / "traces.db"
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: SpanStore(db_path))

    result = lab_api.run_task_sync("写个文件", workspace=tmp_path / "ws")

    assert result.ok is True
    assert result.trace_path == str(db_path)
    assert result.cost_usd > 0  # 价格表命中 fake-model 之外的真实模型名

    store = SpanStore(db_path)
    spans = store.load_spans(result.task_id)
    assert len(spans) > 1
    stored = store.load_task(result.task_id)
    assert stored["ok"] == 1

    # 回归断言：落盘的值必须与返回的结果**一致**。
    # 这一条专门锁住"先落盘后补计量字段"的顺序 bug（会把 cost=0 写进库）。
    assert stored["cost_usd"] == pytest.approx(result.cost_usd)
    assert stored["cost_usd"] > 0
    assert stored["elapsed_ms"] == result.elapsed_ms
    assert stored["total_tokens"] == result.llm_usage.total_tokens


def test_run_task_survives_sut_crash(tmp_path, monkeypatch):
    """SUT 适配层抛异常时，harness 也必须给出结构化失败结果 + 留下 trace。

    这是"一次运行不能中断整个任务集评测"这条要求的验收。
    """
    from lab import api as lab_api

    monkeypatch.setenv("LAB_LLM_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(lab_api, "OpenAILLM", lambda config: ScriptedLLM([]))
    db_path = tmp_path / "traces.db"
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: SpanStore(db_path))

    # 脚本为空 → ScriptedLLM 抛 AssertionError → 被 _invoke_llm 捕获重试 → 最终 ErrorEvent
    result = lab_api.run_task_sync("任务", workspace=tmp_path / "ws")

    assert result.ok is False
    assert result.error_type in {"agent_error", "harness_error", "sut_crash"}
    # 关键：即使失败也要留下 trace
    assert result.trace_path == str(db_path)
    assert len(SpanStore(db_path).load_spans(result.task_id)) > 0
