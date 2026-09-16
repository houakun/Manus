#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""可靠性加固层：预算 / 熔断 / 重试 / 后置校验。

与 EvalOps 层（lab/trace）的分工：
    EvalOps  = 测量仪器（看见发生了什么）
    加固层   = 控制手段（决定要不要干预）

本层刻意先把"干预"关掉（observe 模式）：**阈值必须先有数据才能定**，
凭直觉设一个硬上限，你会得到一堆"任务被砍掉"却没有"到底该给多少预算"的结论。
"""

from lab.guard.budget import Budget, BudgetMode, BudgetPolicy, BudgetReport, BudgetViolation
from lab.guard.loop_guard import LoopGuard, RepeatHit
from lab.guard.postcondition import check_postcondition
from lab.guard.retry import IDEMPOTENT_TOOLS, NON_IDEMPOTENT_TOOLS, RetryDecision, compute_backoff, decide_retry, is_idempotent

__all__ = [
    "Budget", "BudgetMode", "BudgetPolicy", "BudgetReport", "BudgetViolation",
    "LoopGuard", "RepeatHit",
    "check_postcondition",
    "RetryDecision", "compute_backoff", "decide_retry", "is_idempotent",
    "IDEMPOTENT_TOOLS", "NON_IDEMPOTENT_TOOLS",
]
