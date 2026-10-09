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
import json

from lab.bench.runner import RunOutcome, SuiteResult, summarize_task
from lab.bench.task import load_all_tasks
from lab.cli import _render_artifacts, main


def _fake_suite() -> SuiteResult:
    """构造一个只有 1 个任务、2 次运行（1 成功 1 失败）的最小 SuiteResult。"""
    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    outcomes = [
        RunOutcome(
            run_id="r1",
            suite_id="s1",
            task_uid=task.uid,
            task_key=task.key,
            group=task.group,
            run_index=0,
            ok=True,
            tokens=1000,
            cost_usd=0.001,
            elapsed_ms=5000,
            llm_calls=5,
            tool_calls=3,
            steps_done=1,
        ),
        RunOutcome(
            run_id="r2",
            suite_id="s1",
            task_uid=task.uid,
            task_key=task.key,
            group=task.group,
            run_index=1,
            ok=False,
            tokens=2000,
            cost_usd=0.002,
            elapsed_ms=9000,
            llm_calls=8,
            tool_calls=6,
            steps_done=1,
            error_type="agent_error",
            failed_checks=["✗ [numeric] 答案不对"],
        ),
    ]
    return SuiteResult(
        suite_id="s1",
        label="测试",
        model_name="fake-model",
        runs_per_task=1,
        started_at="2026-01-01T00:00:00",
        elapsed_s=14.0,
        outcomes=outcomes,
        task_summaries=[summarize_task(task, outcomes)],
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


def test_cli_bench_judge_dry_run_writes_nothing(tmp_path, monkeypatch):
    """`bench judge` 不带 `--yes`：只打印预估，**不调模型、不写判定文件**。

    为什么钉住它：每条样本要花 3 次调用（98 条 = 294 次），"先看一眼命令"
    若直接开跑就是一次付费评测 —— 项目纪律是付费动作必须显式确认
    （与 `bench run` 同一套约定）。
    """
    monkeypatch.setattr("lab.cli.ensure_runs_dir", lambda: tmp_path)
    gold = tmp_path / "judge_gold.csv"
    gold.write_text(
        "sample_id,task_key,artifact_dir\ns1,syn_sum_range,/nonexistent\n",
        encoding="utf-8",
    )
    out = tmp_path / "judged.json"

    code = main(["bench", "judge", "--gold", str(gold), "--out", str(out)])

    assert code == 2  # 未确认 → 拒绝执行
    assert not out.exists()  # 一个字节都没写


def test_cli_bench_judge_reports_missing_gold(tmp_path, monkeypatch):
    """标注表不存在时要**说清是哪一步缺**（并给出下一步命令），而不是抛裸异常。"""
    monkeypatch.setattr("lab.cli.ensure_runs_dir", lambda: tmp_path)
    code = main(["bench", "judge", "--gold", str(tmp_path / "nope.csv"), "--yes"])
    assert code == 2


def test_cli_bench_judge_writes_judged_json(tmp_path, monkeypatch):
    """`bench judge --yes` 的完整链路（用假 LLM）：判定 JSON 的**形状**要能被 calibrate 直接用。

    这一条守的是接口契约：`{sample_id: {维度: 三态}}` —— 键少了、维度少了、
    或把 `None` 写成 `false`，都会让下游的 κ 静默算错。
    """
    import app.infrastructure.external.llm.openai_llm as sut_llm

    from lab.bench.judge import JUDGE_DIMENSIONS

    class _FakeLLM:
        def __init__(self, config, **kwargs) -> None:
            self.config = config

        async def invoke(self, messages, **kwargs):
            return {"content": '{"verdict": true, "reason": "ok"}'}

    monkeypatch.setattr(sut_llm, "OpenAILLM", _FakeLLM)
    monkeypatch.setattr("lab.cli.ensure_runs_dir", lambda: tmp_path)

    workspace = tmp_path / "ws"
    (workspace / "home" / "ubuntu").mkdir(parents=True)
    (workspace / "home" / "ubuntu" / "sum.txt").write_text("500500\n", encoding="utf-8")

    gold = tmp_path / "judge_gold.csv"
    gold.write_text(
        f"sample_id,task_key,artifact_dir\ns1,syn_sum_range,{workspace}\ns2,syn_sum_range,{workspace}\n",
        encoding="utf-8",
    )
    out = tmp_path / "judged.json"

    code = main(["bench", "judge", "--gold", str(gold), "--out", str(out), "--yes"])

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload) == {"__meta__", "s1", "s2"}
    assert payload["__meta__"]["samples"] == 2
    # 计量代理必须接上：2 条样本 × 3 维度 = 6 次调用。
    # （假 LLM 不返回 usage，所以只断言调用次数 —— 与 Usage 的约定一致：不猜 token）
    assert payload["__meta__"]["llm_calls"] == 6
    for sample_id in ("s1", "s2"):
        assert set(payload[sample_id]) == set(JUDGE_DIMENSIONS)
        assert all(value is True for value in payload[sample_id].values())


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

    def avg_cost_for_group(self, group: str, *, faulted=None):
        """CLI 的预算预估会读它；没有历史时返回 None（走保守默认值）。

        注意签名要跟随真实实现 —— 少一个关键字参数就是 TypeError（踩过）。
        """
        return

    def load_runs_grouped_by_task(self, limit_per_task: int = 500):
        """CLI 的阈值推导会读它；替身里返回空 → 退回默认阈值。"""
        return {}

    def list_suites(self, limit: int = 10):
        return [
            {
                "suite_id": s.suite_id,
                "label": s.label,
                "model_name": s.model_name,
                "runs_per_task": s.runs_per_task,
                "started_at": s.started_at,
                "total_runs": s.total_runs,
                "successes": s.successes,
                "task_count": len(s.task_summaries),
                "elapsed_s": s.elapsed_s,
                "budget_mode": s.budget_mode,
            }
            for s in list(self.suites.values())[-limit:]
        ]

    def load_suite(self, suite_id: str):
        return self.suites.get(suite_id)


