#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""harness 连通性测试：**不花钱**地验证"执行器 → 判定器 → 落库 → 报告"整条链路。

== 为什么必须有这个文件 ==
`test_bench.py` 测的是零件（统计公式、判定规则、任务模型）；
本文件测的是**装配**：真跑一遍 `run_suite`，看它能不能把任务、SUT、
判定器、统计、存储、报告串成一条可用的流水线。

用脚本化 LLM 代替真模型，所以：
- 不需要 API Key，也不花钱；
- 结果确定：**同一个脚本必然产生同一个判定结论**，可以精确断言。

== 这里验证了两种互补的场景 ==
1. **失败路径**：LLM 完全不可用 → 所有任务判定失败，但 harness 不能崩，
   报告仍能生成（否则一次网络抖动就会毁掉整份评测）。
2. **成功路径 + 硬编码检测**：让 SUT "直接把答案写进文件" →
   任务判定**通过**，但同时被过程规则标记为 `hardcoded_answer`。
   这一条同时证明了"结果分"和"过程分"确实是两个独立维度。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.app_config import AgentConfig  # noqa: E402

from lab.bench.report import render_report, render_suite_list  # noqa: E402
from lab.bench.runner import run_suite  # noqa: E402
from lab.tests.fakes import ScriptedLLM, planner_react_script  # noqa: E402
from lab.trace.store import SpanStore  # noqa: E402


@pytest.fixture()
def no_sleep(monkeypatch):
    """把 Agent 的重试间隔压到 0：测试不需要真等 1 秒。"""
    from app.domain.services.agents.base import BaseAgent

    monkeypatch.setattr(BaseAgent, "_retry_interval", 0.01)


def _wire_fake_llm(monkeypatch, tmp_path, script):
    """把真 LLM 换成脚本化 LLM，并把轨迹库指到临时目录。"""
    from lab import api as lab_api

    monkeypatch.setenv("LAB_LLM_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(lab_api, "OpenAILLM", lambda config: ScriptedLLM(list(script)))
    # Agent 配置收敛，避免测试跑很多轮
    monkeypatch.setattr(
        lab_api, "load_agent_config",
        lambda **kwargs: AgentConfig(max_iterations=4, max_retries=2, max_search_results=3),
    )
    db_path = tmp_path / "traces.db"
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: SpanStore(db_path))
    return db_path


def test_failed_run_keeps_harness_alive_and_report_generatable(tmp_path, monkeypatch, no_sleep):
    """失败路径：LLM 完全不可用 → 全部判定失败，但流水线必须跑完并出报告。"""
    db_path = _wire_fake_llm(monkeypatch, tmp_path, [])  # 空脚本 → 每次调用都失败

    suite = asyncio.run(run_suite(
        runs_per_task=1, limit=2, bench_root=tmp_path / "bench", progress=False,
    ))

    assert suite.total_runs == 2
    assert suite.successes == 0
    # Wilson 区间：0/2 的上界远小于 1，不能让人误读成"成功率未知但可能很高"
    assert suite.success_rate.high < 1.0
    # 指标必须算出来（即使全是 0）—— 报告里不能出现空白单元格
    assert suite.tokens.n == 2
    assert suite.cost.n == 2

    report = render_report(suite)
    assert "总体指标" in report
    assert "Wilson" in report
    assert suite.suite_id in report

    # 落库 + 重建（报告要能在不重跑的前提下再生成一次）
    store = SpanStore(db_path)
    store.save_suite(suite)
    loaded = store.load_suite(suite.suite_id)
    assert loaded is not None
    assert loaded.total_runs == 2
    assert loaded.successes == 0
    assert "总体指标" in render_report(loaded)
    assert suite.suite_id[:8] in render_suite_list(store.list_suites())


def test_success_path_and_hardcode_detection(tmp_path, monkeypatch, no_sleep):
    """成功路径：SUT 直接写出答案 → 结果分通过，但过程分标记为硬编码。

    这条测试同时锁住两件事：
    1. 判定器在"产物正确"时确实会放行（不是永远失败）；
    2. 过程分与结果分是两个独立维度（handoff 决策 5）。
    """
    # 让脚本化 LLM 直接调用 write_file 写出正确答案 "500500"
    script = planner_react_script(
        tool_name="write_file",
        tool_args={"filepath": "/home/ubuntu/sum.txt", "content": "500500"},
        final_message="已经计算完成",
    )
    _wire_fake_llm(monkeypatch, tmp_path, script)

    suite = asyncio.run(run_suite(
        runs_per_task=1, keys=["syn_sum_range"], bench_root=tmp_path / "bench", progress=False,
    ))

    assert suite.total_runs == 1
    outcome = suite.outcomes[0]

    # 1.结果分：产物满足 numeric 判定
    assert outcome.ok is True
    assert outcome.failed_checks == []
    # 2.过程分：答案字面量直接出现在写入内容里 → 硬编码可疑
    assert any(flag.startswith("hardcoded_answer") for flag in outcome.process_flags)
    assert suite.process_flag_counts().get("hardcoded_answer") == 1


