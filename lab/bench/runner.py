#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""执行器：对每个任务重复跑 n 次，产出可统计的结果。

== 一次 run 的完整流程 ==
    1. 建立**独立工作区** runs/bench/<suite>/<task>/<run_index>/workspace
    2. 铺 fixtures（任务输入）
    3. 调用 `lab.api.run_task`（真跑 SUT，采集轨迹/用量/加固层报告）
    4. **用同一个工作区**跑判定器（不看 Agent 自述）
    5. 评估过程规则
    6. 落盘

== 为什么每个 run 都要独立工作区 ==
如果复用同一个目录，上一次运行的产物会让下一次的判定器"意外通过"
（例如上一次写对了文件、这一次什么都没做也照样通过）。
这类污染不会报错，只会让成功率虚高 —— 是最难发现的一种实验错误。

== 并发与测量的取舍 ==
支持 `concurrency`，但**默认 1**：并行跑会让 LLM 请求互相竞争，
测出来的耗时不再可比（Step 1 报告里已经记录过孤儿进程污染耗时指标的教训）。
需要压缩墙钟时间时再调高，并在报告里标注"本次耗时不可横向比较"。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from lab.api import run_task
from lab.bench.stats import Interval, mean_ci, wilson_interval
from lab.bench.task import BenchTask, load_all_tasks
from lab.bench.verifier import CheckResult, evaluate_process, run_checks
from lab.infra.local_sandbox import LocalSandbox


class RunOutcome(BaseModel):
    """一次 run 的结果（判定器视角 + SUT 指标 + 加固层观察）。"""

    run_id: str
    suite_id: str
    task_uid: str
    task_key: str
    group: str
    run_index: int

    ok: bool = False  # **只看判定器**（结果分）
    self_reported_ok: bool = False  # SUT 自己认为成功（只用于对比，不参与判定）
    checks: List[CheckResult] = Field(default_factory=list)
    process_flags: List[str] = Field(default_factory=list)

    # SUT 指标
    task_id: str = ""
    sut_name: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    llm_calls: int = 0
    tool_calls: float = 0.0
    steps_done: int = 0
    error_type: Optional[str] = None
    error: Optional[str] = None

    # 加固层观察（Step 3）
    budget_violated: List[str] = Field(default_factory=list)
    budget_would_stop: bool = False
    action_diversity: Optional[float] = None
    postcondition_warnings: int = 0
    faults_injected: int = 0
    error_types: Dict[str, int] = Field(default_factory=dict)

    workspace: str = ""
    failed_checks: List[str] = Field(default_factory=list)


class TaskSummary(BaseModel):
    """一个任务的多次运行汇总。"""

    task_uid: str
    task_key: str = ""
    group: str
    title: str = ""
    runs: int = 0
    successes: int = 0
    success_rate: Interval = Field(default_factory=Interval)
    tokens: Interval = Field(default_factory=Interval)
    cost_usd: Interval = Field(default_factory=Interval)
    elapsed_ms: Interval = Field(default_factory=Interval)
    tool_calls: Interval = Field(default_factory=Interval)
    flag_counts: Dict[str, int] = Field(default_factory=dict)

    @property
    def unstable(self) -> bool:
        """同一任务多次运行结果不一致 —— 这类任务最能说明系统的方差问题。"""
        return 0 < self.successes < self.runs


