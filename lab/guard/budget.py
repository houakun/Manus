#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预算：token / 成本 / 墙钟 / 步数 / 工具调用 的软阈值与硬上限。

== 三种模式（Step 3 只用 OBSERVE）==
    OBSERVE  只测量、只记录，绝不干预 —— 用来采集"到底需要多少预算"的数据
    DEGRADE  超软阈值后强制收尾（把已有结果交付出去）
    ENFORCE  超硬上限立即中止（数据干净，但丢掉结果）

为什么 Step 3 必须先 OBSERVE：
阈值是**关于这个系统的事实**，不是可以拍脑袋的设计参数。用户给的 n=5 基线
（token 均值 63k / max 146k，耗时均值 21s / P95 50s）本身就说明：
**同一个任务的最大值是最小值的两倍以上**（见 Step 1 报告里的 34% 波动）。
在这个方差下，任何"硬上限"都会随机砍掉一部分本来能成功的任务，
而你会把它误读成"加固有效降低了成本"。

== 硬上限的设计 ==
硬上限不由人手工设，而是 `软阈值 × enforce_multiplier`（默认 2.5）。
理由：软阈值是"正常任务的边界"，硬上限应该表达"这已经明显是失控了"。
两者差得足够远，才不会因为一次正常的慢任务就误杀。
预留给 enforce 阶段用，现在只计算、不启用。
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from lab.usage import Usage


class BudgetMode(str, Enum):
    """预算模式。"""

    OBSERVE = "observe"  # 只记录
    DEGRADE = "degrade"  # 超软阈值后收尾
    ENFORCE = "enforce"  # 超硬上限即中止


class BudgetMetric(str, Enum):
    """被计量的预算维度。"""

    TOKENS = "tokens"
    COST = "cost_usd"
    ELAPSED = "elapsed_s"
    STEPS = "steps"  # 已执行的计划步骤数
    TOOL_CALLS = "tool_calls"
    LLM_CALLS = "llm_calls"  # 只观察，无默认阈值


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    if raw.lower() in ("none", "off", "-1"):
        return None  # 显式关闭某个阈值
    try:
        return float(raw)
    except ValueError:
        return default


class BudgetPolicy(BaseModel):
    """软阈值配置（硬上限按倍数推算）。"""

    mode: BudgetMode = BudgetMode.OBSERVE

    # 软阈值：默认值来自用户的 n=5 基线观察值
    max_tokens: Optional[float] = 150_000  # token 总量
    max_cost_usd: Optional[float] = 0.10  # 美元成本
    max_seconds: Optional[float] = 60.0  # 墙钟秒数
    max_steps: Optional[float] = 15  # 计划步骤数
    max_tool_calls: Optional[float] = 15  # 工具调用次数
    # 迭代次数（LLM 回合）默认只观察：它和 tool_calls 高度相关，先不设阈值，
    # 免得两个阈值互相干扰、说不清是哪条先触发。
    max_llm_calls: Optional[float] = None

    # 硬上限 = 软阈值 × 该系数（Step 4 拿到 n>=20 数据、切换到 enforce 后再启用）
    enforce_multiplier: float = 2.5

    @classmethod
    def from_env(cls) -> "BudgetPolicy":
        """从环境变量读配置（CI 里换阈值不需要改代码）。"""
        default = cls()
        mode_raw = (os.getenv("LAB_BUDGET_MODE") or default.mode.value).lower()
        try:
            mode = BudgetMode(mode_raw)
        except ValueError:
            mode = default.mode
        return cls(
            mode=mode,
            max_tokens=_env_float("LAB_BUDGET_TOKENS", default.max_tokens),
            max_cost_usd=_env_float("LAB_BUDGET_COST", default.max_cost_usd),
            max_seconds=_env_float("LAB_BUDGET_SECONDS", default.max_seconds),
            max_steps=_env_float("LAB_BUDGET_STEPS", default.max_steps),
            max_tool_calls=_env_float("LAB_BUDGET_TOOL_CALLS", default.max_tool_calls),
            max_llm_calls=_env_float("LAB_BUDGET_LLM_CALLS", default.max_llm_calls),
            enforce_multiplier=_env_float("LAB_BUDGET_HARD_MULTIPLIER", default.enforce_multiplier) or 2.5,
        )

    def soft_limits(self) -> Dict[BudgetMetric, float]:
        raw = {
            BudgetMetric.TOKENS: self.max_tokens,
            BudgetMetric.COST: self.max_cost_usd,
            BudgetMetric.ELAPSED: self.max_seconds,
            BudgetMetric.STEPS: self.max_steps,
            BudgetMetric.TOOL_CALLS: self.max_tool_calls,
            BudgetMetric.LLM_CALLS: self.max_llm_calls,
        }
        return {metric: value for metric, value in raw.items() if value is not None}

    def hard_limits(self) -> Dict[BudgetMetric, float]:
        return {metric: value * self.enforce_multiplier for metric, value in self.soft_limits().items()}


