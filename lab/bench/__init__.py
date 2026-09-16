#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测 harness（Step 4）：任务集 / 执行器 / 判定器 / 指标 / 报告。

三层结构中的最外层：它把 SUT 变成一条**可比较的曲线**。
"""

from lab.bench.task import BenchTask, Fixture, ProcessRule, SolutionStep, VerifyCheck, load_all_tasks
from lab.bench.verifier import CheckResult, evaluate_process, run_checks

__all__ = [
    "BenchTask", "Fixture", "ProcessRule", "SolutionStep", "VerifyCheck", "load_all_tasks",
    "CheckResult", "run_checks", "evaluate_process",
]
