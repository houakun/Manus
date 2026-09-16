#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step 4 测试：统计 / 判定规则 / 任务模型 / 任务集自检 / harness 连通性。"""

from __future__ import annotations

import pytest

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from lab.bench.stats import (  # noqa: E402
    Interval,
    aggregate,
    compare_paired,
    mean,
    mean_ci,
    stdev,
    t_critical,
    wilson_interval,
)
from lab.bench.task import BenchTask, ProcessRule, VerifyCheck, load_all_tasks, summarize_tasks  # noqa: E402
from lab.bench.verifier import evaluate_process, normalize_text, run_checks  # noqa: E402
from lab.infra.local_sandbox import LocalSandbox  # noqa: E402
from lab.sut.base import StepStats, TaskResult  # noqa: E402
from lab.usage import Usage  # noqa: E402


# ==================== 1. 统计 ====================

def test_mean_and_stdev():
    assert mean([1, 2, 3]) == 2
    assert mean([]) == 0.0
    assert stdev([1]) == 0.0
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.138, abs=0.001)


def test_t_critical_degrades_honestly():
    """非 95% 时退回正态近似，而不是假装有精确表。"""
    assert t_critical(5) == pytest.approx(2.571)
    assert t_critical(100) == pytest.approx(1.96)
    assert t_critical(0) == float("inf")  # n=1 无法估方差 → 区间应无穷宽


def test_mean_ci_requires_two_samples():
    """n=1 时**不能**伪造成 ±0 —— 那等于宣称\"我这一次跑出来就是真值\"。"""
    single = mean_ci([42.0])
    assert single.point == 42.0
    assert single.low is None and single.high is None
    assert single.method == "none"

    multi = mean_ci([1.0, 2.0, 3.0, 4.0, 5.0])
    assert multi.method == "t95"
    assert multi.low < multi.point < multi.high
    assert multi.n == 5


def test_wilson_interval_is_not_absurd_at_extremes():
    """这正是用 Wilson 而不是正态近似的原因：极端比例下区间不能越界/归零。"""
    # 5 次全成功：正态近似会给 ±0（宣称"不可能失败"），Wilson 诚实地给出宽区间
    all_success = wilson_interval(5, 5)
    assert all_success.point == 1.0
    assert all_success.high == 1.0
    assert all_success.low == pytest.approx(0.566, abs=0.01)

    # 5 次全失败
    all_fail = wilson_interval(0, 5)
    assert all_fail.point == 0.0
    assert all_fail.low == 0.0
    assert all_fail.high == pytest.approx(0.434, abs=0.01)

    # 区间必须始终落在 [0,1] 内
    for successes in range(0, 11):
        interval = wilson_interval(successes, 10)
        assert 0.0 <= interval.low <= interval.point <= interval.high <= 1.0


def test_wilson_interval_narrows_with_more_samples():
    """样本越多，区间越窄 —— 这是"n=5 不够"这句话的量化表达。"""
    narrow = wilson_interval(15, 20).half_width
    wide = wilson_interval(3, 4).half_width
    assert narrow < wide


def test_compare_paired_counts_wins_and_losses():
    """配对比较要能回答"新方案赢在几个任务上"，而不只是均值差异。"""
    baseline = {"t1": 100.0, "t2": 200.0, "t3": 50.0}
    candidate = {"t1": 80.0, "t2": 210.0, "t3": 50.0}  # t1 更省、t2 更贵、t3 持平

    result = compare_paired("tokens", baseline, candidate, lower_is_better=True)

    assert result.n_pairs == 3
    assert result.wins == 1  # t1
    assert result.losses == 1  # t2
    assert result.ties == 1  # t3
    assert result.mean_diff == pytest.approx((80 - 100 + 210 - 200 + 0) / 3)


def test_aggregate_reports_spread_not_just_mean():
    data = aggregate([10, 20, 30, 40, 100])
    assert data["n"] == 5
    assert data["mean"] == 40
    assert data["min"] == 10 and data["max"] == 100
    assert data["sd"] > 0
    assert data["p95"] == 100


# ==================== 2. 判定规则 ====================

def test_normalize_modes():
    assert normalize_text("  x  ", "strip") == "x"
    assert normalize_text("  x  ", "none") == "  x  "
    assert normalize_text("AbC", "lower") == "abc"
    assert normalize_text("a   b\n c", "collapse_ws") == "a b c"
    assert normalize_text("b\na\nc", "sorted_lines") == "a\nb\nc"


