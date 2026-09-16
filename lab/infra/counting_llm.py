#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 代理：在 SUT 与真实模型之间采集 token 用量、时延与调用次数。

== 为什么用"代理 + 依赖注入"而不是改 SUT 的 BaseAgent ==
SUT 的 BaseAgent 只依赖 LLM 协议，而 flow 的 llm 又是构造参数 → 可以整体替换。
所以：
- 采集逻辑留在 lab 里，SUT 保持干净（它只需要在返回值里透出 `_usage`，见 S2 改造）；
- Step 4 做 A/B 对比时，两个 SUT 绝对共用同一个代理 → **口径一致**，
  不会出现"新 loop 的 token 统计方式不同导致看起来更省"这种自欺欺人的结论。

== 一个容易忽略的细节 ==
SUT 改造后，OpenAILLM 的返回值里多了 `_usage` / `_latency_ms` / `_model` 三个私有键。
本代理在**采集完立刻把它们摘掉**，保证下游（BaseAgent 的记忆、消息体）
看到的字典与改造前**逐字节一致** —— 即"零行为变更 + 零额外 token 消耗"。

== Step 2 追加：span 上报 ==
除了累计用量，代理还负责产出 **llm span**（父节点由 TraceRecorder 的栈顶决定，
正常情况就是当前正在执行的 step）。这是"零 SUT 改动拿到 span 级 LLM 指标"的关键：
- token 能精确归到某一步（而不是只有任务级总量）；
- 记录 prompt 指纹（体检第 9 项的 prompt/工具描述版本标识）；
- 记录上下文规模（context_chars / messages 数）——"上下文爆炸"的直接观测量。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.external.llm import LLM  # noqa: E402
from lab.trace.span import SpanSink, _digest  # noqa: E402
from lab.usage import Usage  # noqa: E402

# 附加在返回体上的私有遥测键（下划线前缀，采集后立即摘除）
_PRIVATE_KEYS = ("_usage", "_latency_ms", "_model")


class CountingLLM:
    """LLM 协议代理：转发调用 + 采集用量 + 上报 llm span。"""

    def __init__(self, inner: LLM, usage: Usage, sink: Optional[SpanSink] = None,
                 after_call: Optional[Callable[[], None]] = None) -> None:
        self._inner = inner
        self._usage = usage
        # sink 可选：不传也能工作（离线实验 / 单元测试场景）
        self._sink = sink
        # after_call 可选：每次调用后回调。Step 3 用它做"每次 LLM 调用后检查预算"——
        # token 与成本是在 LLM 调用时涨的，只在工具调用前检查会漏检
        # （一个不停思考、不调工具的 Agent 会完全绕过预算）。
        self._after_call = after_call

    def _measure_context(self, messages: List[Dict[str, Any]]) -> int:
        """估算本次请求的上下文字符数。

        为什么用字符数而不是 token 数：token 要等模型返回才知道（prompt_tokens），
        字符数是**发起前**就能测的本地量。两者配合使用——
        字符数看趋势（上下文在不在膨胀），token 数看成本。
        """
        total = 0
        for message in messages or []:
            try:
                total += len(json.dumps(message, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                total += len(str(message))
        return total

    async def invoke(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        """转发一次 LLM 调用并累计用量。"""
        # 1.开一个 llm span。注意 begin_span("llm") 默认 **不压栈**：
        #    它是最内层叶子，不该成为后续动作的父节点（否则"决定调工具的 LLM"会变成
        #    "工具执行"的父节点，语义就反了）。
        handle = None
        if self._sink is not None:
            # 两个指纹，用途完全不同（一开始只算了一个，结果每次调用都变，根本当不了版本标识）：
            #   prompt_digest  = system prompt + 工具描述 → **稳定**，用来标识"提示词/工具集版本"
            #   context_digest = 全量 messages → **每次都变**，用来标识"这一次的确切上下文"
            # 前者支撑体检第 9 项（prompt 版本），后者支撑可复现性排查（"同一次调用到底喂了什么"）。
            system_prompt = next(
                (m.get("content", "") for m in (messages or []) if m.get("role") == "system"), ""
            )
            handle = self._sink.begin_span(
                "llm",
                name=self._inner.model_name,
                attrs={
                    "model": self._inner.model_name,
                    "temperature": self._inner.temperature,
                    "messages": len(messages or []),
                    "has_tools": bool(tools),
                    "tools_count": len(tools) if tools else 0,
                    "tool_choice": tool_choice or "",
                    "response_format": (response_format or {}).get("type", ""),
                    "prompt_digest": _digest({"system": system_prompt, "tools": tools}),
                    "context_digest": _digest(messages),
                    # 上下文规模（"上下文爆炸"的观测量）
                    "context_chars": self._measure_context(messages),
                },
            )

        try:
            result = await self._inner.invoke(
                messages=messages,
                tools=tools,
                response_format=response_format,
                tool_choice=tool_choice,
            )
        except Exception as e:
            # 失败也要计数：真实重试率是可靠性工程的核心指标之一
            self._usage.llm_errors += 1
            if handle is not None:
                handle.end(
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    error_type="llm_error",
                )
            raise

        # 2.采集后再摘掉私有键（顺序很重要：先 pop 再返回，避免污染下游上下文）
        raw_usage = result.pop("_usage", None)
        latency_ms = result.pop("_latency_ms", 0) or 0
        result.pop("_model", None)
        self._usage.add_llm_call(raw_usage, latency_ms)

        # 3.补齐 span 属性
        if handle is not None:
            handle.end(
                status="ok",
                latency_ms=latency_ms,
                prompt_tokens=(raw_usage or {}).get("prompt_tokens"),
                completion_tokens=(raw_usage or {}).get("completion_tokens"),
                total_tokens=(raw_usage or {}).get("total_tokens"),
                has_tool_calls=bool(result.get("tool_calls")),
            )

        # 4.调用后回调（预算检查）。放在最后：此时 span 已经收口，
        #   预算越界会被标注到当前栈顶（即所在 step），而不是这条 llm span 上。
        if self._after_call is not None:
            self._after_call()

        return result

    # --- LLM 协议的只读属性，原样转发 ---

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def temperature(self) -> float:
        return self._inner.temperature

    @property
    def max_tokens(self) -> int:
        return self._inner.max_tokens

    @property
    def usage(self) -> Usage:
        """暴露采集到的用量，供 run_task 汇总。"""
        return self._usage
