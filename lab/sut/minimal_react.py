#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自研最小 ReAct loop —— **第二 SUT**，用于做"朴素基线 vs 课程版 loop"的对照。

== 为什么需要它（这是本项目最大的可信度缺口）==
`lab/` 里的被测对象一直是 `api/`（课程作者的 `PlannerReActFlow`）。
于是"我的 Agent 有多稳"这句话，讲的是**别人写的控制流**；
面试官问"哪部分是你写的"会很被动（`docs/step1-architecture-and-refactor-plan.md:167` 自己写过这个风险）。

所以这里写一个**不用任何框架**的最小 loop（while + messages + 工具调用 + 终止条件），
把它接成同一个 `SUT` 协议 —— 于是同一个 harness、同一套判定器、同一套加固中间件，
可以同时对两条 loop 出数：

    python -m lab bench run --task syn_sum_range --sut manus          # 课程版
    python -m lab bench run --task syn_sum_range --sut minimal-react  # 我写的

== 两个刻意的设计决定 ==
1. **工具层复用 SUT 的**（`FileTool` / `ShellTool`）：
   这样两条 loop 的差别**只有控制流**。如果连工具也自己写一套，
   成功率差异就无法归因（"是 loop 好还是工具好？"）。
2. **错误不抛出，而是回灌给模型**：这是朴素 loop 的真实形态
   （没有重试/熔断，靠模型自己看着办）—— 也正是加固中间件要改善的地方。
   工具层仍走 `guard.wrap_tools(...)`，所以重试/后置校验/故障注入照旧生效。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from lab.sut.base import StepStats, TaskResult


@dataclass
class _Observation:
    """工具失败时回灌给模型的文本载体。

    为什么不用抛异常：朴素 loop 的真实形态就是"把错误告诉模型，让模型自己看着办"；
    而抛异常会直接结束整个任务，那是另一回事（加固层要改善的正是这个）。
    """

    message: str
    data: Any = None


# 极简系统提示：只说"你有工具、做完直接给答复"。
# 刻意不抄课程版的长提示词（那是被测对象的一部分，不是基线该有的东西）。
SYSTEM_PROMPT = """你是一个可以调用工具的智能体，在 Linux 沙箱的 /home/ubuntu 下工作。

规则：
1. 需要了解环境或读写文件时，调用工具；
2. 每一步只做一件明确的事，并根据工具返回的真实结果决定下一步；
3. 任务完成后，**直接给出最终答复**（不要再调用工具）。"""


def _observation(result: Any) -> str:
    """把工具返回体转成给模型看的文本（朴素做法：message + data 的 JSON 截断）。"""
    message = getattr(result, "message", "") or ""
    data = getattr(result, "data", None)
    if data:
        try:
            payload = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(data)
        message = f"{message}\n{payload[:2000]}"
    return message[:4000]


class MinimalReActSUT:
    """最小 ReAct loop（实现 `lab/sut/base.py` 的 SUT 协议，无框架依赖）。"""

    name = "minimal-react[fast]"

    def __init__(
        self,
        *,
        llm: Any,
        tools: List[Any],
        usage: Any,
        workspace: Any,
        model_name: str = "",
        max_steps: int = 12,
        max_seconds: float = 600.0,
        trace: Any = None,
    ) -> None:
        self._llm = llm
        self._tools = list(tools)
        self._usage = usage
        self._workspace = workspace
        self._model_name = model_name
        self._max_steps = max(1, int(max_steps))
        self._max_seconds = float(max_seconds)
        self._trace = trace
        self._schemas: List[Dict[str, Any]] = [s for tool in self._tools for s in tool.get_tools()]

    # ==================== SUT 协议 ====================

    async def run(self, goal: str, attachments: Optional[List[str]] = None) -> TaskResult:
        """跑一个任务：while 循环 + 工具调用，直到模型不再调工具或达到上限。"""
        task_id = str(uuid.uuid4())
        started = time.monotonic()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ]
        tool_sequence: List[str] = []
        answer = ""
        error: Optional[str] = None
        steps = 0

        for index in range(self._max_steps):
            if time.monotonic() - started > self._max_seconds:
                error = f"超过最大执行时长 {self._max_seconds:.0f}s"
                break
            steps += 1
            step_span = self._trace.open_step(f"step {steps}") if self._trace else None
            try:
                message = await self._llm.invoke(messages, tools=self._schemas, tool_choice="auto")
            except Exception as exc:  # noqa: BLE001
                # 朴素 loop 的真实形态：LLM 调用失败就结束（没有重试）
                error = f"LLM 调用失败: {type(exc).__name__}: {exc}"
                if step_span:
                    step_span.end(success=False)
                break

            # 剥掉 lab 附着的遥测键（`_usage` / `_model` …），只把模型本来给的东西放回对话
            messages.append({k: v for k, v in message.items() if not str(k).startswith("_")})
            calls = message.get("tool_calls") or []
            if not calls:
                answer = message.get("content") or ""
                if step_span:
                    step_span.end(success=True)
                break

            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                result = await self._invoke(name, args)
                tool_sequence.append(name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": _observation(result),
                    }
                )
            if step_span:
                step_span.end(success=True)
        else:
            error = f"达到最大步数 {self._max_steps} 仍未结束"

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return TaskResult(
            task_id=task_id,
            sut_name=self.name,
            # `ok` 在这里只是**自述**：判定器（verifier）才是结果口径。
            ok=error is None,
            answer=answer,
            error=error,
            error_type="sut_error" if error else None,
            plan_title="最小 ReAct loop（无框架，自研）",
            plan_goal=goal,
            steps=StepStats(total=steps, done=steps, succeeded=steps if error is None else steps - 1),
            tool_sequence=tool_sequence,
            llm_usage=self._usage,
            cost_usd=self._usage.cost_usd(self._model_name) if self._model_name else 0.0,
            elapsed_ms=elapsed_ms,
            workspace=str(self._workspace),
        )

    async def aclose(self) -> None:
        """释放资源。工具/沙箱由 `lab.api` 的组成根持有，这里没有额外资源。"""

    # ==================== 内部 ====================

    async def _invoke(self, name: str, args: Dict[str, Any]) -> Any:
        """按名字找工具并调用；**错误不抛出**，而是回灌给模型（朴素 loop 的形态）。"""
        for tool in self._tools:
            if tool.has_tool(name):
                try:
                    return await tool.invoke(name, **args)
                except Exception as exc:  # noqa: BLE001
                    return _Observation(f"工具 {name} 抛异常：{type(exc).__name__}: {exc}")
        available = ", ".join(s.get("function", {}).get("name", "?") for s in self._schemas)
        return _Observation(f"没有名为 {name} 的工具（可用：{available}）")