async def _sandbox(tmp_path) -> LocalSandbox:
    sandbox = LocalSandbox(tmp_path / "ws")
    await sandbox.ensure_sandbox()
    return sandbox


async def test_check_file_content_and_normalize(tmp_path):
    sandbox = await _sandbox(tmp_path)
    await sandbox.write_file("/home/ubuntu/a.txt", "hello\n")

    # 默认 strip：结尾换行不应该导致失败（否则会制造大量假阴性）
    ok = await run_checks([VerifyCheck(kind="file_content", path="/home/ubuntu/a.txt", equals="hello")], sandbox)
    assert ok[0].ok is True

    bad = await run_checks([VerifyCheck(kind="file_content", path="/home/ubuntu/a.txt", equals="world")], sandbox)
    assert bad[0].ok is False
    assert "期望" in bad[0].detail and "实际" in bad[0].detail  # 必须给出证据


async def test_check_contains_not_contains_and_regex(tmp_path):
    sandbox = await _sandbox(tmp_path)
    await sandbox.write_file("/home/ubuntu/a.py", "port = config['port']\n")

    results = await run_checks([
        VerifyCheck(kind="file_content", path="/home/ubuntu/a.py", contains="config"),
        VerifyCheck(kind="file_content", path="/home/ubuntu/a.py", not_contains="8080"),
        VerifyCheck(kind="file_content", path="/home/ubuntu/a.py", matches=r"port\s*=\s*\w+"),
    ], sandbox)
    assert all(r.ok for r in results)

    negative = await run_checks([
        VerifyCheck(kind="file_content", path="/home/ubuntu/a.py", not_contains="config"),
    ], sandbox)
    assert negative[0].ok is False


async def test_check_numeric_and_json(tmp_path):
    sandbox = await _sandbox(tmp_path)
    await sandbox.write_file("/home/ubuntu/n.txt", "  500500 \n")
    await sandbox.write_file("/home/ubuntu/j.json", '{"status": {"200": 3}}')

    results = await run_checks([
        VerifyCheck(kind="numeric", path="/home/ubuntu/n.txt", value=500500),
        VerifyCheck(kind="json_equals", path="/home/ubuntu/j.json", equals='{"status": {"200": 3}}'),
    ], sandbox)
    assert all(r.ok for r in results)

    # 非数字内容必须报"无法解析"而不是崩掉
    await sandbox.write_file("/home/ubuntu/bad.txt", "not a number")
    bad = await run_checks([VerifyCheck(kind="numeric", path="/home/ubuntu/bad.txt", value=1)], sandbox)
    assert bad[0].ok is False and "无法解析" in bad[0].detail


async def test_check_csv_rows_is_order_sensitive(tmp_path):
    sandbox = await _sandbox(tmp_path)
    await sandbox.write_file("/home/ubuntu/c.csv", "id,item\n2,b\n1,a\n")

    same = await run_checks([VerifyCheck(
        kind="csv_rows", path="/home/ubuntu/c.csv",
        rows=[["id", "item"], ["2", "b"], ["1", "a"]],
    )], sandbox)
    assert same[0].ok is True

    reordered = await run_checks([VerifyCheck(
        kind="csv_rows", path="/home/ubuntu/c.csv",
        rows=[["id", "item"], ["1", "a"], ["2", "b"]],
    )], sandbox)
    assert reordered[0].ok is False  # 行序有语义时不能放过


async def test_check_missing_file_fails_with_clear_detail(tmp_path):
    sandbox = await _sandbox(tmp_path)
    result = await run_checks([VerifyCheck(kind="file_content", path="/home/ubuntu/nope.txt", equals="x")], sandbox)
    assert result[0].ok is False
    assert "读取失败" in result[0].detail


async def test_check_exceptions_are_contained(tmp_path):
    """判定器自己的异常要记成"失败 + 说明原因"，不能把 harness 带走。"""
    sandbox = await _sandbox(tmp_path)
    await sandbox.write_file("/home/ubuntu/a.txt", "x")

    # 未知检查类型 → 返回失败而不是抛异常
    result = await run_checks([VerifyCheck(kind="unknown_kind", path="/home/ubuntu/a.txt")], sandbox)
    assert result[0].ok is False and "未知的检查类型" in result[0].detail

    # 非法正则 → 也要被兜住
    result = await run_checks([VerifyCheck(
        kind="file_content", path="/home/ubuntu/a.txt", matches="[unclosed"
    )], sandbox)
    assert result[0].ok is False


