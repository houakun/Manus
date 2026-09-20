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
    """任务集形状是**约定**，不能随手变 —— 它直接决定指标的可比性。

    12 合成 + 8 半真实是方案 B 的原始约定；
    后来加了 `ci` 组（CI 失败归因），因为原来的 20 个任务已经**饱和**：
    7/7 regression 任务均 42 次运行 100% 成功 —— 成功率这个指标在那里不动，
    提不了任何改进信号（详见 docs/noise-floor-measured.md）。

    这个断言的价值就在于：任何人往任务集里加/删任务时，都必须到这里来显式改一次，
    而不是惄惄地让基线数字变得不可比。
    """
    tasks = load_all_tasks()
    summary = summarize_tasks(tasks)

    assert summary["by_group"] == {"semireal": 8, "synthetic": 12, "ci": 2}
    assert len(tasks) == 22


def test_ci_tasks_are_capability_not_regression():
    """新增的 ci 任务必须标 capability，直到我们测出它们的方差。

    为什么要单独钉住：`purpose` 直接决定它进不进回归门禁口径。
    一个成功率未知、方差未知的新任务类如果被当成 regression，
    会把回归指标变成一个“看着稳定实际上在抖”的数字。
    测出方差之后再改分类，是**显式的一次决定**，不是默认值。
    """
    ci_tasks = [t for t in load_all_tasks() if t.group == "ci"]
    assert ci_tasks, "ci 组不应为空"
    for task in ci_tasks:
        assert task.purpose == "capability", f"{task.uid} 的 purpose 应为 capability"


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


# ==================== 3.5 判定强度：防“静默失败”的回归测试 ====================

async def _workspace_with_fixtures(tmp_path, task):
    """按任务定义铺好 fixtures，返回沙箱。"""
    sandbox = LocalSandbox(tmp_path / "ws")
    await sandbox.ensure_sandbox()
    for fixture in task.fixtures:
        await sandbox.write_file(fixture.path, fixture.content)
    return sandbox


def _rename_task():
    return next(t for t in load_all_tasks() if t.key == "syn_rename_files")


async def test_rename_task_rejects_content_destroying_rename(tmp_path):
    """回归："删了再建空文件"必须被判失败。

    这是真实存在的判定缺口（当初只验了"文件名变了"）：
        rm a.txt && touch a.md
    原名文件确实不见了、新文件名确实在，**但内容已经丢了**。
    对"重命名"任务而言内容保留才是本质，所以必须验内容。
    这个用例就是用来锁住那条内容校验的 —— 删掉它就会退化成假过关。
    """
    task = _rename_task()
    sandbox = await _workspace_with_fixtures(tmp_path, task)

    # 破坏性"重命名"：文件名对了，内容全空
    for name in ("a", "b"):
        await sandbox.write_file(f"/home/ubuntu/docs/{name}.md", "")
        await sandbox.delete_file(f"/home/ubuntu/docs/{name}.txt")

    checks = await run_checks(task.verify, sandbox)
    assert not all(check.ok for check in checks), "内容丢失却判通过了（判定强度不足）"
    failed = "\n".join(check.detail for check in checks if not check.ok)
    assert "a.md" in failed or "b.md" in failed


async def test_rename_task_rejects_swapped_files(tmp_path):
    """回归：只数文件个数是不够的。

    删掉 notes.log 再建一个 extra.md → 文件数仍然是 4，
    dir_count 会通过，必须靠 no_extra_files 才能发现。
    """
    task = _rename_task()
    sandbox = await _workspace_with_fixtures(tmp_path, task)

    # 正常重命名（满足前面所有检查）
    for name, content in (("a", "alpha"), ("b", "beta")):
        await sandbox.write_file(f"/home/ubuntu/docs/{name}.md", content)
        await sandbox.delete_file(f"/home/ubuntu/docs/{name}.txt")
    # 但额外删了 notes.log、多建了 extra.md —— 文件数依然相等
    await sandbox.delete_file("/home/ubuntu/docs/notes.log")
    await sandbox.write_file("/home/ubuntu/docs/extra.md", "junk")

    checks = await run_checks(task.verify, sandbox)
    assert not all(check.ok for check in checks), "文件被掉包却判通过了"
    assert any(check.kind == "no_extra_files" and not check.ok for check in checks)