def test_artifacts_skip_pycache_and_binary(tmp_path):
    """关键产物列表：不列 `__pycache__`（运行残留），也不打印二进制内容。

    实测踩到过：`sem_string_reverse` 的 `__pycache__/*.pyc` 既占掉 `max_files`
    名额、又把一屏乱码打出来，把真正要看的产物挤出去。
    """
    ws = tmp_path / "run0"
    (ws / "home/ubuntu/input").mkdir(parents=True)
    (ws / "home/ubuntu/__pycache__").mkdir(parents=True)
    (ws / "home/ubuntu/__pycache__/reverse.cpython-313.pyc").write_bytes(b"\x00\x01binary")
    (ws / "home/ubuntu/blob.bin").write_bytes(b"\x00\x01binary")
    (ws / "home/ubuntu/reversed.txt").write_text("TNEGA SUNAM", encoding="utf-8")

    text = "\n".join(_render_artifacts(str(ws), max_files=10))
    assert "reversed.txt" in text
    assert "TNEGA SUNAM" in text
    assert "__pycache__" not in text  # 运行残留不进产物列表
    assert "二进制内容" in text  # 二进制只说大小，不打内容


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

    monkeypatch.setattr(sandbox_module, "escape_roots", list)
    assert main(["sandbox", "clean-escapes", "--yes"]) == 0


def test_cli_bench_gate_passes_and_fails(tmp_path, monkeypatch):
    """`bench gate` 必须用**退出码**表达结论（0 通过 / 1 失败）—— 这是 CI 门禁的全部意义。"""
    from lab import api as lab_api
    from lab.bench.runner import RunOutcome, SuiteResult, summarize_task
    from lab.bench.task import load_all_tasks

    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")

    def _suite(suite_id, ok_count):
        outcomes = [
            RunOutcome(
                run_id=f"{suite_id}-{i}",
                suite_id=suite_id,
                task_uid=task.uid,
                task_key=task.key,
                group=task.group,
                run_index=i,
                ok=(i < ok_count),
                tokens=1000,
                cost_usd=0.001,
                elapsed_ms=5000,
                tool_calls=3,
                steps_done=1,
            )
            for i in range(10)
        ]
        return SuiteResult(
            suite_id=suite_id, label=suite_id, outcomes=outcomes, task_summaries=[summarize_task(task, outcomes)]
        )

    store = _FakeStore()
    store.save_suite(_suite("base-ok", 10))  # 基准：10/10
    store.save_suite(_suite("cand-good", 10))  # 候选：同样 10/10 → 应通过
    store.save_suite(_suite("cand-bad", 5))  # 候选：5/10 → 成功率掉 50pt → 应失败
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: store)

    assert main(["bench", "gate", "--baseline", "base-ok", "--candidate", "cand-good"]) == 0
    assert main(["bench", "gate", "--baseline", "base-ok", "--candidate", "cand-bad"]) == 1