class BudgetViolation(BaseModel):
    """一次阈值越界。"""

    metric: str
    value: float
    soft: float
    hard: Optional[float] = None
    exceeded_hard: bool = False

    def describe(self) -> str:
        hard_note = f"，硬上限 {self.hard:.4g}" if self.hard else ""
        return f"{self.metric}={self.value:.4g} 超过软阈值 {self.soft:.4g}{hard_note}"


class BudgetReport(BaseModel):
    """任务结束时的预算报告（落进 TaskResult，供 Step 4 聚合）。"""

    mode: str = BudgetMode.OBSERVE.value
    checks: int = 0  # 一共检查了多少次
    violations: List[BudgetViolation] = Field(default_factory=list)  # 首次越界记录
    violated_metrics: List[str] = Field(default_factory=list)  # 去重后的越界维度
    would_stop: bool = False  # enforce 模式下"本会中止"
    would_degrade: bool = False  # degrade 模式下"本会收尾"
    hard_exceeded: bool = False  # 是否突破了硬上限
    final: Dict[str, float] = Field(default_factory=dict)  # 结束时各项实际值
    limits: Dict[str, float] = Field(default_factory=dict)  # 生效的软阈值（留档，便于复现）


class Budget:
    """预算计数器与判定器。

    它有状态（步数/工具调用计数、开始时间），所以**一个 Budget 实例 = 一个任务**。
    与 TraceRecorder 一样，这是刻意的粒度选择：Step 4 并行跑任务时每个任务一套。
    """

    def __init__(
            self,
            usage: Usage,
            *,
            model_name: str = "",
            policy: Optional[BudgetPolicy] = None,
            clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._usage = usage
        self._model_name = model_name
        self.policy = policy or BudgetPolicy()
        self._clock = clock
        self._started_at = clock()

        # 由中间件推进的计数
        self.steps = 0
        self.tool_calls = 0

        # 观察结果
        self._checks = 0
        self._violations: List[BudgetViolation] = []
        self._violated_metrics: List[str] = []

    # ==================== 计数 ====================

    def note_step(self, count: int = 1) -> None:
        self.steps += count

    def note_tool_call(self, count: int = 1) -> None:
        self.tool_calls += count

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._started_at

    # ==================== 快照与判定 ====================

    def snapshot(self) -> Dict[BudgetMetric, float]:
        """当前各项实际值。"""
        return {
            BudgetMetric.TOKENS: float(self._usage.total_tokens),
            BudgetMetric.COST: float(self._usage.cost_usd(self._model_name)),
            BudgetMetric.ELAPSED: round(self.elapsed_s, 3),
            BudgetMetric.STEPS: float(self.steps),
            BudgetMetric.TOOL_CALLS: float(self.tool_calls),
            BudgetMetric.LLM_CALLS: float(self._usage.llm_calls),
        }

    def check(self) -> List[BudgetViolation]:
        """检查一次预算，返回**本次新发现**的越界项。

        去重语义很重要：如果我们每次检查都把已越界的维度再报一遍，
        那么"违约次数"这个指标就变成了"检查次数"的别名，毫无意义。
        所以每个维度只在**首次越界**时记录一次。
        """
        self._checks += 1
        values = self.snapshot()
        soft_limits = self.policy.soft_limits()
        hard_limits = self.policy.hard_limits()

        new_violations: List[BudgetViolation] = []
        for metric, soft in soft_limits.items():
            value = values.get(metric, 0.0)
            if value <= soft:
                continue
            if metric.value in self._violated_metrics:
                continue

            hard = hard_limits.get(metric)
            violation = BudgetViolation(
                metric=metric.value,
                value=value,
                soft=soft,
                hard=hard,
                exceeded_hard=bool(hard is not None and value > hard),
            )
            new_violations.append(violation)
            self._violated_metrics.append(metric.value)
            self._violations.append(violation)

        return new_violations

    # ==================== 报告 ====================

    def report(self) -> BudgetReport:
        """产出最终报告。"""
        hard_exceeded = any(v.exceeded_hard for v in self._violations)
        return BudgetReport(
            mode=self.policy.mode.value,
            checks=self._checks,
            violations=self._violations,
            violated_metrics=list(self._violated_metrics),
            # 三种模式下的"本会怎样"：observe 阶段最有用的就是这两个布尔量，
            # 它们让我们在**不改变 SUT 行为**的前提下，预演 enforce/degrade 的后果。
            would_stop=self.policy.mode != BudgetMode.ENFORCE and hard_exceeded,
            would_degrade=self.policy.mode != BudgetMode.DEGRADE and bool(self._violated_metrics),
            hard_exceeded=hard_exceeded,
            final={metric.value: round(value, 4) for metric, value in self.snapshot().items()},
            limits={metric.value: value for metric, value in self.policy.soft_limits().items()},
        )

    @property
    def violations(self) -> List[BudgetViolation]:
        return list(self._violations)