async def test_rename_task_accepts_the_reference_behavior(tmp_path):
    """反向确认：真正正确的做法（改名 + 保留内容 + 不动其它文件）必须判通过。

    没有这个反向用例，前面两个"拒绝对错答案"的测试可以靠"永远返回失败"蒙混过关。
    """
    task = _rename_task()
    sandbox = await _workspace_with_fixtures(tmp_path, task)

    for name, content in (("a", "alpha"), ("b", "beta")):
        await sandbox.write_file(f"/home/ubuntu/docs/{name}.md", content)
        await sandbox.delete_file(f"/home/ubuntu/docs/{name}.txt")

    checks = await run_checks(task.verify, sandbox)
    assert all(check.ok for check in checks), [c.line() for c in checks if not c.ok]


# ==================== 3.6 任务集自检的"牙齿" ====================

async def test_task_set_has_no_tautological_validation():
    """所有任务的参考解都不应该"直接把期望值写出来"。

    这是那次事故（两个任务的期望值写错、自检却全过）的直接防护：
    参考解只要真的算一遍，期望值写错时自检就会失败。
    """
    from lab.bench.validate import _tautology_flags

    offenders = {task.uid: _tautology_flags(task) for task in load_all_tasks()}
    flagged = {uid: flags for uid, flags in offenders.items() if flags}
    assert flagged == {}, f"以下任务的验证是同义反复（期望值的正确性没被独立验证）：{flagged}"


async def test_validation_detects_a_wrong_expectation(tmp_path):
    """**元测试**：自检必须能发现"期望值本身写错了"。

    做法：把一个任务的期望值故意改错，然后跑自检 —— 必须失败。
    这条测试证明了整个任务集可信度的基础：
    如果连它都过不了，就说明自检又退回了"只能验证判定器能读回自己写的东西"。
    """
    from lab.bench.validate import validate_task

    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    mutated = task.model_copy(deep=True)
    mutated.verify[0].value = 999_999  # 故意写错（真实事故就是这类错误）

    result = await validate_task(mutated, tmp_path)

    assert not result.ok, "期望值写错了，自检却没发现 —— 任务集的可信度前提被破坏"
    reference_step = next(s for s in result.steps if s.name == "reference_solution")
    assert reference_step.actual_pass is False


async def test_validation_detects_a_wrong_expectation_in_content_task(tmp_path):
    """同上，但针对文本类任务（`syn_string_reverse` 的真实事故形态）。"""
    from lab.bench.validate import validate_task

    task = next(t for t in load_all_tasks() if t.key == "syn_string_reverse")
    mutated = task.model_copy(deep=True)
    mutated.verify[0].equals = "WRONG ANSWER"  # 就像当初把 SUNAM 写成 SUNUM

    result = await validate_task(mutated, tmp_path)

    assert not result.ok


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


# ==================== 3.7 Step 5：配对对比与 Pareto ====================

def _fake_suite(suite_id: str, ok_flags, tokens_list, cost=0.1):
    from lab.bench.runner import RunOutcome, SuiteResult
    outcomes = [
        RunOutcome(
            run_id=f"{suite_id}-{i}", suite_id=suite_id, task_uid=f"synthetic/t{i}", task_key=f"t{i}",
            group="synthetic", run_index=0, ok=ok_flags[i], tokens=tokens_list[i],
            cost_usd=cost, elapsed_ms=1000, tool_calls=5, steps_done=1,
        )
        for i in range(len(ok_flags))
    ]
    return SuiteResult(suite_id=suite_id, label=suite_id, outcomes=outcomes)


def test_paired_comparison_removes_task_difficulty_variance():
    """配对对比要消掉任务难度差异：差值应按**任务**配对，而不是比两组均值。"""
    from lab.bench.compare import compare_suites

    # 任务难度差别极大（t2 贵 10 倍），但两个方案**每个任务**都只差 20 次
    baseline = _fake_suite("a", [True, True, True], [1000, 1000, 10000])
    candidate = _fake_suite("b", [True, True, True], [800, 800, 8000])

    report = compare_suites(baseline, candidate)
    paired = report.paired["tokens"]

    assert paired.n_pairs == 3
    # 差值分别是 -200 / -200 / -2000（t2 贵 10 倍，所以绝对差也大 10 倍）
    assert paired.mean_diff == pytest.approx(-800.0)
    # 不变量是**相对变化**：每个任务都恰好 -20% ——
    # 这才证明了"难度差异被消掉"（如果是简单地比两组总均值，
    # 结果会被 t2 这种贵任务的绝对量纲主导）
    assert paired.relative_change == pytest.approx(-0.2, abs=0.01)
    assert paired.wins == 3 and paired.losses == 0
    assert paired.significant is True  # 差值恒定 → 区间不跨 0


