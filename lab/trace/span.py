#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Span 模型与轨迹记录器（Step 2 的地基）。

== 决策一：为什么在 lab 侧重建，而不是改 SUT 埋点 ==
handoff 决策 1 要求"自研 loop 是核心资产"，Step 4 还要把自研 loop 挂到同一套 harness
上做 A/B 对比。如果 span 埋点写在 SUT 的 BaseAgent 里：
- 老项目的代码被评测逻辑污染；
- 自研 loop 必须重复实现一遍同样的埋点，两边的埋点质量稍有差异，A/B 结论就不可信。
所以在 lab 侧从**事件流**重建 span 树，SUT 保持零侵入。

代价（如实记录）：拿不到 SUT 内部的私有数据（例如某次 LLM 调用对应哪次迭代号）。
换取的是"双方共用同一套测量口径"。

== 决策二：为什么不用 contextvars ==
PlannerReActFlow 是一个协作式 async generator：消费者（ManusSUT）在调用 `__anext__`
之前设置状态，生成器恢复执行时就能读到（PEP 567 下协程不隔离 Context）。
所以只要 recorder 自己维护一个 span 栈，并在**事件消费循环**里 push/pop：
    StepEvent(STARTED)  -> push(step)   ... 生成器恢复 -> LLM/工具调用读栈顶 = step
    StepEvent(COMPLETED)-> pop(step)
就能得到正确的 task -> step -> {llm, tool} 嵌套，完全不需要 contextvars。

什么情况下这个方案会失效：SUT 用 asyncio.gather 并发跑多个步骤（栈会串味）。
届时需要换成 "contextvar 持有 span 栈"，或者在 recorder 外层按 task 隔离。
现在不引入这个复杂度。

== span 的语义边界（如实说明）==
只区分 task / step / tool / llm 四类：
- **不单独建"规划阶段"span**：要知道 planning/updating 的边界必须让 SUT 上报状态，
  那就破坏零侵入了。好在信息没丢——规划期的 LLM 调用父节点就是 task span，
  从 trace 里能直接看出来。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

from pydantic import BaseModel, Field


class SpanKind(str, Enum):
    """span 类型。"""

    TASK = "task"  # 整个任务（根）
    STEP = "step"  # 计划中的一步
    TOOL = "tool"  # 一次工具调用
    LLM = "llm"  # 一次语言模型调用


class Span(BaseModel):
    """一个执行区间。

    时间字段用**相对任务开始的毫秒偏移**而不是绝对时间戳：
    这样两条 trace 可以直接叠在一起对比（"新方案在第几步开始变慢"一眼可见），
    也不会因为时区/时钟漂移出问题。绝对时间记在 tasks 表里。
    """

    span_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    parent_id: Optional[str] = None
    name: str = ""
    kind: SpanKind = SpanKind.TASK
    status: str = "ok"  # ok / error / running
    start_ms: int = 0  # 相对任务开始的偏移
    end_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    attrs: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.end_ms is None


class SpanHandle:
    """一次 span 的句柄：由 recorder 发放，调用方负责 end()。

    为什么要句柄而不是"begin/end 都传 id"：避免调用方自己管理 id，
    也让"忘了 end"这件事变得显眼（recorder 会在收尾时把未关闭的 span 标成 error）。
    """

    __slots__ = ("_recorder", "_span")

    def __init__(self, recorder: "TraceRecorder", span: Span) -> None:
        self._recorder = recorder
        self._span = span

    @property
    def span(self) -> Span:
        return self._span

    @property
    def span_id(self) -> str:
        return self._span.span_id

    def end(
            self,
            *,
            status: str = "ok",
            error: Optional[str] = None,
            error_type: Optional[str] = None,
            **attrs: Any,
    ) -> Span:
        """结束 span，附上结果属性。幂等：重复调用不会再改时间。"""
        if not self._span.is_open:
            return self._span

        self._span.end_ms = self._recorder.now_offset_ms()
        self._span.duration_ms = max(0, self._span.end_ms - self._span.start_ms)
        self._span.status = status
        if error:
            self._span.error = error
        if error_type:
            self._span.attrs["error_type"] = error_type
        for key, value in attrs.items():
            # 只记录可 JSON 序列化的值，避免把对象塞进 trace
            self._span.attrs[key] = value if _is_jsonable(value) else str(value)

        self._recorder._on_span_end(self._span)
        return self._span


