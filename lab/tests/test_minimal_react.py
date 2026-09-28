#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自研最小 loop（`lab/sut/minimal_react.py`，T10）的回归测试。

这个文件守的是"**自写 SUT**"这个主张：
- 它是 `docs/step1-architecture-and-refactor-plan.md` 里"选项 B"的落地 ——
  被测对象不能只有课程作者那份控制流，否则"我的 Agent 有多稳"讲的是别人的代码；
- 而它的正确性不能靠真模型冒烟来守（要花钱、且不确定），所以这里用脚本化 LLM
  把它**钉成契约**：工具调用怎么解析、观察怎么回灌、何时终止、产物是否真落盘。
"""

from __future__ import annotations

import asyncio
import json

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.services.tools.file import FileTool

from lab.infra.local_sandbox import LocalSandbox
from lab.sut.minimal_react import MinimalReActSUT
from lab.tests.fakes import ScriptedLLM
from lab.usage import Usage


def _tool_call(name: str, args: dict) -> dict:
    """构造一条 OpenAI 风格的工具调用消息（与真实返回同构）。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


def test_minimal_react_writes_file_then_answers(tmp_path):
    """一次工具调用 + 最终答复：产物必须真的落在工作区里，且工具序列被记录。"""
    workspace = tmp_path / "ws"
    sandbox = LocalSandbox(workspace)
    script = [
        _tool_call("write_file", {"filepath": "/home/ubuntu/out.txt", "content": "42"}),
        {"role": "assistant", "content": "已写入 /home/ubuntu/out.txt"},
    ]
    sut = MinimalReActSUT(
        llm=ScriptedLLM(script),
        tools=[FileTool(sandbox=sandbox)],
        usage=Usage(),
        workspace=workspace,
        model_name="fake-model",
        max_steps=4,
    )

    result = asyncio.run(sut.run("把 42 写入 /home/ubuntu/out.txt"))

    assert result.ok is True, result.error
    assert result.tool_sequence == ["write_file"]
    assert result.sut_name == "minimal-react[fast]"
    assert (workspace / "home" / "ubuntu" / "out.txt").read_text(encoding="utf-8") == "42"


def test_minimal_react_unknown_tool_is_fed_back_not_raised(tmp_path):
    """工具不存在时**回灌文本**让模型自己纠正 —— 朴素 loop 的真实形态（不抛异常）。

    为什么钉住它：这是"基线 vs 加固"的对照里最容易被误解的一处 ——
    朴素 loop 不是"没有错误处理"，而是"把错误交给模型"。
    """
    workspace = tmp_path / "ws"
    scripts = [
        _tool_call("no_such_tool", {"x": 1}),
        {"role": "assistant", "content": "换个办法"},
    ]
    llm = ScriptedLLM(scripts)
    sut = MinimalReActSUT(
        llm=llm,
        tools=[FileTool(sandbox=LocalSandbox(workspace))],
        usage=Usage(),
        workspace=workspace,
        model_name="fake-model",
        max_steps=4,
    )

    result = asyncio.run(sut.run("随便做点什么"))

    assert result.ok is True
    # 第二次调用时，上一轮的观察必须已经进了 messages（含"没有名为 ... 的工具"）
    second_messages = llm.calls[1]["messages"]
    assert any("no_such_tool" in str(message.get("content") or "") for message in second_messages), second_messages


def test_minimal_react_stops_at_max_steps(tmp_path):
    """一直调工具不收敛时，必须**在步数上限处停下**并如实报告（而不是无限循环）。"""
    workspace = tmp_path / "ws"
    # 脚本给 5 条工具调用，而上限只有 2 步
    script = [_tool_call("read_file", {"filepath": "/home/ubuntu/nope.txt"}) for _ in range(5)]
    sut = MinimalReActSUT(
        llm=ScriptedLLM(script),
        tools=[FileTool(sandbox=LocalSandbox(workspace))],
        usage=Usage(),
        workspace=workspace,
        model_name="fake-model",
        max_steps=2,
    )

    result = asyncio.run(sut.run("读一个不存在的文件"))

    assert result.ok is False
    assert "最大步数" in (result.error or "")
    assert len(result.tool_sequence) == 2