class SuiteResult(BaseModel):
    """一次完整评测（一个 suite = 任务集 × n 次重复）。"""

    suite_id: str
    label: str = ""
    model_name: str = ""
    temperature: float = 0.0
    runs_per_task: int = 1
    concurrency: int = 1
    sut_name: str = ""
    budget_mode: str = "observe"
    started_at: str = ""
    elapsed_s: float = 0.0
    task_summaries: List[TaskSummary] = Field(default_factory=list)
    outcomes: List[RunOutcome] = Field(default_factory=list)
    fixture_failures: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    # 旧数据（在加 self_reported_ok 字段之前跑的）没有这个字段。
    # 用显式标志而不是把 NULL 当成 False —— 把"未知"当成"没虚报"就是造假数据。
    self_report_available: bool = True

    # ---- 总体统计 ----

    @property
    def total_runs(self) -> int:
        return len(self.outcomes)

    @property
    def successes(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    # ---- SUT 自述 vs 独立判定（**两个数字都要报**）----

    @property
    def self_reported_successes(self) -> int:
        """SUT 自己认为成功的运行数。

        为什么必须单独统计并与判定结果对比：
        实测中遇到过"规划出 0 个步骤、一个工具都没调"的任务，SUT 仍然返回 ok=True。
        如果只看自述，成功率会是 100%；独立判定给的是 50%。
        这个差值本身就是一项指标：它衡量的是**SUT 的自我评估能力**，
        而很多 Agent 在这上面是不可靠的（它们会"声称完成"而不是"完成"）。
        """
        return sum(1 for o in self.outcomes if o.self_reported_ok)

    @property
    def self_report_gap(self) -> int:
        """自报成功但被判定失败的次数（即"虚报"数量）。"""
        return sum(1 for o in self.outcomes if o.self_reported_ok and not o.ok)

    @property
    def honest_failures(self) -> int:
        """自报失败且确实失败的次数（SUT 如实报告的失败）。"""
        return sum(1 for o in self.outcomes if not o.self_reported_ok and not o.ok)

    @property
    def success_rate(self) -> Interval:
        return wilson_interval(self.successes, self.total_runs)

    def _values(self, field: str) -> List[float]:
        return [float(getattr(o, field) or 0) for o in self.outcomes]

    @property
    def tokens(self) -> Interval:
        return mean_ci(self._values("tokens"))

    @property
    def cost(self) -> Interval:
        return mean_ci(self._values("cost_usd"))

    @property
    def elapsed(self) -> Interval:
        return mean_ci(self._values("elapsed_ms"))

    @property
    def tool_calls(self) -> Interval:
        return mean_ci(self._values("tool_calls"))

    def by_group(self) -> Dict[str, Dict[str, Any]]:
        """按任务分组（synthetic / semireal）汇总。"""
        groups: Dict[str, List[RunOutcome]] = {}
        for outcome in self.outcomes:
            groups.setdefault(outcome.group, []).append(outcome)

        result: Dict[str, Dict[str, Any]] = {}
        for group, items in groups.items():
            successes = sum(1 for o in items if o.ok)
            result[group] = {
                "runs": len(items),
                "successes": successes,
                "success_rate": wilson_interval(successes, len(items)),
                "tokens": mean_ci([float(o.tokens) for o in items]),
                "cost": mean_ci([float(o.cost_usd) for o in items]),
                "elapsed": mean_ci([float(o.elapsed_ms) for o in items]),
            }
        return result

    def error_attribution(self) -> Dict[str, int]:
        """失败归因：按 error_type 统计（handoff 第 8 节的"定位/规划/验证/工具"雏形）。"""
        counts: Dict[str, int] = {}
        for outcome in self.outcomes:
            if outcome.ok:
                continue
            key = outcome.error_type or "verification_failed"
            counts[key] = counts.get(key, 0) + 1
        # 工具级失败类型（来自加固层的 error_types）也一起统计，
        # 这样"Agent 说成功但工具层面出过错"的情况不会被漏掉
        for outcome in self.outcomes:
            for error_type, count in (outcome.error_types or {}).items():
                key = f"tool:{error_type}"
                counts[key] = counts.get(key, 0) + count
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def process_flag_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for outcome in self.outcomes:
            for flag in outcome.process_flags:
                key = flag.split(":")[0]
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def budget_stats(self) -> Dict[str, Any]:
        """预算观察统计（observe 模式的核心产出：为切模式提供依据）。"""
        violated_runs = [o for o in self.outcomes if o.budget_violated]
        metrics: Dict[str, int] = {}
        for outcome in self.outcomes:
            for metric in outcome.budget_violated:
                metrics[metric] = metrics.get(metric, 0) + 1
        return {
            "runs": len(self.outcomes),
            "violated_runs": len(violated_runs),
            "violated_rate": (len(violated_runs) / len(self.outcomes)) if self.outcomes else 0.0,
            "would_stop_runs": sum(1 for o in self.outcomes if o.budget_would_stop),
            "by_metric": dict(sorted(metrics.items(), key=lambda kv: -kv[1])),
        }


def summarize_task(task: BenchTask, outcomes: List[RunOutcome]) -> TaskSummary:
    """把同一任务的多次 run 汇总成 TaskSummary。"""
    successes = sum(1 for o in outcomes if o.ok)
    flags: Dict[str, int] = {}
    for outcome in outcomes:
        for flag in outcome.process_flags:
            key = flag.split(":")[0]
            flags[key] = flags.get(key, 0) + 1

    return TaskSummary(
        task_uid=task.uid,
        task_key=task.key,
        group=task.group,
        title=task.title,
        runs=len(outcomes),
        successes=successes,
        success_rate=wilson_interval(successes, len(outcomes)),
        tokens=mean_ci([float(o.tokens) for o in outcomes]),
        cost_usd=mean_ci([float(o.cost_usd) for o in outcomes]),
        elapsed_ms=mean_ci([float(o.elapsed_ms) for o in outcomes]),
        tool_calls=mean_ci([float(o.tool_calls) for o in outcomes]),
        flag_counts=flags,
    )


async def run_once(
        task: BenchTask,
        *,
        suite_id: str,
        run_index: int,
        bench_root: Path,
        model_name: str = "",
        temperature: Optional[float] = None,
        budget_policy: Any = None,
        fault_rules: Any = None,
        max_seconds: Optional[float] = None,
) -> RunOutcome:
    """跑一次任务：铺输入 → 真跑 SUT → 判定 → 评估过程。"""
    run_id = str(uuid.uuid4())
    workspace = bench_root / task.group / task.key / f"run{run_index}" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    outcome = RunOutcome(
        run_id=run_id, suite_id=suite_id, task_uid=task.uid, task_key=task.key,
        group=task.group, run_index=run_index, workspace=str(workspace),
    )

    # 1.铺 fixtures（用沙箱写入，保证与 Agent 看到的路径一致）
    setup = LocalSandbox(workspace)
    await setup.ensure_sandbox()
    for fixture in task.fixtures:
        written = await setup.write_file(fixture.path, fixture.content)
        if not written.success:
            raise RuntimeError(f"铺设输入失败 {fixture.path}: {written.message}")

    # 2.真跑 SUT
    started = time.monotonic()
    result = await run_task(
        task.goal,
        workspace=workspace,
        max_seconds=max_seconds or task.max_seconds,
        temperature=temperature,
        budget_policy=budget_policy,
        fault_rules=fault_rules,
        watch_literals=task.process.answer_literals,
    )
    _ = time.monotonic() - started

    # 3.判定（**只看产物**）
    verifier = LocalSandbox(workspace)
    checks = await run_checks(task.verify, verifier)
    ok = all(check.ok for check in checks) and bool(checks)

    # 4.过程规则
    flags = evaluate_process(task, result)

    # 5.填充
    guard = result.guard or {}
    budget = guard.get("budget") or {}
    loop = guard.get("loop") or {}
    outcome.ok = ok
    outcome.self_reported_ok = bool(result.ok)
    outcome.checks = checks
    outcome.process_flags = flags
    outcome.failed_checks = [c.line() for c in checks if not c.ok]
    outcome.task_id = result.task_id
    outcome.sut_name = result.sut_name
    outcome.tokens = result.llm_usage.total_tokens
    outcome.cost_usd = result.cost_usd
    outcome.elapsed_ms = result.elapsed_ms
    outcome.llm_calls = result.llm_usage.llm_calls
    outcome.tool_calls = float(loop.get("tool_calls") or 0)
    outcome.steps_done = result.steps.done
    outcome.error_type = result.error_type
    outcome.error = result.error
    outcome.budget_violated = list(budget.get("violated_metrics") or [])
    outcome.budget_would_stop = bool(budget.get("would_stop"))
    outcome.action_diversity = loop.get("action_diversity")
    outcome.postcondition_warnings = len((guard.get("postconditions") or {}).get("warnings") or [])
    outcome.faults_injected = int((guard.get("faults") or {}).get("injections") or 0)
    outcome.error_types = dict(guard.get("error_types") or {})
    return outcome


async def run_suite(
        *,
        runs_per_task: int = 1,
        group: Optional[str] = None,
        limit: Optional[int] = None,
        keys: Optional[List[str]] = None,
        label: str = "",
        bench_root: Path,
        concurrency: int = 1,
        temperature: Optional[float] = None,
        budget_policy: Any = None,
        fault_rules: Any = None,
        max_seconds: Optional[float] = None,
        progress: bool = True,
) -> SuiteResult:
    """跑一个完整 suite（任务集 × n 次重复）。"""
    from datetime import datetime

    from lab.config import load_llm_config

    tasks = load_all_tasks(group=group)
    if keys:
        # 定向运行个别任务：调试、只补跑失败任务、做单任务对照实验都用得上
        wanted = set(keys)
        tasks = [t for t in tasks if t.key in wanted or t.uid in wanted]
        missing = wanted - {t.key for t in tasks} - {t.uid for t in tasks}
        if missing:
            raise ValueError(f"未找到指定的任务: {sorted(missing)}")
    if limit:
        tasks = tasks[:limit]

    suite_id = str(uuid.uuid4())
    llm_config = load_llm_config(temperature=temperature)

    suite = SuiteResult(
        suite_id=suite_id,
        label=label or f"{len(tasks)}任务×{runs_per_task}次",
        model_name=llm_config.model_name,
        temperature=llm_config.temperature,
        runs_per_task=runs_per_task,
        concurrency=concurrency,
        budget_mode=(budget_policy.mode.value if budget_policy else "observe"),
        started_at=datetime.now().isoformat(timespec="seconds"),
    )

    started = time.monotonic()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(task: BenchTask, index: int) -> RunOutcome:
        async with semaphore:
            if progress:
                print(f"  → {task.uid} run{index + 1}/{runs_per_task}", flush=True)
            return await run_once(
                task, suite_id=suite_id, run_index=index, bench_root=bench_root,
                model_name=llm_config.model_name, temperature=temperature,
                budget_policy=budget_policy, fault_rules=fault_rules, max_seconds=max_seconds,
            )

    jobs = [_one(task, index) for task in tasks for index in range(runs_per_task)]
    outcomes = await asyncio.gather(*jobs, return_exceptions=True)

    # 单个 run 出错不能带走整个 suite（否则一次评测白跑）
    collected: List[RunOutcome] = []
    for job, outcome in zip(jobs, outcomes):
        if isinstance(outcome, BaseException):
            suite.fixture_failures.append(f"{type(outcome).__name__}: {outcome}")
            continue
        collected.append(outcome)

    suite.outcomes = collected
    suite.task_summaries = [
        summarize_task(task, [o for o in collected if o.task_uid == task.uid])
        for task in tasks
    ]
    suite.elapsed_s = round(time.monotonic() - started, 1)
    if concurrency > 1:
        suite.notes.append(
            f"本次以并发 {concurrency} 运行：**耗时指标不可横向比较**"
            "（并行会互相竞争模型配额与本机资源）。"
        )
    return suite
