#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SUT 健壮性测试：结构化输出不符合约定时，**不能崩、不能虚报成功**。

== 这个文件对应 Step 4 发现的三次真实故障（同一根因）==
| 编号 | 触发条件 | 修复前的后果 |
|---|---|---|
| D5  | `Plan` 为 `None` | AttributeError，整条流崩 |
| D9  | `plan.steps` 为空 | **虚报成功**（`ok=True`、一个工具都没调） |
| D10 | `Step.model_validate` 失败 | ValidationError，整条流崩（实测发生在 `sem_json_to_csv`）|

根因是同一个：**提示词要求\"必须返回严格 JSON\"，但代码没把\"违反约定\"当可预期情况处理**。

修法也是统一的：把解析失败变成\"把错误 + 期望结构回灌给模型重试一次\"，
再失败才 yield `ErrorEvent`。所以这三个用例共用一套\"脚本化 LLM 返回坏格式\"的手法。

== 为什么这些用例必须存在 ==
它们是**唯一能证明修复生效**的东西。如果只改代码不写测试，下次有人重构
`invoke_structured` 时把重试逻辑丢了，谁也不会发现 —— 直到线上的模型又返回一次数组。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.app_config import AgentConfig  # noqa: E402

from lab.infra.counting_llm import CountingLLM  # noqa: E402
from lab.infra.local_sandbox import LocalSandbox  # noqa: E402
from lab.infra.nulls import NullBrowser, NullSearchEngine  # noqa: E402
from lab.sut.manus_adapter import ManusSUT  # noqa: E402
from lab.tests.fakes import ScriptedLLM, content, json_content, tool_call  # noqa: E402
from lab.trace.span import TraceRecorder  # noqa: E402
from lab.usage import Usage  # noqa: E402

VALID_STEP_RESULT = {"success": True, "attachments": [], "result": "已完成"}
VALID_SUMMARY = {"message": "任务完成", "attachments": []}


def _build(tmp_path, recorder, script: List[Any]) -> Tuple[ManusSUT, ScriptedLLM]:
    """构造 SUT 并**把假 LLM 暴露出来**，以便断言它收到了纠错提示。"""
    usage = Usage()
    fake = ScriptedLLM(script)
    llm = CountingLLM(fake, usage, sink=recorder, after_call=None)
    workspace = tmp_path / "ws"
    sut = ManusSUT(
        llm=llm,
        agent_config=AgentConfig(max_iterations=8, max_retries=2, max_search_results=3),
        sandbox=LocalSandbox(workspace),
        workspace=workspace,
        fast_mode=True,
        browser=NullBrowser(),
        search_engine=NullSearchEngine(),
        usage=usage,
        max_seconds=30,
        trace=recorder,
    )
    return sut, fake


def _valid_plan(steps: List[Dict[str, str]]) -> Dict[str, Any]:
    return {"title": "测试任务", "goal": "测试目标", "language": "中文",
            "steps": steps, "message": "开始执行"}


# ==================== D10：格式不符 → 纠错重试 → 成功 ====================

async def test_malformed_structured_output_is_retried_and_recovers(tmp_path):
    """模型第一次返回了数组（实测中的真实故障形态），纠错重试后应成功完成。

    这同时证明了：修复不是"把崩溃改成失败"，而是**真的把任务救回来了**。
    """
    recorder = TraceRecorder("t-d10-retry")
    script = [
        # 1) create_plan 第一次：返回一个数组 —— 正是 sem_json_to_csv 崩溃的那种形态
        content('[{"name": "monitor", "price": 320, "stock": 4}]'),
        # 2) 纠错后重试：返回合法 Plan
        json_content(_valid_plan([{"description": "写文件"}])),
        # 3) react：调工具
        tool_call("write_file", {"filepath": "/home/ubuntu/a.txt", "content": "hello"}),
        # 4) react：结构化结果
        json_content(VALID_STEP_RESULT),
        # 5) update_plan
        json_content({"steps": []}),
        # 6) summarize
        json_content(VALID_SUMMARY),
    ]
    sut, fake = _build(tmp_path, recorder, script)

    result = await sut.run("把 hello 写入 /home/ubuntu/a.txt")

    # 1.任务最终成功（而不是"不崩但失败"）
    assert result.ok is True
    assert result.error is None
    assert result.error_type is None

    # 2.确实发生了纠错重试：第 2 次调用的 messages 里带上了纠错提示与期望结构
    assert len(fake.calls) >= 2
    second_call_messages = fake.calls[1]["messages"]
    correction = "\n".join(
        str(m.get("content") or "") for m in second_call_messages if m.get("role") == "user"
    )
    assert "格式不符合约定" in correction
    # 期望结构必须真的在提示里（否则模型没法知道该改成什么形状）
    assert "title" in correction and "steps" in correction
    # 上一条错误的助手回复要留在上下文里，模型才知道"我上次错在哪"
    assert any(m.get("role") == "assistant" for m in second_call_messages)


