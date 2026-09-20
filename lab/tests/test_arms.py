#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交错 A/B、噪声地板、实验条件落库的测试。

== 这个文件要守住的三件事 ==
1. **交错顺序不能被静默改掉**：交错是"分离时间漂移"的唯一手段。
   有人重构一下循环、把轮转去掉，所有数字看起来仍然正常 ——
   所以顺序本身必须有直接的单测。
2. **两个臂绝不能共用工作区**：共用会让两个臂写同一份产物，
   成功率因此虚高，而且**不会报错**。这是这个项目里最贵的一类 bug。
3. **不同实验条件不能当同一次实验比**：拿 `--guard none` 的结果去比
   `--guard all` 的结果，差异根本不是"代码回归"。不显式报出来，
   这个错误会被读成结论。
"""

from __future__ import annotations

import json

import pytest

from lab.bench.noise import (
    NoiseFloor,
    load_noise_floor,
    measure_noise_floor,
    render_noise_floor,
    save_noise_floor,
)
from lab.bench.runner import (
    ArmConfig,
    RunOutcome,
    SuiteResult,
    arm_slug,
    build_schedule,
    fault_spec_of,
    summarize_task,
)
from lab.bench.task import load_all_tasks
from lab.faults.kinds import FaultKind, FaultRule


# ==================== 1. 交错顺序 ====================

def test_schedule_interleaves_and_rotates():
    """两臂时按轮次交替，且每轮把顺序轮转一位（抵消"谁总是先跑"）。"""
    tasks = ["t1", "t2"]
    arms = [ArmConfig(label="A"), ArmConfig(label="B")]
    schedule = build_schedule(tasks, 2, arms, interleave=True)

    order = [(task, index, arm.label) for task, index, arm in schedule]
    assert order == [
        ("t1", 0, "A"), ("t2", 0, "A"),
        ("t1", 0, "B"), ("t2", 0, "B"),
        ("t1", 1, "B"), ("t2", 1, "B"),   # ← 第二轮 A/B 顺序反过来
        ("t1", 1, "A"), ("t2", 1, "A"),
    ]


def test_schedule_single_arm_is_unchanged_from_before():
    """单臂时调度顺序必须与改造前逐项一致（向后兼容的历史数据仍可比）。"""
    tasks = ["t1", "t2"]
    schedule = build_schedule(tasks, 3, [ArmConfig(label="all")], interleave=True)
    assert [(t, i) for t, i, _ in schedule] == [
        ("t1", 0), ("t1", 1), ("t1", 2), ("t2", 0), ("t2", 1), ("t2", 2),
    ]


def test_schedule_no_interleave_groups_by_arm_only_when_asked():
    """显式关掉交错时才按臂分组（对照组，用来演示时间混淆）。"""
    tasks = ["t1"]
    arms = [ArmConfig(label="A"), ArmConfig(label="B")]
    schedule = build_schedule(tasks, 2, arms, interleave=False)
    assert [arm.label for _, _, arm in schedule] == ["A", "A"]


def test_every_run_index_covers_every_arm_exactly_once_per_round():
    """不变式：每一轮里每个臂的每个任务都恰好出现一次。

    这条比"具体顺序"更本质 —— 顺序可以调整，但"每轮覆盖完整"不能破，
    否则某个臂会少样本，而少样本的表现只是"区间变宽"，不会报错。
    """
    tasks = ["t1", "t2", "t3"]
    arms = [ArmConfig(label=f"arm{i}") for i in range(3)]
    schedule = build_schedule(tasks, 4, arms, interleave=True)

    for index in range(4):
        keys = sorted((task, arm.label) for task, i, arm in schedule if i == index)
        assert keys == sorted((task, arm.label) for task in tasks for arm in arms)


def test_arm_slug_is_consistent_between_metadata_and_directory():
    """`for_arm(label)` 必须能在目录 slug 上找到对应记录。

    两处如果用了不同的规范化规则，拆臂会**安静地拆出空集** ——
    看起来就像"那个臂全失败了"。
    """
    arm = ArmConfig(label="guard=none / fault=partial_write")
    assert arm.slug() == arm_slug(arm.label)
    assert "=" not in arm.slug() and "/" not in arm.slug()


# ==================== 2. 拆臂与实验条件落库 ====================

def _arm_suite(arms=("a", "b"), runs=2, ok_pattern=None):
    """构造一个交错 suite 的替身（不跑模型）。"""
    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    outcomes = []
    counter = 0
    for index in range(runs):
        for arm_label in arms:
            counter += 1
            ok = True if ok_pattern is None else ok_pattern(arm_label, index)
            outcomes.append(RunOutcome(
                run_id=f"{arm_label}-{index}", suite_id="s1", task_uid=task.uid,
                task_key=task.key, group=task.group, run_index=index, ok=ok,
                tokens=1000, cost_usd=0.001, elapsed_ms=5000, tool_calls=3, steps_done=1,
                guard_config="none" if arm_label == "a" else "all", arm=arm_label,
                fault_spec="partial_write@write_file(rate=0.5)",
            ))
    return SuiteResult(
        suite_id="s1", label="交错实验", model_name="m", runs_per_task=runs,
        guard_config="mixed", fault_spec="partial_write@write_file(rate=0.5)",
        arms=[ArmConfig(label=name).to_metadata() for name in arms],
        interleaved=True, outcomes=outcomes,
        task_summaries=[summarize_task(task, outcomes)],
    )


def test_for_arm_splits_and_recomputes_summaries():
    suite = _arm_suite()
    left, right = suite.for_arm("a"), suite.for_arm("b")

    assert (left.total_runs, right.total_runs) == (2, 2)
    assert suite.arm_labels == ["a", "b"]
    assert suite.is_multi_arm
    # 子 suite 的汇总必须**重算**（沿用父 suite 的总数会让报告里的 n 全错）
    assert left.task_summaries[0].runs == 2
    assert left.suite_id != suite.suite_id


def test_for_arm_accepts_the_directory_slug():
    """恢复出来的记录臂名是目录 slug，按原始 label 也要能筛到。"""
    suite = _arm_suite()
    suite.outcomes[0].arm = arm_slug("a")
    assert suite.for_arm("a").total_runs == 2


def test_fault_spec_is_textual_not_just_a_count():
    """`faults_injected` 只有次数，答不了"注入了哪一类" —— 故障矩阵的前提是规格列。"""
    rules = [
        FaultRule(kind=FaultKind.PARTIAL_WRITE, tool="write_file", rate=0.5),
        FaultRule(kind=FaultKind.TIMEOUT, tool="read_file"),
    ]
    spec = fault_spec_of(rules)
    assert "partial_write@write_file(rate=0.5)" in spec
    assert "timeout@read_file" in spec
    assert fault_spec_of(None) == ""


def test_suite_config_fingerprint_distinguishes_conditions():
    suite = _arm_suite()
    fingerprint = suite.config_fingerprint()
    assert fingerprint["guard_config"] == "mixed"
    assert fingerprint["fault_spec"]
    assert fingerprint["replay_mode"] == "off"
    assert fingerprint["task_set"]


# ==================== 3. 实验条件不同的对比必须被报警 ====================

def test_compare_flags_hard_confounds():
    """模型不同 → 两个不同的测量仪器，比出来的不是同一个东西。"""
    from lab.bench.compare import compare_suites

    baseline = _arm_suite()
    candidate = _arm_suite()
    candidate.model_name = "another-model"

    report = compare_suites(baseline, candidate)
    assert "model_name" in report.hard_confounds
    assert not report.comparable
    assert "不可直接比" in __import__("lab.bench.compare", fromlist=["x"]).render_comparison(report)


def test_compare_treats_guard_difference_as_intended_variable():
    """加固不同是**合法的自变量**，不是致命冲突 —— 但不能说成"代码回归"。"""
    from lab.bench.compare import compare_suites, render_comparison

    baseline = _arm_suite()
    baseline.guard_config = "none"
    candidate = _arm_suite()
    candidate.guard_config = "all"

    report = compare_suites(baseline, candidate)
    assert "guard_config" in report.config_differences
    assert report.hard_confounds == [] and report.comparable
    assert "自变量" in render_comparison(report)


def test_interleaved_pair_is_detected_and_suppresses_the_confound_note():
    """同一个交错 suite 拆出来的两臂，时间混淆已被设计消除。"""
    from lab.bench.compare import compare_suites, render_comparison

    suite = _arm_suite()
    report = compare_suites(suite.for_arm("a"), suite.for_arm("b"))
    assert report.interleaved_pair
    assert "交错配对" in render_comparison(report)


# ==================== 4. 噪声地板 ====================

def test_noise_floor_from_two_identical_arms_is_near_zero():
    """两臂同配置 → 观测差值应当很小（它**就是**噪声）。"""
    suite = _arm_suite(runs=3)
    floor = measure_noise_floor(suite)

    assert floor.arms == ["a", "b"]
    assert floor.interleaved
    success = floor.get("success_pt")
    assert success is not None and abs(success.delta) < 1e-9
    assert floor.get("cost_pct") is not None
    assert any("加任务" in note for note in floor.notes)


def test_noise_floor_requires_exactly_two_arms():
    """1 个臂或 3 个臂都测不出"同一配置重跑"的差值。"""
    with pytest.raises(ValueError):
        measure_noise_floor(_arm_suite(arms=("only",)))
    with pytest.raises(ValueError):
        measure_noise_floor(_arm_suite(arms=("a", "b", "c")))


def test_noise_floor_warns_when_not_interleaved():
    """未交错的地板被高估 → 真实退化会被当噪声放过，必须显式警告。"""
    suite = _arm_suite()
    suite.interleaved = False
    floor = measure_noise_floor(suite)
    assert any("没有交错" in note for note in floor.notes)


def test_noise_floor_flags_a_single_task_dominating():
    """地板若完全由一个任务贡献，那是"那个任务不稳"，不是"测量系统抖"。"""
    suite = _arm_suite(ok_pattern=lambda arm, index: not (arm == "b" and index == 0))
    floor = measure_noise_floor(suite)
    success = floor.get("success_pt")
    assert abs(success.worst_task) > 0


def test_noise_floor_roundtrip_and_corrupt_file_is_ignored(tmp_path):
    suite = _arm_suite()
    floor = measure_noise_floor(suite, label="测试地板")
    path = save_noise_floor(floor, tmp_path / "floor.json")

    loaded = load_noise_floor(path)
    assert loaded is not None and loaded.label == "测试地板"
    assert loaded.resolution("success_pt") == floor.resolution("success_pt")

    # 坏文件不能让门禁崩掉：地板是"辅助信息"，不该阻断判定
    path.write_text("{ not json", encoding="utf-8")
    assert load_noise_floor(path) is None
    assert load_noise_floor(tmp_path / "missing.json") is None


def test_noise_floor_renders_the_usage_rule():
    floor = measure_noise_floor(_arm_suite())
    text = render_noise_floor(floor)
    assert "噪声地板" in text and "不能" in text and "±" in text


# ==================== 5. 门禁与噪声地板的联动 ====================

def _gate_report(runs: int = 4):
    from lab.bench.compare import compare_suites

    baseline = _arm_suite(runs=runs, ok_pattern=lambda arm, index: True)
    candidate = _arm_suite(runs=runs, ok_pattern=lambda arm, index: index > 0)
    return compare_suites(baseline, candidate)


def test_gate_attaches_measured_resolution_to_every_row():
    from lab.bench.compare import evaluate_gates, render_gates

    floor = NoiseFloor(
        suite_id="s", task_count=8,
        metrics=[__import__("lab.bench.noise", fromlist=["x"]).NoiseMetric(
            name="success_pt", label="成功率", unit="pt", delta=1.0, resolution=4.0,
        )],
    )
    results = evaluate_gates(_gate_report(), noise_floor=floor)
    assert results[0].resolution == "±4.00pt"
    assert "噪声地板" in render_gates(results)


def test_gate_without_floor_says_so_instead_of_pretending():
    """没测过地板时，不能安静地按拍脑袋阈值通过 —— 必须显示「（未测）」。"""
    from lab.bench.compare import evaluate_gates

    results = evaluate_gates(_gate_report())
    assert results[0].resolution == "（未测）"


def test_auto_thresholds_raises_the_bar_to_the_floor():
    """阈值必须来自数据：地板 30pt 时，5pt 的门禁毫无意义。"""
    from lab.bench.compare import evaluate_gates

    floor = NoiseFloor(
        suite_id="s", task_count=8,
        metrics=[__import__("lab.bench.noise", fromlist=["x"]).NoiseMetric(
            name="success_pt", label="成功率", unit="pt", resolution=30.0,
        )],
    )
    manual = evaluate_gates(_gate_report(), max_success_drop_pt=5.0, noise_floor=floor)
    auto = evaluate_gates(_gate_report(), max_success_drop_pt=5.0, noise_floor=floor,
                          auto_thresholds=True)

    assert "5.0pt" in manual[0].threshold
    assert "30.0pt" in auto[0].threshold and "已按噪声地板抬高" in auto[0].threshold
    # 抬高之后，原本因噪声而"失败"的门禁应该通过
    assert not manual[0].passed and auto[0].passed


# ==================== 6. 落库往返 ====================

def test_store_roundtrip_keeps_all_experiment_metadata(tmp_path):
    """实验条件必须能从库里**原样读回来**：少一列，"同配置分组"就无从谈起。"""
    from lab.trace.store import SpanStore

    store = SpanStore(tmp_path / "t.db")
    suite = _arm_suite()
    suite.replay_mode = "replay"
    store.save_suite(suite)

    loaded = store.load_suite(suite.suite_id)
    assert loaded is not None
    assert loaded.guard_config == "mixed"
    assert loaded.fault_spec == "partial_write@write_file(rate=0.5)"
    assert loaded.replay_mode == "replay"
    assert loaded.interleaved is True
    assert [arm["label"] for arm in loaded.arms] == ["a", "b"]

    arms = {o.arm for o in loaded.outcomes}
    assert arms == {"a", "b"}
    assert {o.fault_spec for o in loaded.outcomes} == {"partial_write@write_file(rate=0.5)"}
    assert loaded.arm_labels == ["a", "b"]


def test_legacy_suite_without_new_columns_still_loads(tmp_path):
    """旧库（没有 arms/interleaved/fault_spec 列）读出来要退化成默认值，而不是抛异常。

    做法：先建新库，再用 `DROP COLUMN` 把它退化成旧形状 ——
    这比手写一份旧 schema 更忠实（不会因为"我记得的旧表结构错了"而测试失真）。
    """
    import sqlite3

    from lab.trace.store import SpanStore

    path = tmp_path / "legacy.db"
    suite = _arm_suite()
    store = SpanStore(path)
    store.save_suite(suite)

    with sqlite3.connect(str(path)) as conn:
        for column in ("guard_config", "fault_spec", "replay_mode", "arms", "interleaved"):
            conn.execute(f"ALTER TABLE bench_suites DROP COLUMN {column}")
        for column in ("fault_spec", "arm", "replay_mode", "guard_config"):
            conn.execute(f"ALTER TABLE bench_runs DROP COLUMN {column}")

    # 重开：建表语句不会给已存在的表加列，必须靠迁移补回来
    migrated = SpanStore(path)
    loaded = migrated.load_suite(suite.suite_id)

    assert loaded is not None
    assert loaded.arms == [] and loaded.interleaved is False
    assert loaded.replay_mode == "off" and loaded.guard_config == ""
    # 运行级缺失的实验条件也得退化成默认值，而不是让整个加载挂掉
    assert loaded.outcomes[0].fault_spec == "" and loaded.outcomes[0].arm == ""
    assert loaded.outcomes[0].guard_config == "all"   # 旧数据的既有默认值
    assert [o.ok for o in loaded.outcomes]

    # 迁移后又可以正常写入（新列真的存在了）
    migrated.save_suite(suite)


# ==================== 7. 端到端：真跑一次交错双臂 ====================

def test_end_to_end_interleaved_two_arms_isolates_workspaces(tmp_path, monkeypatch):
    """真跑（用脚本化 LLM）一次交错双臂，验收三件事：

    1. 两个臂**各**得到 runs_per_task 次运行；
    2. 两个臂的工作区目录**不重叠**（否则产物会互相污染，且不报错）；
    3. 落库后的实验条件能被分组查询。
    """
    import asyncio

    import lab.api as api_module
    from app.domain.models.app_config import LLMConfig
    from lab.bench.runner import run_suite
    from lab.guard.config import GuardConfig
    from lab.tests.fakes import ScriptedLLM, json_content
    from lab.trace.store import SpanStore

    def script():
        return [
            json_content({"title": "t", "goal": "g", "language": "中文",
                          "steps": [{"description": "s"}], "message": "m"}),
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "write_file",
                             "arguments": '{"filepath": "/home/ubuntu/a.txt", "content": "x"}'},
            }]},
            json_content({"success": True, "attachments": [], "result": "ok"}),
            json_content({"steps": []}),
            json_content({"message": "done", "attachments": []}),
        ]

    monkeypatch.setattr(api_module, "OpenAILLM", lambda cfg: ScriptedLLM(script()))
    monkeypatch.setattr(api_module, "load_llm_config", lambda **kwargs: LLMConfig(
        base_url="http://x", api_key="k", model_name="deepseek-chat",
        temperature=0.0, max_tokens=1024,
    ))

    store = SpanStore(tmp_path / "traces.db")
    arms = [
        ArmConfig(label="guard=none", guard_spec="none",
                  guard_config=GuardConfig.from_spec("none"), guard_label="none"),
        ArmConfig(label="guard=all", guard_spec="all",
                  guard_config=GuardConfig.from_spec("all"), guard_label="all"),
    ]
    suite = asyncio.run(run_suite(
        runs_per_task=2,
        keys=["syn_sum_range"],
        label="端到端交错",
        bench_root=tmp_path / "bench",
        guard_config=None,
        arms=arms,
        interleave=True,
        progress=False,
        on_start=store.save_suite_header,
        on_outcome=store.save_outcome,
        on_finish=store.finalize_suite,
    ))

    assert suite.total_runs == 4, "2 个臂 × 2 次 = 4 次"
    for arm_label in ("guard=none", "guard=all"):
        sub = suite.for_arm(arm_label)
        assert sub.total_runs == 2

    # 工作区必须按臂隔离
    workspaces = {o.workspace for o in suite.outcomes}
    assert len(workspaces) == 4, f"工作区发生重叠：{workspaces}"
    assert any("guard-none" in path for path in workspaces)
    assert any("guard-all" in path for path in workspaces)

    # 落库后可分组查询
    loaded = store.load_suite(suite.suite_id)
    assert loaded is not None and loaded.interleaved
    guards = {o.arm: o.guard_config for o in loaded.outcomes}
    assert guards == {"guard=none": "none", "guard=all": "all"}

    import sqlite3

    with sqlite3.connect(tmp_path / "traces.db") as conn:
        rows = conn.execute(
            "SELECT arm, guard_config, COUNT(*) FROM bench_runs "
            "WHERE suite_id = ? GROUP BY arm, guard_config", (suite.suite_id,)
        ).fetchall()
    assert sorted(rows) == [("guard=all", "all", 2), ("guard=none", "none", 2)]


# ==================== 8. 回放不完整必须响亮地失败 ====================

def test_incomplete_replay_is_a_process_flag_not_just_a_note():
    """回放未命中 → 必须变成一个过程 flag。

    == 为什么这条单独存在 ==
    实测：严格回放未命中 6 次时，SUT 在 LLM 失败后**降级继续**，
    仍然交付了合格产物，判定器于是给出 OK。
    只看 `ok` 的话，一次"只剩半条轨迹"的运行与正常成功**长得一模一样**。
    """
    suite = _arm_suite(arms=("only",))
    suite.outcomes[0].replay_misses = 6
    assert suite.outcomes[0].replay_degraded
    assert suite.replay_missed_runs == 1


def test_gate_fails_when_candidate_replay_is_incomplete():
    """回放不完整的候选不能"通过门禁" —— 它的数字本来就不该被信任。"""
    from lab.bench.compare import compare_suites, evaluate_gates, render_gates

    candidate = _arm_suite(arms=("only",))
    candidate.outcomes[0].replay_misses = 6
    report = compare_suites(_arm_suite(arms=("only",)), candidate)

    results = evaluate_gates(report)
    integrity = [item for item in results if item.name == "回放完整性"]
    assert integrity and not integrity[0].passed
    assert "回放完整性" in render_gates(results)


def test_gate_has_no_integrity_row_when_replay_is_clean():
    """干净的回放不该有一条多余的门禁行（否则人会习惯性忽略这一行）。"""
    from lab.bench.compare import compare_suites, evaluate_gates

    report = compare_suites(_arm_suite(arms=("only",)), _arm_suite(arms=("only",)))
    assert not [item for item in evaluate_gates(report) if item.name == "回放完整性"]


def test_report_shouts_about_incomplete_replay():
    """报告必须把"回放不完整"写到已知限制里（而不是只留在日志里）。"""
    from lab.bench.report import render_report

    suite = _arm_suite(arms=("only",))
    suite.replay_mode = "replay"
    suite.outcomes[0].replay_misses = 6
    text = render_report(suite)
    assert "回放不完整" in text and "复现成功" in text


def test_report_confirms_a_clean_replay():
    """干净的离线复现也要有正面确认：它在 CI 里就是"这次没联网"的证据。"""
    from lab.bench.report import render_report

    suite = _arm_suite(arms=("only",))
    suite.replay_mode = "replay"
    assert "回放是完整的" in render_report(suite)


def test_identical_arms_are_not_labelled_mixed():
    """两臂配置**相同**时不能写 "mixed"。

    噪声地板实验正好就是这种情况（两个臂的 guard 一模一样），
    写成 "mixed" 会让后来读 `noise_floor.json` 的人以为自变量是加固 ——
    那是对实验目的的彻底误读。
    """
    from lab.bench.runner import _common_or_mixed

    assert _common_or_mixed(["all", "all"]) == "all"
    assert _common_or_mixed(["none", "all"]) == "all;none"
    assert _common_or_mixed(["", ""], empty="") == ""


def test_suite_guard_config_reflects_a_single_shared_arm_config():
    """端到端：两臂同 guard 时 suite 级的 guard_config 就是那个值。"""
    suite = _arm_suite(arms=("noise-a", "noise-b"))
    suite.outcomes[0].guard_config = "all"
    suite.outcomes[1].guard_config = "all"
    suite.guard_config = "all"
    assert suite.config_fingerprint()["guard_config"] == "all"


def test_noise_floor_excludes_capability_tasks_without_rerunning():
    """口径修正必须在**同一份数据上免费重算**（这是 `--from-suite` 存在的理由）。

    实踩：初版跑的时候漏传了 purpose 过滤，跑出了 8 个任务（含本该排除的
    capability 任务）。如果只能重跑才能修正，那就得再花一次钱买**已经有**的数据。
    """
    regression = [t for t in load_all_tasks() if t.purpose == "regression"]
    capability = [t for t in load_all_tasks() if t.purpose == "capability"]
    assert capability, "任务集里应当存在 capability 任务（否则这条测试没有意义）"

    task = regression[0]
    outcomes = []
    for index in range(2):
        for arm_label in ("noise-a", "noise-b"):
            outcomes.append(RunOutcome(
                run_id=f"{arm_label}-{index}", suite_id="s", task_uid=task.uid,
                task_key=task.key, group=task.group, run_index=index, ok=True,
                tokens=1000, cost_usd=0.001, elapsed_ms=5000, tool_calls=3, steps_done=1,
                arm=arm_label,
            ))
    # 额外塞一个 capability 任务的失败样本（模拟"跑的时候没排除"）
    extra = capability[0]
    for index in range(2):
        outcomes.append(RunOutcome(
            run_id=f"cap-{index}", suite_id="s", task_uid=extra.uid,
            task_key=extra.key, group=extra.group, run_index=index, ok=(index == 0),
            tokens=1000, cost_usd=0.001, elapsed_ms=5000, tool_calls=3, steps_done=1,
            arm="noise-a" if index == 0 else "noise-b",
        ))
    suite = SuiteResult(
        suite_id="s", label="含 capability", model_name="m", runs_per_task=2,
        interleaved=True, outcomes=outcomes,
        task_summaries=[summarize_task(task, outcomes)],
    )

    with_capability = measure_noise_floor(suite)
    without = measure_noise_floor(suite, include_uids=[task.uid for task in regression])

    # 含 capability 任务时，地板被那个任务的波动抬起来
    assert abs(with_capability.get("success_pt").worst_task) > 0
    assert without.get("success_pt").worst_task == 0
    assert without.task_count == 1
    assert any("排除了 1 个任务" in note for note in without.notes)


def test_noise_floor_raises_a_clear_error_when_filter_matches_nothing():
    """口径与数据不匹配时要给能看懂的错，而不是"只有 0 个臂"。"""
    suite = _arm_suite()
    with pytest.raises(ValueError, match="不匹配"):
        measure_noise_floor(suite, include_uids=["不存在的任务"])


def test_success_rate_resolution_zero_comes_with_a_warning():
    """离散指标的一个陷阱：无任务翻转时配对区间会塔成 0，看起来像"完全可复现"。"""
    floor = measure_noise_floor(_arm_suite(runs=2))
    assert floor.resolution("success_pt") == 0
    assert any("塔成 0" in note or "不要" in note for note in floor.notes)


def test_recover_parses_arm_suffixed_run_directories(tmp_path):
    """交错产生的 `run0-guard-none` 目录必须能被恢复识别。

    旧实现用 `int(name.replace("run",""))` 解析 → 抛 ValueError → **静默跳过**：
    恢复出来的评测会少掉整条臂的样本，却看不出任何异常。
    """
    from lab.bench.recover import _workspace_layout

    root = tmp_path / "bench"
    for name in ("run0", "run1-guard-none", "run2-guard-all", "junk"):
        (root / "synthetic" / "syn_sum_range" / name / "workspace").mkdir(parents=True)

    layout = _workspace_layout(root, group="synthetic")
    assert [(index, arm) for _, index, arm, _ in layout] == [
        (0, ""), (1, "guard-none"), (2, "guard-all"),
    ]


# ==================== 9. 门禁口径：capability 任务不许进门禁 ====================

def test_gate_scope_excludes_capability_tasks():
    """`scope="regression"` 必须把 capability 任务排除在外（真实缺陷的固化）。

    == 这条测试的来历 ==
    项目自己写得很清楚（docs/step6-followup-*.md）：
      "regression 口径才是回归门禁指标；capability 任务的波动是**能力边界**，不能进门禁"
      —— 并给了实测例证："8.3pt 的差异完全由 sem_markdown_toc 一个任务的 3 次运行决定"。
    但 `bench gate` 当时调的是 `task_level()`（不过滤），**把那个任务算进去了**：
    报告里分开列两份口径、门禁里混在一起，人对不上号，
    而且门禁会因一个已知不稳定的任务变红（"频繁变红的门禁等于没有门禁"）。

    实测历史数据（70a79a26 → 80caef04）：全量口径 pass^5 下降 -25.0pt，
    而 regression 口径只有 **-14.3pt** —— 差的 10.7pt 全来自那一个 capability 任务。
    """
    from lab.bench.compare import compare_suites
    from lab.bench.task import load_all_tasks

    tasks = {t.uid: t for t in load_all_tasks()}
    regression = next(t for t in load_all_tasks() if t.purpose == "regression")
    capability = next(t for t in load_all_tasks() if t.purpose == "capability")

    def suite(suite_id, *, stable_capability: bool):
        outcomes = []
        for index in range(2):
            # regression 任务两边都稳定（3 次全对）
            for run in range(3):
                outcomes.append(RunOutcome(
                    run_id=f"{suite_id}-{regression.key}-{run}", suite_id=suite_id,
                    task_uid=regression.uid, task_key=regression.key, group=regression.group,
                    run_index=run, ok=True, tokens=100, cost_usd=0.01, elapsed_ms=10,
                    tool_calls=1, steps_done=1,
                ))
            # capability 任务：一边全对，一边只对 2/3 → pass^k 在它上面掉
            for run in range(3):
                ok = True if (stable_capability or run < 2) else False
                outcomes.append(RunOutcome(
                    run_id=f"{suite_id}-{capability.key}-{run}", suite_id=suite_id,
                    task_uid=capability.uid, task_key=capability.key, group=capability.group,
                    run_index=run, ok=ok, tokens=100, cost_usd=0.01, elapsed_ms=10,
                    tool_calls=1, steps_done=1,
                ))
        return SuiteResult(
            suite_id=suite_id, label=suite_id, model_name="m", runs_per_task=3,
            outcomes=outcomes,
            task_summaries=[summarize_task(tasks[o.task_uid], [o]) for o in outcomes[:1]],
        )

    baseline = suite("base", stable_capability=True)
    candidate = suite("cand", stable_capability=False)

    wide = compare_suites(baseline, candidate, scope="all")
    narrow = compare_suites(baseline, candidate, scope="regression")

    # 全量口径：capability 任务不稳定 → pass^k 掉
    assert wide.baseline_passk.tasks == 2
    assert wide.baseline_passk.pass_pow_k == 2
    assert wide.candidate_passk.pass_pow_k == 1

    # regression 口径：两个任务都稳定 → pass^k 不掉（门禁不该因它变红）
    assert narrow.baseline_passk.tasks == 1
    assert narrow.baseline_passk.pass_pow_k == 1
    assert narrow.candidate_passk.pass_pow_k == 1
    assert narrow.scope == "regression"


def test_gate_marks_a_skipped_passk_visibly():
    """口径下没有任务时，pass^k 那一行要**显式跳过**，不能静静消失。

    "没有这一行"很容易被读成"这一项通过了" —— 两者完全不同。
    """
    from lab.bench.compare import compare_suites, evaluate_gates

    suite = _arm_suite(arms=("only",))
    # 把所有任务的 purpose 都当成 capability（模拟 --group ci 这种只有能力任务的 suite）
    import lab.bench.task as task_module
    original = task_module.regression_uids
    try:
        task_module.regression_uids = lambda: set()
        # _scoped_suites 在 keep 为空时退化为全量 → 用直接构造的 report 验证渲染分支
        report = compare_suites(suite, suite, scope="regression")
        report.baseline_passk = None
        report.candidate_passk = None
        results = evaluate_gates(report)
        row = [r for r in results if r.name == "pass^k（任务级）"]
        assert row and row[0].observed == "跳过" and row[0].passed
    finally:
        task_module.regression_uids = original