def test_cli_bench_gate_refuses_tiny_candidate(tmp_path, monkeypatch):
    """候选样本太小时拒绝判定 —— 否则 CI 会被噪声反复弄红。"""
    from lab import api as lab_api
    from lab.bench.runner import RunOutcome, SuiteResult, summarize_task
    from lab.bench.task import load_all_tasks

    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")

    def _suite(suite_id, n):
        outcomes = [
            RunOutcome(
                run_id=f"{suite_id}-{i}",
                suite_id=suite_id,
                task_uid=task.uid,
                task_key=task.key,
                group=task.group,
                run_index=i,
                ok=True,
                tokens=1000,
                cost_usd=0.001,
                elapsed_ms=5000,
                tool_calls=3,
                steps_done=1,
            )
            for i in range(n)
        ]
        return SuiteResult(
            suite_id=suite_id, label=suite_id, outcomes=outcomes, task_summaries=[summarize_task(task, outcomes)]
        )

    store = _FakeStore()
    store.save_suite(_suite("base-ok", 20))
    store.save_suite(_suite("tiny", 2))
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: store)

    assert main(["bench", "gate", "--baseline", "base-ok", "--candidate", "tiny", "--min-runs", "10"]) == 2


def test_cli_bench_run_accepts_new_replay_and_ab_arguments():
    """新增的录制回放 / 交错 A/B 参数都必须**编译通过并被解析出来**。

    沿用小节上一条测试的教训：子命令用了 `args.X` 而没在**该子命令**上声明，
    是"静态看不出来、跑到才炸"的那种 bug。
    """
    from lab.cli import build_parser

    args = build_parser().parse_args(
        [
            "bench",
            "run",
            "--runs",
            "2",
            "--yes",
            "--ab-guard",
            "none",
            "--ab-guard",
            "retry,postcondition",
            "--ab-label",
            "无加固",
            "--ab-label",
            "加固",
            "--replay",
            "record",
            "--replay-cache",
            "x.db",
            "--replay-ignore-volatile",
        ]
    )

    assert args.ab_guard == ["none", "retry,postcondition"]
    assert args.ab_label == ["无加固", "加固"]
    assert args.replay == "record" and args.replay_cache == "x.db"
    assert args.replay_ignore_volatile is True
    assert args.no_interleave is False

    noise = build_parser().parse_args(
        [
            "bench",
            "noise-floor",
            "--runs",
            "3",
            "--replay",
            "replay",
            "--replay-cache",
            "y.db",
            "--replay-ignore-volatile",
            "--yes",
        ]
    )
    for name in (
        "runs",
        "purpose",
        "label_a",
        "label_b",
        "file",
        "yes",
        "replay",
        "replay_cache",
        "replay_ignore_volatile",
        "guard",
    ):
        assert hasattr(noise, name), f"bench noise-floor 缺少参数 {name}"

    compare = build_parser().parse_args(["bench", "compare", "abc123", "--arm", "A", "--arm", "B"])
    assert compare.arm == ["A", "B"] and compare.candidate is None

    gate = build_parser().parse_args(["bench", "gate", "--baseline", "x"])
    assert gate.noise_floor == "auto" and gate.auto_thresholds is False


def test_cli_replay_stats_without_cache(tmp_path, monkeypatch):
    """缓存库不存在时给可操作的提示（告诉用户怎么产生它），退出码非 0。"""
    monkeypatch.setenv("LAB_LLM_CACHE", str(tmp_path / "nope.db"))
    assert main(["replay", "stats"]) == 2