async def test_check_file_exists_and_dir_count(tmp_path):
    sandbox = await _sandbox(tmp_path)
    await sandbox.write_file("/home/ubuntu/docs/a.md", "a")
    await sandbox.write_file("/home/ubuntu/docs/b.md", "b")

    results = await run_checks([
        VerifyCheck(kind="file_exists", path="/home/ubuntu/docs/a.md", exists=True),
        VerifyCheck(kind="file_exists", path="/home/ubuntu/docs/a.txt", exists=False),
        VerifyCheck(kind="dir_count", path="/home/ubuntu/docs", count=2),
        VerifyCheck(kind="no_extra_files", path="/home/ubuntu/docs", allow=["a.md", "b.md"]),
    ], sandbox)
    assert all(r.ok for r in results)

    extra = await run_checks([
        VerifyCheck(kind="no_extra_files", path="/home/ubuntu/docs", allow=["a.md"]),
    ], sandbox)
    assert extra[0].ok is False


# ==================== 3. 任务模型与任务集 ====================

def test_task_set_shape():
    """任务集规模必须是 12 合成 + 8 半真实（这是方案 B 的约定）。"""
    tasks = load_all_tasks()
    summary = summarize_tasks(tasks)

    assert summary["by_group"] == {"semireal": 8, "synthetic": 12}
    assert len(tasks) == 20


def test_every_task_has_verifier_solution_and_wrong_answer():
    """每个任务都必须有判定规则 + 参考解 + 错误解，否则自检无从谈起。"""
    for task in load_all_tasks():
        assert task.verify, f"{task.uid} 缺少 verify"
        assert task.solution, f"{task.uid} 缺少参考解"
        assert task.wrong, f"{task.uid} 缺少错误解"
        assert task.goal.strip(), f"{task.uid} 目标为空"


def test_task_keys_are_unique():
    keys = [task.uid for task in load_all_tasks()]
    assert len(keys) == len(set(keys))


# ==================== 4. 过程规则 ====================

def _result_with_guard(guard: dict, **kwargs) -> TaskResult:
    usage = Usage()
    usage.add_llm_call({"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}, 0)
    base = dict(
        task_id="t", sut_name="fake", ok=True, llm_usage=usage,
        steps=StepStats(total=1, done=1, succeeded=1), guard=guard,
    )
    base.update(kwargs)
    return TaskResult(**base)


def test_process_rules_flag_efficiency_security_and_cheating():
    task = BenchTask(
        key="t", group="synthetic", goal="g",
        verify=[VerifyCheck(kind="file_exists", path="/x")],
        process=ProcessRule(
            max_tool_calls=3, max_llm_calls=0, max_steps=0,
            forbid_path_escape=True, answer_literals=["42"],
        ),
    )
    result = _result_with_guard({
        "loop": {"tool_calls": 10.0, "action_diversity": 0.2},
        "error_types": {"path_escape": 1},
        "hardcode_suspects": {"/home/ubuntu/a.txt": ["42"]},
    })

    flags = evaluate_process(task, result)

    assert any(f.startswith("too_many_tool_calls") for f in flags)
    assert any(f.startswith("too_many_llm_calls") for f in flags)
    assert any(f.startswith("too_many_steps") for f in flags)
    assert "path_escape_attempted" in flags
    assert any(f.startswith("hardcoded_answer") for f in flags)
    assert any(f.startswith("low_action_diversity") for f in flags)


def test_process_rules_are_quiet_on_healthy_runs():
    task = BenchTask(
        key="t", group="synthetic", goal="g",
        verify=[VerifyCheck(kind="file_exists", path="/x")],
        process=ProcessRule(max_tool_calls=20, max_llm_calls=20, max_steps=10),
    )
    result = _result_with_guard({"loop": {"tool_calls": 5.0, "action_diversity": 0.9}})
    assert evaluate_process(task, result) == []


def test_process_flags_do_not_change_result_score():
    """过程问题不影响 `ok` —— 结果分与过程分必须分开（handoff 决策 5）。"""
    task = BenchTask(
        key="t", group="synthetic", goal="g",
        verify=[VerifyCheck(kind="file_exists", path="/x")],
        process=ProcessRule(max_tool_calls=1),
    )
    result = _result_with_guard({"loop": {"tool_calls": 99.0}})

    flags = evaluate_process(task, result)
    assert flags  # 有过程问题
    assert result.ok is True  # 但结果分不受影响