async def test_correction_prompt_lists_required_fields(tmp_path):
    """纠错提示里要列出字段与是否必填 —— 这是模型能一次改对的前提。

    这里直接测 `BaseAgent` 的两个纯函数（不跑 flow），因为它们就是纠错提示的全部内容来源。
    """
    from app.domain.models.message import Message
    from app.domain.models.plan import Plan
    from app.domain.services.agents.base import BaseAgent
    from app.infrastructure.external.json_parser.repair_json_parser import RepairJSONParser

    agent = BaseAgent(
        uow_factory=lambda: None, session_id="s",
        agent_config=AgentConfig(max_iterations=4, max_retries=2, max_search_results=3),
        llm=ScriptedLLM([]), json_parser=RepairJSONParser(), tools=[],
    )

    plan_schema = agent._expected_schema(Plan)
    assert "title" in plan_schema and "steps" in plan_schema
    assert "message" in agent._expected_schema(Message)

    # 解析失败时的错误说明必须**同时**包含"错在哪"与"期望什么"。
    #   注意：JSON 修复解析器（json_repair）很宽容，极少抛异常 ——
    #   即使传"这不是 JSON"它也会返回一个字符串，于是大部分失败会落到
    #   ValidationError 分支（"你返回的是 str，期望一个对象"），而不是"不是合法 JSON"分支。
    #   两条分支都保留：前者是主路径，后者只作为防御（换了解析器时才有用）。
    parsed, error = await agent._parse_structured("[]", Plan)
    assert parsed is None
    assert "list" in error  # 告诉模型它返回的是数组（形状不对）

    parsed, error = await agent._parse_structured('{"steps": "不是数组"}', Plan)
    assert parsed is None
    assert "steps" in error  # 指出具体是哪个字段的问题

    for bad in ("这不是 JSON", "", "null"):
        parsed, error = await agent._parse_structured(bad, Plan)
        assert parsed is None, f"{bad!r} 不应被解析成有效 Plan"
        assert error, f"{bad!r} 必须给出可读的失败说明"


# ==================== 坏格式持续出现 → 结构化错误，而不是崩溃 ====================

async def test_persistent_malformed_output_yields_error_event_not_crash(tmp_path):
    """连续两次格式不符 → `ErrorEvent`（而不是 ValidationError 把流炸掉）。

    断言 `error_type != "sut_crash"` 是关键：区分"Agent 没做对"和"代码崩了"。
    """
    recorder = TraceRecorder("t-d10-fail")
    # 三次都返回数组：invoke_structured 只重试一次，多余的脚本项不会用到
    script = [content("[]"), content("[]"), content("[]")]
    sut, fake = _build(tmp_path, recorder, script)

    result = await sut.run("随便什么任务")

    assert result.ok is False
    assert result.error_type == "agent_error"  # 不是 sut_crash
    assert "不符合约定格式" in (result.error or "")
    # 只试了 2 次（1 次原始 + 1 次纠错），不能无限重试烧 token
    assert len(fake.calls) == 2


async def test_tool_events_are_still_forwarded_during_structured_call(tmp_path):
    """改造成结构化调用后，工具事件仍要正常透出（否则轨迹和 UI 就断了）。

    这是一个容易被重构破坏的性质：中间事件必须在 `invoke_structured` 里原样 yield。
    """
    recorder = TraceRecorder("t-forward")
    script = [
        json_content(_valid_plan([{"description": "写文件"}])),
        tool_call("write_file", {"filepath": "/home/ubuntu/b.txt", "content": "x"}),
        json_content(VALID_STEP_RESULT),
        json_content({"steps": []}),
        json_content(VALID_SUMMARY),
    ]
    sut, _ = _build(tmp_path, recorder, script)

    result = await sut.run("写文件")

    assert result.ok is True
    assert result.tool_sequence == ["write_file"]
    tool_spans = [s for s in recorder.spans if s.kind.value == "tool"]
    assert len(tool_spans) == 1