def test_cli_replay_stats_reports_nondeterminism(tmp_path, monkeypatch):
    """`replay stats` 的核心产出是**服务端非确定性**比例。"""

    from lab.replay.cache import LLMCache
    from lab.replay.llm import CachedLLM, ReplayMode
    from lab.tests.fakes import ScriptedLLM, content

    cache_path = tmp_path / "cache.db"
    cache = LLMCache(cache_path)
    # 同一个请求、两次不同的回答 → 应该被统计成非确定性
    llm = CachedLLM(ScriptedLLM([content("a"), content("b")]), cache, mode=ReplayMode.RECORD)
    asyncio.run(llm.invoke([{"role": "user", "content": "q"}]))
    asyncio.run(llm.invoke([{"role": "user", "content": "q"}]))

    monkeypatch.setenv("LAB_LLM_CACHE", str(cache_path))
    assert main(["replay", "stats"]) == 0
    assert main(["replay", "stats", "--json"]) == 0


def test_cli_noise_floor_empty_purpose_names_the_reason(capsys):
    """整组都是 capability 时，报错必须说出"是 purpose 过滤筛掉的"。

    为什么值得一个用例：`ci` 组的任务全是 capability，而 `--purpose` 默认是 regression
    → 空集。只说"没有任务可测"会把人赶去查任务集，而问题其实在参数上。
    （本用例在**任何网络/花钱之前**就返回，所以它永远不会误花钱。）
    """
    assert main(["bench", "noise-floor", "--group", "ci", "--runs", "1"]) == 2
    err = capsys.readouterr().err
    assert "--purpose" in err and "capability" in err, err


def test_purpose_verdict_rules():
    """标签对账的三条规则（T5）：干净失败 → 建议改标；capability 全对 → 可考虑改回。

    为什么钉住规则：它是“标签会腐烂”的唯一拦截手段 ——
    一个 regression 任务后来变得不稳定时，没人会主动重跑一遍统计。
    """
    from lab.cli import _purpose_verdict

    assert "建议改标 capability" in _purpose_verdict("regression", 15, 14)
    assert "可考虑改回 regression" in _purpose_verdict("capability", 6, 6)
    assert _purpose_verdict("regression", 33, 33) == "一致"
    # 样本太小先不动（避免因为 3 次全对就把它塞回回归门禁）
    assert _purpose_verdict("capability", 3, 3) == "一致"


def test_cli_bench_run_empty_purpose_names_the_reason(capsys):
    """`bench run --group ci --purpose regression` 空集时必须说出真正原因。

    与 noise-floor 同一个坑：`ci` 组的任务全是 capability，而 regression 一个都不剩。
    （本用例在**任何网络/花钱之前**就返回，所以它永远不会误花钱。）
    """
    assert main(["bench", "run", "--group", "ci", "--purpose", "regression", "--limit", "1"]) == 2
    err = capsys.readouterr().err
    assert "--purpose" in err and "capability" in err, err


def test_cli_bench_run_purpose_filter_is_passed_through(monkeypatch):
    """`bench run --purpose` 必须传到 run_suite，而且默认是 all（不改变既有行为）。

    为什么钉住它：`bench run` 原来没有 `--purpose`，于是"只在 regression 任务上做
    对照实验"只能手写 7 个 `--task` —— 漏一个就悄悄多跑一个任务，还会写进报告。
    更隐蔽的是：CLI 里按 purpose 筛过的 tasks 只影响**成本预估**，
    真正跑什么由 run_suite 决定（它会自己重新 load 任务集），所以口径必须往下传。
    """
    from lab import api as lab_api

    captured = {}

    def fake_run_suite(**kwargs):
        captured.update(kwargs)
        return _async(_two_arm_suite())

    monkeypatch.setattr("lab.bench.runner.run_suite", fake_run_suite)
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _FakeStore())

    assert main(["bench", "run", "--group", "ci", "--purpose", "capability", "--limit", "1", "--yes"]) == 0
    assert captured["purpose"] == "capability"
    assert captured["group"] == "ci"

    captured.clear()
    assert main(["bench", "run", "--group", "synthetic", "--limit", "1", "--yes"]) == 0
    # 默认 all = 不过滤 → 传 None（runner 只认 None 或具体 purpose 值）
    assert captured["purpose"] is None


# ==================== 读轨迹入口（T2 / 审计 §4.8） ====================