class SpanSink(Protocol):
    """给"旁路采集器"用的最小接口（CountingLLM 就是靠它上报 LLM span）。

    只暴露一个方法，是为了让 LLM 代理不依赖 recorder 的全部能力：
    代理只需要"开一个 span、结束时填属性"，不需要知道栈的存在。
    """

    def begin_span(self, kind: str, name: str, attrs: Optional[Dict[str, Any]] = None) -> SpanHandle:
        ...


def _is_jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _digest(value: Any) -> str:
    """给任意结构算一个短指纹（用于 prompt / 工具描述版本标识）。"""
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class TraceRecorder:
    """一条轨迹的记录器。

    **不变量：一个 recorder 实例 = 一个任务 = 一条串行事件流。**
    span 栈是实例级普通列表，不做并发保护 —— 并发场景请为每个任务创建独立 recorder。
    这个约束是刻意的：Step 4 并行跑任务集时，"每个任务一条 trace"本来就是正确粒度。
    """

    def __init__(self, task_id: str, *, sut_name: str = "", goal: str = "") -> None:
        self.task_id = task_id
        self.sut_name = sut_name
        self.goal = goal
        self.started_at = time.time()
        self._t0 = time.monotonic()

        self._spans: List[Span] = []
        self._stack: List[Span] = []

        # 根 span 立即创建（父节点为 None）
        self.task_span = self._new_span(SpanKind.TASK, name=sut_name or "task", parent=None)
        self._spans.append(self.task_span)
        self._stack.append(self.task_span)

    # ==================== 元信息 ====================

    def label(self, *, sut_name: Optional[str] = None, goal: Optional[str] = None) -> None:
        """补充任务元信息。

        为什么需要这个方法：recorder 必须在 SUT 构造**之前**创建（因为 LLM 代理要用它），
        而 SUT 的名字又是在构造时才确定的。用 label() 补而不是把名字硬编码进 run_task，
        避免"两处各写一份 SUT 名字"导致对不上。
        """
        if sut_name:
            self.sut_name = sut_name
            self.task_span.name = sut_name
        if goal:
            self.goal = goal

    # ==================== 时间 ====================

    def now_offset_ms(self) -> int:
        """当前时间相对任务开始的毫秒偏移。"""
        return int((time.monotonic() - self._t0) * 1000)

    # ==================== span 生命周期 ====================

    def _new_span(self, kind: SpanKind, name: str, parent: Span, attrs: Optional[Dict[str, Any]] = None) -> Span:
        return Span(
            task_id=self.task_id,
            parent_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
            status="running",
            start_ms=self.now_offset_ms(),
            attrs=dict(attrs or {}),
        )

    def begin_span(
            self,
            kind: str,
            name: str,
            attrs: Optional[Dict[str, Any]] = None,
            *,
            push: Optional[bool] = None,
    ) -> SpanHandle:
        """开一个 span。

        push 语义（决定了 span 树长什么样）：
        - step / tool 默认 **push**：它们会"包住"其间的 llm 调用与子动作；
        - llm 默认 **不 push**：它是最内层的叶子，不该成为后续动作的父节点。
        这一点很关键：如果 llm 也压栈，那么"决定调用工具的那次 LLM 调用"
        就会变成工具 span 的父节点，语义就错了（是先有 LLM 决策、后有工具执行，不是包含关系）。
        """
        span_kind = SpanKind(kind)
        if push is None:
            push = span_kind in (SpanKind.STEP, SpanKind.TOOL)

        parent = self._stack[-1] if self._stack else None
        span = self._new_span(span_kind, name, parent, attrs)
        self._spans.append(span)
        if push:
            self._stack.append(span)
        return SpanHandle(self, span)

    def _on_span_end(self, span: Span) -> None:
        """span 结束时把它从栈里摘掉（可能不在栈顶：异常路径会乱序结束）。"""
        if span in self._stack:
            # 从栈顶往下找第一个匹配项，并把它之上的也弹掉（保持栈一致）
            while self._stack:
                top = self._stack.pop()
                if top is span:
                    break

    def current_span(self) -> Optional[Span]:
        """当前栈顶 span（中间件用它来定位"该给哪个 span 补属性"）。"""
        return self._stack[-1] if self._stack else None

    def annotate(self, **attrs: Any) -> None:
        """把附加属性写到**当前栈顶** span 上。

        为什么中间件不自己建 span：工具 span 已经由适配器根据 `ToolEvent` 建好了
        （事件才是权威来源）。中间件只需要"在已有 span 上补注"；
        如果它也建一个，一次工具调用就会出现两个 span，所有工具类指标都会翻倍。
        """
        span = self.current_span()
        if span is None:
            return
        for key, value in attrs.items():
            if value is None:
                continue
            span.attrs[key] = value if _is_jsonable(value) else str(value)

    def close_open_spans(self, *, error: Optional[str] = None, status: str = "error") -> None:
        """收尾：把所有未关闭的 span 关掉。

        为什么必须有这个：异常/超时/break 跳出事件循环时，栈里会残留 span。
        如果不处理，trace 里就会出现一堆 duration=None 的"幽灵 span"，
        而**恰恰是异常路径最需要看清**（这是排查崩溃时唯一的一手材料）。
        """
        while self._stack:
            span = self._stack.pop()
            if span.is_open:
                span.end_ms = self.now_offset_ms()
                span.duration_ms = max(0, span.end_ms - span.start_ms)
                span.status = status
                if error and span.kind == SpanKind.TASK:
                    span.error = error

    def finish_task(
            self,
            *,
            ok: bool,
            error: Optional[str] = None,
            error_type: Optional[str] = None,
            **attrs: Any,
    ) -> Span:
        """结束根 span 并落上任务级汇总属性。"""
        self.close_open_spans(error=error, status="error" if not ok else "ok")

        self.task_span.end_ms = self.now_offset_ms()
        self.task_span.duration_ms = max(0, self.task_span.end_ms - self.task_span.start_ms)
        self.task_span.status = "ok" if ok else "error"
        self.task_span.error = error
        if error_type:
            self.task_span.attrs["error_type"] = error_type
        for key, value in attrs.items():
            self.task_span.attrs[key] = value if _is_jsonable(value) else str(value)
        return self.task_span

    # ==================== 便捷方法（SUT 适配器用） ====================

    def open_step(self, description: str, step_id: str = "") -> SpanHandle:
        return self.begin_span(SpanKind.STEP, description[:120] or "step", {"step_id": step_id})

    def open_tool(self, function_name: str, tool_name: str = "", args: Optional[Dict[str, Any]] = None) -> SpanHandle:
        return self.begin_span(
            SpanKind.TOOL,
            function_name,
            {"tool_name": tool_name, "args_digest": _digest(args or {}), "args": _truncate_args(args or {})},
        )

    # ==================== 读取 ====================

    @property
    def spans(self) -> List[Span]:
        return list(self._spans)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "sut_name": self.sut_name,
            "goal": self.goal,
            "spans": [s.model_dump(mode="json") for s in self._spans],
        }


def _truncate_args(args: Dict[str, Any], limit: int = 400) -> Dict[str, Any]:
    """截断工具参数，避免把整篇文件内容塞进 trace（trace 要能被人读）。"""
    out: Dict[str, Any] = {}
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        out[key] = text if len(text) <= limit else text[:limit] + f"...(截断,共{len(text)}字符)"
    return out
