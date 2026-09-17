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
    """只实现 CLI 用到的那几个方法的内存替身。

    注意要包含**增量落库**接口（save_suite_header / save_outcome / finalize_suite）：
    CLI 会把这三个方法作为回调传给 run_suite —— 即使 run_suite 被替换掉，
    属性查找也会在构造 kwargs 时立刻发生（这正是这条测试能抓到漏 import 的原因）。
    """

    def __init__(self) -> None:
        self.suites = {}

    def save_suite(self, suite) -> None:
        self.suites[suite.suite_id] = suite

    def save_suite_header(self, suite) -> None:
        self.suites.setdefault(suite.suite_id, suite)

    def save_outcome(self, outcome) -> None:
        suite = self.suites.get(outcome.suite_id)
        if suite is not None:
            suite.outcomes = [o for o in suite.outcomes if o.run_id != outcome.run_id]
            suite.outcomes.append(outcome)

    def finalize_suite(self, suite) -> None:
        self.suites[suite.suite_id] = suite

    def avg_cost_for_group(self, group: str):
        """CLI 的预算预估会读它；没有历史时返回 None（走保守默认值）。"""
        return None

    def load_runs_grouped_by_task(self, limit_per_task: int = 500):
        """CLI 的阈值推导会读它；替身里返回空 → 退回默认阈值。"""
        return {}

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


def test_cli_sandbox_clean_escapes_is_dry_run_by_default(tmp_path, monkeypatch):
    """`sandbox clean-escapes` 默认只报告、不删 —— 它在删工作区**外面**的东西。

    这是 fast mode 的那个真实缺陷（脚本用绝对路径 → 产物落到 `<盘>:/home/ubuntu`）
    对应的清理命令。实测一天就积了 15MB（含两个 5MB 的 zip 和 24 个分片文件），
    所以清理必须是可重复的命令。
    """
    import lab.infra.local_sandbox as sandbox_module

    fake_root = tmp_path / "home" / "ubuntu"
    fake_root.mkdir(parents=True)
    (fake_root / "junk.bin").write_bytes(b"x" * 1024)
    monkeypatch.setattr(sandbox_module, "escape_roots", lambda: [fake_root])

    # 1.dry-run：只报告
    assert main(["sandbox", "clean-escapes"]) == 0
    assert fake_root.exists(), "dry-run 不应该删除任何东西"

    # 2.--yes：真的删掉，并把空的父目录也一起清掉
    assert main(["sandbox", "clean-escapes", "--yes"]) == 0
    assert not fake_root.exists()
    assert not (tmp_path / "home").exists()  # 空父目录也清掉


def test_cli_sandbox_clean_reports_nothing_when_clean(monkeypatch):
    import lab.infra.local_sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module, "escape_roots", lambda: [])
    assert main(["sandbox", "clean-escapes", "--yes"]) == 0