def _failed_run_store(tmp_path):
    """造一个临时库：一次**失败**的运行（含 span / 工具序列 / 失败检查 / 产物）。

    为什么不用用户的真实库：CI 里没有 traces.db，而"读轨迹"这条命令的价值
    恰恰在于**任何机器上都能复现** —— 所以测试必须自带数据。
    """
    from lab.bench.runner import RunOutcome, SuiteResult
    from lab.sut.base import StepStats, TaskResult
    from lab.trace.span import TraceRecorder
    from lab.trace.store import SpanStore
    from lab.usage import Usage

    workspace = tmp_path / "ws"
    (workspace / "home" / "ubuntu").mkdir(parents=True, exist_ok=True)
    (workspace / "home" / "ubuntu" / "TOC.md").write_text("- 安装\n", encoding="utf-8")

    recorder = TraceRecorder("t-fail", sut_name="fake-sut")
    step = recorder.open_step("一步")
    recorder.begin_span("tool", "shell_execute").end(success=True)
    step.end(success=True)
    recorder.finish_task(ok=False)

    usage = Usage()
    usage.add_llm_call({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, 3)
    result = TaskResult(
        task_id="t-fail",
        sut_name="fake-sut",
        ok=False,
        plan_title="标题",
        steps=StepStats(total=1, done=1, succeeded=0),
        tool_sequence=["read_file", "write_file", "shell_execute"],
        llm_usage=usage,
        cost_usd=0.01,
        elapsed_ms=4200,
        workspace=str(workspace),
    )
    store = SpanStore(tmp_path / "traces.db")
    store.save_run(result, recorder.spans, goal="生成目录")
    store.save_suite(
        SuiteResult(
            suite_id="s-fail",
            label="假 suite（失败样本）",
            model_name="fake",
            outcomes=[
                RunOutcome(
                    run_id="r1",
                    suite_id="s-fail",
                    task_uid="u1",
                    task_key="syn_sum_range",
                    group="synthetic",
                    run_index=0,
                    ok=False,
                    self_reported_ok=True,
                    failed_checks=["✗ [file_exists] TOC.md 缺少 H1"],
                    process_flags=["too_many_tool_calls:13>12"],
                    task_id="t-fail",
                    sut_name="fake-sut",
                    tokens=15,
                    cost_usd=0.01,
                    elapsed_ms=4200,
                    tool_calls=3,
                    arm="noise-a",
                    workspace=str(workspace),
                )
            ],
        )
    )
    return store


def test_cli_bench_show_prints_sequence_checks_and_artifacts(tmp_path, monkeypatch, capsys):
    """`bench show` 必须把"这一次运行"摊开：工具序列 + 失败检查 + 关键产物 + 轨迹树。"""
    from lab import api as lab_api

    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _failed_run_store(tmp_path))

    assert main(["bench", "show", "s-fail", "syn_sum_range", "--run", "0"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "read_file → write_file → shell_execute" in out  # 工具序列（事件流）
    assert "缺少 H1" in out  # 失败检查
    assert "TOC.md" in out  # 关键产物
    assert "自述成功=True" in out and "只看自述会被骗" in out  # 自述与判定不一致要点出来


def test_cli_bench_show_lists_available_tasks_when_no_match(tmp_path, monkeypatch, capsys):
    """找不到匹配运行时要给**可操作**的提示（列出可用任务），而不是空白。"""
    from lab import api as lab_api

    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _failed_run_store(tmp_path))

    assert main(["bench", "show", "s-fail", "不存在的任务"]) == 2
    err = capsys.readouterr().err
    assert "可用任务" in err and "syn_sum_range" in err


def test_cli_bench_sample_failures_points_to_show(tmp_path, monkeypatch, capsys):
    """`bench sample-failures` 要挑出失败运行，并给出**可直接复制的深挖命令**。"""
    from lab import api as lab_api

    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _failed_run_store(tmp_path))

    assert main(["bench", "sample-failures", "--n", "3"]) == 0
    out = capsys.readouterr().out
    assert "syn_sum_range" in out and "缺少 H1" in out
    assert "bench show s-fail syn_sum_range --run 0" in out
    assert "too_many_tool_calls:13>12" in out