def test_paired_comparison_reports_when_direction_is_unclear():
    """方向不一致时不能说“更优”——区间跨 0 必须如实标注。"""
    from lab.bench.compare import compare_suites

    baseline = _fake_suite("a", [True, True], [1000, 1000])
    candidate = _fake_suite("b", [True, True], [500, 1500])  # 一优一劣

    report = compare_suites(baseline, candidate)
    paired = report.paired["tokens"]

    assert paired.wins == 1 and paired.losses == 1
    assert paired.significant is False  # 方向不明


def test_pareto_only_compares_same_task_set():
    """Pareto 必须按任务集分组 —— 不同任务集/规模的点不可比。"""
    from lab.bench.compare import group_by_task_set

    small = _fake_suite("small", [True], [1000])          # n=1，只有 1 个任务
    big = _fake_suite("big", [True, True, True], [1000, 1000, 1000])  # n=3，3 个任务

    groups = group_by_task_set([small, big], min_runs=3)
    assert list(groups) == [("synthetic/t0", "synthetic/t1", "synthetic/t2")]  # 只留下 big


def test_pareto_frontier_marks_dominated_points():
    from lab.bench.compare import pareto_points

    better = _fake_suite("better", [True, True], [100, 100], cost=0.05)
    worse = _fake_suite("worse", [True, False], [100, 100], cost=0.10)

    points = {p.suite_id: p for p in pareto_points([better, worse])}

    assert points["better"].on_frontier is True
    assert points["worse"].dominated_by == "better"


def test_bootstrap_interval_widens_with_spread():
    """bootstrap 区间要反映离散程度（重尾数据下比 t 区间更可信）。"""
    from lab.bench.stats import bootstrap_ci

    tight = bootstrap_ci([100, 101, 99, 100, 100])
    wide = bootstrap_ci([10, 50, 100, 200, 900])

    assert tight.half_width < wide.half_width
    assert wide.point > tight.point


# ==================== 3.8 任务级指标 pass^k / pass@k ====================

def test_pass_k_exposes_instability_that_run_level_rate_dilutes():
    """`pass^k` 的核心价值：把"少数任务不稳定"暴露出来，而不是被平摊掉。

    真实数据：semireal v1 运行级成功率 95.0%（38/40），看起来与 100% 差不多；
    但 8 个任务里有 2 个是 4/5 → pass^5 = 6/8 = 75%。
    """
    from lab.bench.stats import task_level_rates

    per_task = {
        "t1": [True] * 5, "t2": [True] * 5, "t3": [True] * 5,
        "t4": [True, True, True, True, False],  # 4/5
        "t5": [True] * 5, "t6": [True] * 5, "t7": [True] * 5,
        "t8": [False, True, True, True, True],  # 4/5
    }
    metrics = task_level_rates(per_task)

    assert metrics.tasks == 8 and metrics.k == 5
    assert metrics.pass_pow_k == 6  # 只有 6 个任务 5 次全对
    assert metrics.pass_pow_k_rate.point == pytest.approx(0.75)
    assert metrics.pass_at_k == 8  # 但每个任务都至少成功过一次
    assert len(metrics.unstable) == 2
    # 运行级口径会把这个差异平摊成 95%
    assert sum(sum(v) for v in per_task.values()) / 40 == pytest.approx(0.95)


def test_task_level_rates_rejects_bare_sequences():
    """防呆：传裸序列必须报错，不能静默算出错误的 k。

    真事故：初版签名是 Sequence，调用方传了 dict →
    迭代得到键（字符串）→ `map(bool, "semireal/sem_bug_fix")` 把 20 个字符
    变成 20 个 True → k=20。**没有异常，只是数字悄悄错了。**
    """
    from lab.bench.stats import task_level_rates

    with pytest.raises(TypeError, match="Mapping"):
        task_level_rates(["semireal/sem_bug_fix"])


def test_task_level_rates_flags_non_uniform_k():
    """各任务次数不一致时要标出来 —— 它会低估实际稳定性。"""
    from lab.bench.stats import task_level_rates

    metrics = task_level_rates({"t1": [True] * 5, "t2": [True] * 3})
    assert metrics.uniform_k is False
    assert metrics.k == 3  # 取最小值
    assert any("不一致" in note for note in metrics.caveats)