# ==================== D9：空计划不能虚报成功 ====================

async def test_empty_plan_is_reported_as_error_not_success(tmp_path):
    """D9 的验收：计划里 0 个步骤 → 必须是失败，不能是 `ok=True`。

    修复前：`if not self.plan or len(self.plan.steps) == 0` 直接把状态置成 COMPLETED，
    流跑完 `TaskResult` 是 `ok=True / error=None`，而实际什么都没做。
    只有外部判定器靠"产物不存在"才能发现 —— 所以 SUT 自述成功率会虚高。
    """
    recorder = TraceRecorder("t-d9")
    script = [json_content(_valid_plan([]))]  # 合法 JSON，但 steps 是空数组
    sut, _ = _build(tmp_path, recorder, script)

    result = await sut.run("一个什么都没做的任务")

    assert result.ok is False, "空计划被当成了任务完成（虚报成功）"
    assert result.error_type == "agent_error"
    assert "Plan 为空" in (result.error or "")
    assert result.steps.total == 0
    assert result.tool_sequence == []


async def test_empty_plan_does_not_duplicate_error_events(tmp_path):
    """空计划只应报**一个**错误，不能既在 PLANNING 又在 COMPLETED 各报一次。

    为什么要测：重复报错会让错误链出现两条同样的条目，
    归因统计（按 error_type 计数）就会被重复计数，指标失真。
    """
    recorder = TraceRecorder("t-d9-dup")
    script = [json_content(_valid_plan([]))]
    sut, _ = _build(tmp_path, recorder, script)

    result = await sut.run("空计划任务")

    assert len(result.error_chain) == 1
    # task span 上记录的链长也要一致
    assert recorder.task_span.attrs.get("error_chain_len") == 1


# ==================== 工具层兜底：不存在的工具名 / 坏参数 ====================

@pytest.fixture()
def no_sleep(monkeypatch):
    """把重试间隔压到 0：测试不需要真等 1 秒。"""
    from app.domain.services.agents.base import BaseAgent

    monkeypatch.setattr(BaseAgent, "_retry_interval", 0.01)


