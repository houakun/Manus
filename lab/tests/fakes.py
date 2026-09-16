#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试替身：脚本化 LLM 与构造器小工具。

== 为什么必须有它（这是 Step 4 harness 的基础设施）==
用真模型测"埋点是否正确"，等于把两件不相干的事耦在一起：
埋点错了、模型今天走了另一条路径，你无法区分。所以：
- **确定性**：同样的脚本 → 同样的工具序列 → 可以精确断言 span 树结构；
- **零成本**：trace 相关的回归测试可以随便跑；
- **高保真到接口层**：脚本化 LLM 实现的是 LLM 协议（含 S2 改造后透出的 `_usage`），
  所以它同时也在**回归验证"私有遥测键"的契约**——如果哪天 OpenAILLM 不再透出 usage，
  这里会先挂（而不是等到线上发现成本统计全是 0）。

Step 4 会在此基础上加"故障注入型 LLM"（超时/429/截断），所以这里刻意把
"脚本项可以是异常"设计进去了：脚本里放一个 Exception 实例就代表这次调用失败。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def content(text: str) -> Dict[str, Any]:
    """构造一条纯文本 assistant 响应（用于 planner / summarize 这类 JSON 输出）。"""
    return {"role": "assistant", "content": text}


def json_content(payload: Dict[str, Any]) -> Dict[str, Any]:
    """构造一条内容是 JSON 字符串的 assistant 响应。"""
    return content(json.dumps(payload, ensure_ascii=False))


def tool_call(name: str, args: Dict[str, Any], call_id: str = "call-1") -> Dict[str, Any]:
    """构造一条工具调用响应。

    注意 arguments 必须是**字符串**：这与 OpenAI 兼容接口的真实返回一致，
    SUT 的 BaseAgent 会用 JSON 解析器去解析它。
    """
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


class ScriptedLLM:
    """按脚本顺序返回预设响应的假 LLM。"""

    def __init__(
            self,
            script: List[Any],
            *,
            model_name: str = "fake-model",
            prompt_tokens: int = 100,
            completion_tokens: int = 20,
            latency_ms: int = 12,
    ) -> None:
        self._script = list(script)
        self._model_name = model_name
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._latency_ms = latency_ms
        # 记录每次调用的入参，供断言"prompt 指纹是否随提示词变化而变化"
        self.calls: List[Dict[str, Any]] = []

    async def invoke(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "response_format": response_format,
            "tool_choice": tool_choice,
        })

        if not self._script:
            raise AssertionError(
                f"脚本已耗尽：第 {len(self.calls)} 次调用没有预设响应。"
                "请检查 SUT 是否多调了一次 LLM（这本身就是个有价值的发现）。"
            )

        item = self._script.pop(0)
        if isinstance(item, BaseException):
            # 脚本里放异常 = 模拟这次 LLM 调用失败（Step 4 故障注入的接口约定）
            raise item

        result = dict(item)
        # 模拟 S2 改造后的私有遥测键：契约一旦被破坏，这里的断言会挂
        result["_usage"] = {
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._prompt_tokens + self._completion_tokens,
        }
        result["_latency_ms"] = self._latency_ms
        result["_model"] = self._model_name
        return result

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def temperature(self) -> float:
        return 0.0

    @property
    def max_tokens(self) -> int:
        return 1024

    @property
    def remaining(self) -> int:
        return len(self._script)


def planner_react_script(
        *,
        tool_name: str = "write_file",
        tool_args: Optional[Dict[str, Any]] = None,
        step_description: str = "写文件",
        final_message: str = "任务完成",
        attachments: Optional[List[str]] = None,
) -> List[Any]:
    """构造一段"1 步计划 + 1 次工具调用"的标准脚本。

    顺序与 SUT 实际调用顺序严格对应（这是本脚本最重要的价值：它把 SUT 的
    真实调用序固化成可读的契约）：
        1. PlannerAgent.create_plan
        2. ReActAgent.execute_step -> 决定调用工具
        3. ReActAgent.execute_step -> 工具结果回灌后产出结构化结果
        4. PlannerAgent.update_plan
        5. ReActAgent.summarize
    """
    return [
        json_content({
            "title": "测试任务",
            "goal": "测试目标",
            "language": "中文",
            "steps": [{"description": step_description}],
            "message": "开始执行",
        }),
        tool_call(tool_name, tool_args or {"filepath": "/home/ubuntu/a.txt", "content": "hello"}),
        json_content({"success": True, "attachments": [], "result": "已写入"}),
        json_content({"steps": []}),
        json_content({"message": final_message, "attachments": attachments or []}),
    ]
