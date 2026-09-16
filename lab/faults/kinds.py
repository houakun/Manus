#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""故障类型定义。

10 类故障，每一类都对应**一种不同的检测/恢复能力**，不是凑数：

| # | 故障 | 表征 | 只有什么能力才能抓住它 |
|---|------|------|------------------------|
| 1 | timeout | 工具超时 | 超时控制 + 幂等重试 |
| 2 | transient_error | 前 N 次失败、之后正常 | 重试（且必须幂等感知） |
| 3 | permanent_error | 每次必失败 | 熔断（别把预算烧在必败上） |
| 4 | malformed_result | success=True 但字段缺失 | 结构校验（后置校验） |
| 5 | empty_result | success=True 但数据为空 | 空值校验（后置校验） |
| 6 | truncated_result | 结果被悄悄截断 | 长度/内容比对（后置校验） |
| 7 | silent_wrong_result | success=True 且结构完整，**内容是错的** | 内容级校验 —— 最难的一类 |
| 8 | partial_write | 写入只完成一部分 | 回读比对（最强的后置校验） |
| 9 | latency_spike | 不失败，只是很慢 | 预算/墙钟监控 |
| 10 | flaky | 随机失败 | 统计（单次运行看不出来） |

第 4~8 类都是"静默失败"：没有任何一方会报警，Agent 会基于假事实继续推理。
**后置校验是唯一能发现它们的手段** —— 这也是为什么 Step 3 必须同时做故障注入和校验，
只做注入的话你只会看到"成功率下降"，却不知道下降是因为模型变笨还是因为校验缺失。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class FaultKind(str, Enum):
    """故障类型。"""

    TIMEOUT = "timeout"
    TRANSIENT_ERROR = "transient_error"
    PERMANENT_ERROR = "permanent_error"
    MALFORMED_RESULT = "malformed_result"
    EMPTY_RESULT = "empty_result"
    TRUNCATED_RESULT = "truncated_result"
    SILENT_WRONG_RESULT = "silent_wrong_result"
    PARTIAL_WRITE = "partial_write"
    LATENCY_SPIKE = "latency_spike"
    FLAKY = "flaky"

    @property
    def is_silent(self) -> bool:
        """是否属于"静默失败"（success=True 但结果有问题）。

        这个分类直接决定"该用什么手段发现它"：
        显式失败靠重试与熔断，静默失败只能靠后置校验。
        """
        return self in {
            FaultKind.MALFORMED_RESULT,
            FaultKind.EMPTY_RESULT,
            FaultKind.TRUNCATED_RESULT,
            FaultKind.SILENT_WRONG_RESULT,
            FaultKind.PARTIAL_WRITE,
        }

    @property
    def is_retryable_class(self) -> bool:
        """这个故障是否属于"暂态"（值得重试）。"""
        return self in {FaultKind.TIMEOUT, FaultKind.TRANSIENT_ERROR, FaultKind.FLAKY}


class FaultRule(BaseModel):
    """一条故障注入规则。"""

    kind: FaultKind
    tool: str = "*"  # 匹配的工具名，支持 glob（如 "shell_*"）
    rate: float = Field(1.0, ge=0.0, le=1.0)  # 命中概率
    start_after: int = 0  # 该工具被调用 N 次之后才开始注入（模拟"跑到一半开始坏"）
    max_injections: Optional[int] = None  # 最多注入几次（None = 不限）
    fail_times: Optional[int] = None  # transient_error 专用：前 N 次失败
    latency_s: float = 2.0  # latency_spike 专用：注入的额外延迟

    def describe(self) -> str:
        return f"{self.kind.value}@{self.tool}(rate={self.rate})"