async def test_unknown_tool_name_is_fed_back_not_crashed(tmp_path, no_sleep):
    """模型幻觉出一个不存在的工具名 → 不能崩，要把失败回灌让它自己改。

    原实现里 `self._get_tool(function_name)` 是裸调用，抛 ValueError 会穿透整个 flow，
    直接表现为 `sut_crash` —— 而幻觉工具名比"返回数组"常见得多。
    """
    recorder = TraceRecorder("t-ghost-tool")
    script = [
        json_content(_valid_plan([{"description": "干点活"}])),
        tool_call("ghost_tool", {"a": 1}),  # 工具清单里没有这个
        json_content(VALID_STEP_RESULT),  # 看到失败后模型改回了正常回复
        json_content({"steps": []}),
        json_content(VALID_SUMMARY),
    ]
    sut, fake = _build(tmp_path, recorder, script)

    result = await sut.run("一个任务")

    # 1.任务没有被这次无效调用拖崩
    assert result.ok is True, result.error
    assert result.error_type is None

    # 2.失败被回灌给模型（否则它无法知道自己写错了工具名）
    tool_messages = [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages, "无效的工具调用没有配上 tool 消息 —— 下一次请求会被服务端拒绝"
    # 断言具体信号而不是文案：工具名 + 错误类型都要回灌给模型，
    # 它才能知道“哪个名字写错了、错在哪一类”
    assert "ghost_tool" in tool_messages[-1]["content"]
    assert "tool_not_found" in tool_messages[-1]["content"]

    # 3.轨迹里能看见这次无效调用及其失败类型
    failed_tools = [
        s for s in recorder.spans
        if s.kind.value == "tool" and s.attrs.get("error_type") == "tool_not_found"
    ]
    assert failed_tools


class _ExplodingParser:
    """参数里带 EXPLODE 就抛异常，用来模拟"工具参数不是合法 JSON"。

    为什么不直接发一段坏 JSON：SUT 用的是 json_repair，它**极其宽容**，
    常规坏 JSON 它不抛异常（会尽力修）。要稳稳地测到这条分支，
    必须让解析器真的抛 —— 这也是这个分支更适合单元测试的原因。
    """

    async def invoke(self, text, default_value=None):
        if "EXPLODE" in str(text):
            raise ValueError("模拟：工具参数不是合法 JSON")
        from app.infrastructure.external.json_parser.repair_json_parser import RepairJSONParser

        return await RepairJSONParser().invoke(text, default_value)


async def test_malformed_tool_arguments_are_fed_back():
    """工具参数解析失败 → 不崩，产出 `invalid_arguments` 结果并回灌给模型。"""
    from app.domain.models.event import ToolEvent, ToolEventStatus
    from app.domain.models.session import Session
    from app.domain.services.agents.base import BaseAgent

    from lab.infra.memory_uow import create_uow_factory

    uow_factory = create_uow_factory()
    async with uow_factory() as uow:
        await uow.session.save(Session(id="s-malformed"))

    bad_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-x",
            "type": "function",
            "function": {"name": "write_file", "arguments": "EXPLODE"},
        }],
    }
    fake = ScriptedLLM([bad_call, content("我看到参数解析失败了，已换一种写法。")])
    agent = BaseAgent(
        uow_factory=uow_factory, session_id="s-malformed",
        agent_config=AgentConfig(max_iterations=4, max_retries=2, max_search_results=3),
        llm=fake, json_parser=_ExplodingParser(), tools=[],
    )

    events = [event async for event in agent.invoke("随便干点什么")]

    # 1.没有异常往上冒，事件流照常产出
    assert events
    # 2.产出了一对 CALLING/CALLED 事件，且结果是 invalid_arguments
    called = [e for e in events if isinstance(e, ToolEvent) and e.status == ToolEventStatus.CALLED]
    assert called and called[0].function_result.error_type == "invalid_arguments"
    # 3.失败被回灌给模型
    tool_messages = [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and "合法 JSON" in tool_messages[-1]["content"]


# ==================== 收尾兜底：活干完了但总结失败 ====================

async def test_summarize_failure_falls_back_to_delivered_results(tmp_path, no_sleep):
    """汇总结论生成失败、但步骤已完成 → 用兜底总结交付，**不应报成失败**。

    实测 4/40 次运行属于这种情况（网络抖动导致汇总 LLM 调用耗尽重试），
    它们被判失败纯粹是因为"最后一步没写好总结"。
    """
    recorder = TraceRecorder("t-summary-fallback")
    script = [
        json_content(_valid_plan([{"description": "写文件"}])),
        tool_call("write_file", {"filepath": "/home/ubuntu/a.txt", "content": "hello"}),
        json_content({"success": True, "attachments": [], "result": "已写入 a.txt"}),
        json_content({"steps": []}),
        RuntimeError("汇总阶段网络抖动"),
        RuntimeError("汇总阶段网络抖动"),
    ]
    sut, _ = _build(tmp_path, recorder, script)

    result = await sut.run("把 hello 写入 /home/ubuntu/a.txt")

    assert result.ok is True, f"活干完了却被报成失败：{result.error}"
    assert "最后一步失败" in result.answer  # 兜底总结里说明了这件事
    assert "已写入 a.txt" in result.answer  # 且真的交付了步骤结果
    assert (tmp_path / "ws" / "home" / "ubuntu" / "a.txt").read_text() == "hello"


async def test_summarize_failure_without_results_still_reports_error(tmp_path, no_sleep):
    """没有任何可用结果时，收尾失败必须**如实上报**。

    兜底的是"活干完了但收尾失败"，不是"把失败包装成成功"。
    没有这个反向用例，上面的兜底就变成了一个"永远报成功"的后门。
    """
    recorder = TraceRecorder("t-summary-honest")
    script = [
        json_content(_valid_plan([{"description": "什么都不做的步骤"}])),
        tool_call("write_file", {"filepath": "/home/ubuntu/b.txt", "content": "x"}),
        # 步骤报告成功但 result 为空 → 没有可交付的内容
        json_content({"success": True, "attachments": [], "result": ""}),
        json_content({"steps": []}),
        RuntimeError("汇总阶段网络抖动"),
        RuntimeError("汇总阶段网络抖动"),
    ]
    sut, _ = _build(tmp_path, recorder, script)

    result = await sut.run("一个没有产出的任务")

    assert result.ok is False
    assert result.error_type == "agent_error"
