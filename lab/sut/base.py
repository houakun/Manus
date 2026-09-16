#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SUT（被测系统）契约 + lab 的标准化返回结构。

为什么先定义契约（handoff 决策 6）：
- 要求"agent 必须 headless 可调用"，落地形态就是 `run(goal) -> TaskResult`；
- Step 4 要写自己的精简 loop（lab/agent/loop.py），它必须能作为**另一个 SUT**
  挂到同一套 harness 上做 A/B 对比。所以这里抽成 Protocol：
  任何实现了 `async def run(goal, attachments) -> TaskResult` 的对象都是合法 SUT。
- UI 只是壳，评测只看 TaskResult —— 这样"跑一次任务"这件事才能进 CI。

字段设计对应 handoff 第 8 节的指标模板：
    ok                  → 成功率
    steps / usage       → 效率（步数、tokens/任务）
    cost_usd            → $/任务
    elapsed_ms          → P95 延迟
    error_type          → 失效归因（定位/规划/验证/工具）
    tool_sequence       → 过程分与"路径是否正确"分析的原材料
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from pydantic import BaseModel, Field

from lab.usage import Usage


class StepStats(BaseModel):
    """计划步骤的统计结果。"""

    total: int = 0  # 计划总步数
    done: int = 0  # 已结束（完成或失败）的步数
    succeeded: int = 0  # 执行成功的步数
    failed: int = 0  # 执行失败的步数


class TaskResult(BaseModel):
    """一次任务的标准化结果。

    这是 lab 内部以及对外（CLI / CI / 报告）唯一的数据交换格式。
    """

    # --- 身份与结论 ---
    task_id: str
    sut_name: str = ""  # 被测系统名字（A/B 对比时要能区分两条曲线）
    ok: bool = False  # 任务是否成功
    answer: str = ""  # 最终交付给用户的答复
    attachments: List[str] = Field(default_factory=list)  # 交付的文件（沙箱逻辑路径）
    error: Optional[str] = None  # 失败原因（人类可读）
    error_type: Optional[str] = None  # 失败类型（可枚举，用于归因统计）
    # 全部错误事件（按发生顺序）。为什么需要它：
    # 一次失败往往会产生**级联错误**（如"LLM 挂了" → "计划为空" → "任务终止"）。
    # 只看最后一条会把根因藏起来，归因结果就完全错了。error 保留**首个**错误（根因），
    # error_chain 保留完整链（用于区分"根因"与"后果"）。
    error_chain: List[str] = Field(default_factory=list)

    # --- 过程指标 ---
    plan_title: str = ""
    plan_goal: str = ""
    steps: StepStats = Field(default_factory=StepStats)
    tool_sequence: List[str] = Field(default_factory=list)  # 工具调用序列（按时间顺序）
    llm_usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    elapsed_ms: int = 0

    # --- 产物与轨迹 ---
    workspace: str = ""  # 本次任务的本地工作目录（可直接进去看 Agent 写了什么）
    trace_path: Optional[str] = None  # 轨迹文件路径（Step 2 填充）
    # 加固层观察结果（Step 3）：预算越界 / 循环检测 / 故障注入 / 重试 / 后置校验。
    # 用 dict 而不是一堆具名字段：这一步的观察项还会变（Step 4 会加置信区间相关项），
    # 每次都改 TaskResult 的字段会让所有构造点都要跟着改。
    guard: Dict[str, Any] = Field(default_factory=dict)

    def summary_line(self) -> str:
        """一行摘要，用于 CLI 输出与 CI 日志。"""
        status = "OK " if self.ok else "FAIL"
        return (
            f"[{status}] {self.sut_name} task={self.task_id[:8]} "
            f"steps={self.steps.done}/{self.steps.total} "
            f"tools={len(self.tool_sequence)} "
            f"tokens={self.llm_usage.total_tokens} "
            f"cost=${self.cost_usd:.4f} "
            f"elapsed={self.elapsed_ms}ms"
            + (f" error_type={self.error_type}" if self.error_type else "")
        )


class SUT(Protocol):
    """被测系统协议。实现它就自动获得 lab 的全部测量能力。"""

    name: str

    async def run(
            self,
            goal: str,
            attachments: Optional[List[str]] = None,
    ) -> TaskResult:
        """执行一个任务并返回标准化结果。"""
        ...

    async def aclose(self) -> None:
        """释放 SUT 持有的资源（沙箱、连接池、子进程等）。"""
        ...


class EventSink(Protocol):
    """事件旁路接口（Step 2 用）。

    现在只有一个空实现，目的是**先把接缝留出来**：
    轨迹采集不应该污染 TaskResult 的构造逻辑。
    """

    def on_event(self, event: Any) -> None:
        """接收 SUT 产生的原始事件。"""
        ...

    def extra_fields(self) -> Dict[str, Any]:
        """需要合并进 TaskResult 的附加字段（如 trace_path）。"""
        ...