def test_suite_isolates_workspaces_between_runs(tmp_path, monkeypatch, no_sleep):
    """每次 run 必须有独立工作区。

    否则上一次写对的文件会让下一次"什么都没做也通过" —— 成功率会虚高，
    而且这种污染完全不会报错，是最难发现的实验错误。
    """
    script = planner_react_script(
        tool_name="write_file",
        tool_args={"filepath": "/home/ubuntu/sum.txt", "content": "500500"},
        final_message="done",
    )
    _wire_fake_llm(monkeypatch, tmp_path, script + script)  # 两次运行，两次都能成功

    suite = asyncio.run(run_suite(
        runs_per_task=2, keys=["syn_sum_range"], bench_root=tmp_path / "bench", progress=False,
    ))

    assert suite.total_runs == 2
    workspaces = {outcome.workspace for outcome in suite.outcomes}
    assert len(workspaces) == 2  # 两次运行落在两个不同目录
    assert all(outcome.ok for outcome in suite.outcomes)


def test_recover_rebuilds_suite_from_workspaces(tmp_path, monkeypatch, no_sleep):
    """`recover` 的验收：模拟"落库丢失"后能从工作区重建出**一致**的结论。

    这个能力在真正被 Ctrl-C 打断时是救数据的唯一手段（写它的时候我误判了一次事故，
    但能力本身是对的）。这里锁住两件事：
    1. 重建出的判定结论与原始一致（判定器是纯函数）；
    2. 指标（token/ok）能从轨迹库里正确反查回来。
    """
    from lab.bench.recover import recover_suite
    from lab.bench.runner import run_suite as real_run_suite

    db_path = _wire_fake_llm(monkeypatch, tmp_path, planner_react_script(
        tool_name="write_file",
        tool_args={"filepath": "/home/ubuntu/a.txt", "content": "alpha"},
        final_message="done",
    ))

    bench_root = tmp_path / "bench"
    original = asyncio.run(real_run_suite(
        runs_per_task=1, keys=["syn_sort_numbers"], bench_root=bench_root, progress=False,
    ))
    assert original.total_runs == 1
    original_outcome = original.outcomes[0]

    # 落库（用增量接口，与 CLI 一致），然后模拟"库里的评定结果丢失"
    store = SpanStore(db_path)
    store.save_suite_header(original)
    store.save_outcome(original_outcome)
    store.finalize_suite(original)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM bench_runs WHERE suite_id = ?", (original.suite_id,))
    assert store.load_suite(original.suite_id).outcomes == []

    # 从工作区 + 轨迹重建
    recovered = asyncio.run(recover_suite(
        bench_root=bench_root, store=store,
        suite_id="recovered-1", label="重建", group="synthetic",
    ))

    assert len(recovered.outcomes) == 1
    rebuilt = recovered.outcomes[0]
    assert rebuilt.ok == original_outcome.ok
    assert rebuilt.tokens == original_outcome.tokens
    assert rebuilt.workspace == original_outcome.workspace
    # 重建结果也进了库
    assert len(store.load_suite("recovered-1").outcomes) == 1

    # 分组过滤：指定 semireal 时不应把 synthetic 的工作区扫进来
    # （这个 bug 真实存在过：重建 semireal 的数据时混进了 synthetic 的工作区）
    empty = asyncio.run(recover_suite(
        bench_root=bench_root, store=store,
        suite_id="recovered-2", label="空", group="semireal",
    ))
    assert empty.outcomes == []


def test_budget_observation_is_recorded_in_outcomes(tmp_path, monkeypatch, no_sleep):
    """observe 模式下预算越界要进 outcome（切 enforce 的决策依据就是这个数据）。

    这里刻意让阈值落在"超过软阈值、但没到硬上限"的区间：
    只有这样同时验证两件事 —— 越界被记录（软线），且"本会中止"为假（硬线）。
    如果硬上限也超了，那就变成在测 enforce 的后果，而不是 observe 的记录能力。
    """
    from lab.guard.budget import BudgetPolicy

    script = planner_react_script(
        tool_name="write_file",
        tool_args={"filepath": "/home/ubuntu/sum.txt", "content": "500500"},
        final_message="done",
    )
    _wire_fake_llm(monkeypatch, tmp_path, script)

    # 脚本化 LLM 每次消耗 120 token、共 5 次 = 600；
    # 软阈值 300（会被突破），硬上限 300×2.5=750（不会）
    suite = asyncio.run(run_suite(
        runs_per_task=1, keys=["syn_sum_range"], bench_root=tmp_path / "bench", progress=False,
        budget_policy=BudgetPolicy(max_tokens=300),
    ))

    outcome = suite.outcomes[0]
    assert outcome.ok is True  # observe 不干预：任务照常成功
    assert outcome.tokens > 300
    assert "tokens" in outcome.budget_violated  # 软阈值被记录
    stats = suite.budget_stats()
    assert stats["violated_runs"] == 1
    assert stats["would_stop_runs"] == 0  # 但没碰硬上限 → enforce 下不会被砍
    assert stats["by_metric"] == {"tokens": 1}