def test_cli_bench_noise_floor_writes_file(tmp_path, monkeypatch):
    """`bench noise-floor` 必须把测出来的地板写盘（`bench gate` 会自动读它）。"""
    from lab import api as lab_api
    from lab.bench.noise import load_noise_floor

    monkeypatch.setattr("lab.bench.runner.run_suite", lambda **kwargs: _async(_two_arm_suite()))
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _FakeStore())
    monkeypatch.setattr("lab.cli.ensure_runs_dir", lambda: tmp_path)

    target = tmp_path / "floor.json"
    # --purpose all：本用例测的是"写盘 + 链路"，而假的 suite 里只有 1 个任务，
    # 默认的 regression 口径会把另外 1 个任务也纳入 → 口径与数据不匹配。
    # 口径过滤本身另有专门用例（见 test_noise_floor_filters_non_regression_tasks）。
    code = main(
        ["bench", "noise-floor", "--runs", "1", "--limit", "2", "--purpose", "all", "--file", str(target), "--yes"]
    )

    assert code == 0
    floor = load_noise_floor(target)
    assert floor is not None and floor.arms == ["noise-a", "noise-b"]
    assert floor.interleaved is True


def test_cli_bench_compare_splits_arms_by_flag(tmp_path, monkeypatch):
    """`compare <suite> --arm A --arm B` 要能从**一个**交错 suite 里拆臂对比。"""
    from lab import api as lab_api

    suite = _two_arm_suite()
    store = _FakeStore()
    store.save_suite(suite)
    monkeypatch.setattr(lab_api, "default_trace_store", lambda: store)

    assert main(["bench", "compare", suite.suite_id[:8], "--arm", "noise-a", "--arm", "noise-b"]) == 0
    # 臂名写错 → 必须报错，而不是安静地拆出空集
    assert main(["bench", "compare", suite.suite_id[:8], "--arm", "noise-a", "--arm", "typo"]) == 2


def _two_arm_suite():
    """两个同配置臂交错跑出来的最小 suite（不调模型）。"""
    from lab.bench.runner import ArmConfig, RunOutcome, SuiteResult, summarize_task

    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    outcomes = []
    for index in range(1):
        for arm_label in ("noise-a", "noise-b"):
            outcomes.append(
                RunOutcome(
                    run_id=f"{arm_label}-{index}",
                    suite_id="s-noise",
                    task_uid=task.uid,
                    task_key=task.key,
                    group=task.group,
                    run_index=index,
                    ok=True,
                    tokens=1000,
                    cost_usd=0.001,
                    elapsed_ms=5000,
                    tool_calls=3,
                    steps_done=1,
                    arm=arm_label,
                )
            )
    return SuiteResult(
        suite_id="s-noise",
        label="噪声地板",
        model_name="fake",
        runs_per_task=1,
        arms=[ArmConfig(label="noise-a").to_metadata(), ArmConfig(label="noise-b").to_metadata()],
        interleaved=True,
        outcomes=outcomes,
        task_summaries=[summarize_task(task, outcomes)],
    )


def test_cli_bench_run_accepts_every_referenced_fault_argument():
    """子命令的每个被代码引用的参数都必须在**该子命令**上声明。

    实测踩过两次同类 bug：
      1. `bench run` 的主体用了 `ensure_runs_dir` 但没 import；
      2. `bench run` 引用了 `args.fault_latency`，而 `--fault-latency`
         只声明在顶层 `run` 上 → 只有真跑到那条分支才炸。
    这类 bug 的共同点是"静态看不出来、跑到才炸"，所以用一条
    **把参数全传一遍**的解析测试来兜住。
    """
    from lab.cli import build_parser

    args = build_parser().parse_args(
        [
            "bench",
            "run",
            "--runs",
            "1",
            "--guard",
            "none",
            "--fault",
            "latency_spike",
            "--fault-tool",
            "shell_*",
            "--fault-rate",
            "0.5",
            "--fault-latency",
            "0.01",
            "--budget-mode",
            "observe",
            "--concurrency",
            "1",
            "--yes",
        ]
    )

    # 这些属性都被 _cmd_bench_run 引用过，少一个就会 AttributeError
    for name in (
        "guard",
        "fault",
        "fault_tool",
        "fault_rate",
        "fault_latency",
        "budget_mode",
        "budget_defaults",
        "concurrency",
        "runs",
        "yes",
        "limit",
        "task",
    ):
        assert hasattr(args, name), f"bench run 缺少参数 {name}，而代码里会引用它"
