#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CLI 层的测试。

== 为什么 CLI 也要测 ==
这一层的 bug 模式很特殊：函数体里 `from x import y` 的**局部导入**
写错了名字，只要没人执行那条分支就永远不会报错
（本项目的 `bench run` 就真的漏了一个 import，直到第一次真跑才炸）。
而 CLI 恰恰是"人第一次接触这个工具"的入口 —— 它一炸，工具就等于不存在。

所以这里用"替换掉昂贵/慢的部分、只驱动参数解析与输出流程"的方式覆盖它：
- `bench run`：把 `run_suite` 换成返回构造好的 SuiteResult（不调模型、不花钱）
- `bench report` / `bench list`：直接跑真实代码路径
"""

from __future__ import annotations

import asyncio

import pytest

from lab.bench.runner import RunOutcome, SuiteResult, summarize_task
from lab.bench.task import load_all_tasks
from lab.cli import main


def _fake_suite() -> SuiteResult:
    """构造一个只有 1 个任务、2 次运行（1 成功 1 失败）的最小 SuiteResult。"""
    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    outcomes = [
        RunOutcome(
            run_id="r1", suite_id="s1", task_uid=task.uid, task_key=task.key,
            group=task.group, run_index=0, ok=True, tokens=1000, cost_usd=0.001,
            elapsed_ms=5000, llm_calls=5, tool_calls=3, steps_done=1,
        ),
        RunOutcome(
            run_id="r2", suite_id="s1", task_uid=task.uid, task_key=task.key,
            group=task.group, run_index=1, ok=False, tokens=2000, cost_usd=0.002,
            elapsed_ms=9000, llm_calls=8, tool_calls=6, steps_done=1,
            error_type="agent_error", failed_checks=["✗ [numeric] 答案不对"],
        ),
    ]
    return SuiteResult(
        suite_id="s1", label="测试", model_name="fake-model", runs_per_task=1,
        started_at="2026-01-01T00:00:00", elapsed_s=14.0,
        outcomes=outcomes, task_summaries=[summarize_task(task, outcomes)],
    )


def test_cli_bench_run_writes_report(tmp_path, monkeypatch):
    """`bench run --report` 必须能跑完并写出报告文件（不调真模型）。"""
    import lab.bench.runner as runner_module
    from lab import api as lab_api

    monkeypatch.setattr(runner_module, "run_suite", lambda **kwargs: _async(_fake_suite()))
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _FakeStore())
    monkeypatch.setattr("lab.cli.ensure_runs_dir", lambda: tmp_path)

    code = main(["bench", "run", "--runs", "1", "--label", "x", "--report", "--yes"])

    assert code == 0
    reports = list((tmp_path / "reports").glob("*.md"))
    assert len(reports) == 1
    assert "总体指标" in reports[0].read_text(encoding="utf-8")


def test_cli_bench_run_refuses_expensive_run_without_yes(tmp_path, monkeypatch):
    """花钱的操作必须先确认：预估超过阈值时应该拒绝执行。"""
    import lab.bench.runner as runner_module

    called = {"n": 0}

    def _should_not_run(**kwargs):
        called["n"] += 1
        return _async(_fake_suite())

    monkeypatch.setattr(runner_module, "run_suite", _should_not_run)

    # 20 个任务 × 5 次 → 估算超过 1 美元 → 应被拦下
    code = main(["bench", "run", "--runs", "5"])

    assert code == 2
    assert called["n"] == 0  # 一步都没跑


def test_cli_bench_list_and_report(tmp_path, monkeypatch):
    """`bench list` 与 `bench report` 要能从库里读出来并渲染。"""
    from lab import api as lab_api

    store = _FakeStore()
    store.save_suite(_fake_suite())
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: store)

    assert main(["bench", "list"]) == 0
    assert main(["bench", "report", "s1"]) == 0
    assert main(["bench", "report"]) == 0  # 不传 id → 取最近一次


def test_cli_bench_tasks_and_validate():
    """`bench tasks` / `bench validate` 都不花钱，必须能跑。"""
    assert main(["bench", "tasks", "--group", "synthetic"]) == 0
    assert main(["bench", "validate", "--group", "synthetic"]) == 0


# ==================== 替身 ====================

async def _async(value):
    return value


class _FakeStore:
    """只实现 CLI 用到的那几个方法的内存替身。"""

    def __init__(self) -> None:
        self.suites = {}

    def save_suite(self, suite) -> None:
        self.suites[suite.suite_id] = suite

    def list_suites(self, limit: int = 10):
        return [
            {"suite_id": s.suite_id, "label": s.label, "model_name": s.model_name,
             "runs_per_task": s.runs_per_task, "started_at": s.started_at,
             "total_runs": s.total_runs, "successes": s.successes,
             "task_count": len(s.task_summaries), "elapsed_s": s.elapsed_s,
             "budget_mode": s.budget_mode}
            for s in list(self.suites.values())[-limit:]
        ]

    def load_suite(self, suite_id: str):
        return self.suites.get(suite_id)
