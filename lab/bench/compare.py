#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨 suite 对比 + Pareto 前沿（Step 5）。

== 三件事 ==
1. **单个 suite 的汇总**（含两种区间：t 与 bootstrap）
2. **配对对比**：同一个任务在两个方案上的差值 —— 消掉任务难度差异
3. **Pareto 前沿**：成功率 vs 成本的支配关系，输出**文本表**
   （不是图：handoff 第 1 节写明当前模型不能读图，Step 2 的 trace 也是文本优先）

== 一条必须写在报告里的诚实声明 ==
配对能消掉**任务难度**差异，但消不掉**时间相关**的服务端漂移。
两组跑在不同时段时，服务端整体变快/变慢会同时影响两边，伪装成"方案差异"。
要真正分离两者必须做**交错 A/B**（A,B,A,B…）。
所以本模块产出的对比严格来说是"**同期对比**"而不是"因果对比"，
`ComparisonReport.confound_note` 会把这个限制带到报告里。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from lab.bench.runner import RunOutcome, SuiteResult
from lab.bench.stats import (
    Interval,
    bootstrap_ci,
    mean,
    mean_ci,
    paired_bootstrap_ci,
    wilson_interval,
)

# 参与对比的连续指标：属性名 → (显示名, 单位, 是否越低越好)
METRICS = {
    "tokens": ("tokens/任务", "", True),
    "cost_usd": ("成本/任务", " 美元", True),
    "elapsed_ms": ("耗时/任务", " ms", True),
    "tool_calls": ("工具调用/任务", "", True),
}


class MetricSummary(BaseModel):
    """一个指标的两种区间（差得多就说明分布重尾）。"""

    name: str
    unit: str = ""
    lower_is_better: bool = True
    mean: float = 0.0
    t_interval: Interval = Field(default_factory=Interval)
    bootstrap_interval: Interval = Field(default_factory=Interval)
    median: float = 0.0
    p95: float = 0.0

    @property
    def heavy_tailed(self) -> bool:
        """bootstrap 区间比 t 区间宽 1.3 倍以上 → 尾部很重，均值不稳定。"""
        if not self.t_interval.half_width or not self.bootstrap_interval.half_width:
            return False
        return self.bootstrap_interval.half_width > self.t_interval.half_width * 1.3


class SuiteSummary(BaseModel):
    """一个 suite 的汇总。"""

    suite_id: str
    label: str = ""
    sut_version: str = ""
    n: int = 0
    successes: int = 0
    success_rate: Interval = Field(default_factory=Interval)
    metrics: Dict[str, MetricSummary] = Field(default_factory=dict)
    cost_per_success: float = 0.0  # 成本/成功任务：把"成本"与"可靠性"合成一个数
    error_types: Dict[str, int] = Field(default_factory=dict)
    process_flags: Dict[str, int] = Field(default_factory=dict)
    self_reported_successes: int = 0
    false_positives: int = 0  # 自报成功但判定失败
    false_negatives: int = 0  # 自报失败但产物合格

    @property
    def total_cost(self) -> float:
        return self.metrics["cost_usd"].mean * self.n if "cost_usd" in self.metrics else 0.0


def summarize_suite(suite: SuiteResult) -> SuiteSummary:
    """把一个 suite 汇总成可比较的形状。"""
    outcomes = suite.outcomes
    n = len(outcomes)
    successes = sum(1 for o in outcomes if o.ok)

    metrics: Dict[str, MetricSummary] = {}
    for key, (name, unit, lower_better) in METRICS.items():
        raw = [getattr(o, key) for o in outcomes]
        values = [float(v or 0) for v in raw]
        if not values:
            continue
        t_interval = mean_ci(values)
        boot = bootstrap_ci(values)
        ordered = sorted(values)
        metrics[key] = MetricSummary(
            name=name, unit=unit, lower_is_better=lower_better,
            mean=mean(values), t_interval=t_interval, bootstrap_interval=boot,
            median=boot.point if boot.method.endswith("median") else ordered[len(ordered) // 2],
            p95=ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        )

    errors: Dict[str, int] = {}
    for outcome in outcomes:
        if not outcome.ok:
            key = outcome.error_type or "verification_failed"
            errors[key] = errors.get(key, 0) + 1
        for error_type, count in (outcome.error_types or {}).items():
            key = f"tool:{error_type}"
            errors[key] = errors.get(key, 0) + count

    flags: Dict[str, int] = {}
    for outcome in outcomes:
        for flag in outcome.process_flags:
            key = flag.split(":")[0]
            flags[key] = flags.get(key, 0) + 1

    total_cost = sum(o.cost_usd for o in outcomes)
    return SuiteSummary(
        suite_id=suite.suite_id,
        label=suite.label,
        n=n,
        successes=successes,
        success_rate=wilson_interval(successes, n),
        metrics=metrics,
        cost_per_success=(total_cost / successes) if successes else float("inf"),
        error_types=dict(sorted(errors.items(), key=lambda kv: -kv[1])),
        process_flags=dict(sorted(flags.items(), key=lambda kv: -kv[1])),
        self_reported_successes=suite.self_reported_successes,
        false_positives=suite.self_report_gap,
        false_negatives=suite.false_negative_runs,
    )


class PairedMetric(BaseModel):
    """一个指标的配对对比。"""

    name: str
    unit: str = ""
    n_pairs: int = 0
    baseline_mean: float = 0.0
    candidate_mean: float = 0.0
    mean_diff: float = 0.0
    diff_interval: Interval = Field(default_factory=Interval)
    wins: int = 0  # 候选方案更优的任务数
    losses: int = 0
    ties: int = 0

    @property
    def relative_change(self) -> float:
        return (self.mean_diff / self.baseline_mean) if self.baseline_mean else 0.0

    @property
    def significant(self) -> bool:
        """区间不跨 0 → 差异方向可信（但**未**排除时间混淆）。"""
        if self.diff_interval.low is None or self.diff_interval.high is None:
            return False
        return self.diff_interval.low > 0 or self.diff_interval.high < 0


class ComparisonReport(BaseModel):
    """两个 suite 的对比。"""

    baseline: SuiteSummary
    candidate: SuiteSummary
    paired: Dict[str, PairedMetric] = Field(default_factory=dict)
    success_rate_delta: float = 0.0
    only_in_baseline: List[str] = Field(default_factory=list)
    only_in_candidate: List[str] = Field(default_factory=list)
    confound_note: str = (
        "配对消掉了任务难度差异，但**消不掉时间相关的服务端漂移**："
        "两组跑在不同时段时，服务端整体变快/变慢会同时影响两边，伪装成方案差异。"
        "要分离两者必须做**交错 A/B**（A,B,A,B…）。"
        "因此本对比是「同期对比」，不是严格意义上的因果对比。"
    )


def compare_suites(baseline: SuiteResult, candidate: SuiteResult) -> ComparisonReport:
    """配对对比两个 suite（按 task_key 配对，每边取该任务的均值）。"""
    report = ComparisonReport(
        baseline=summarize_suite(baseline),
        candidate=summarize_suite(candidate),
        success_rate_delta=(
            sum(1 for o in candidate.outcomes if o.ok) / max(1, len(candidate.outcomes))
            - sum(1 for o in baseline.outcomes if o.ok) / max(1, len(baseline.outcomes))
        ),
    )

    by_task_a = _group_by_task(baseline.outcomes)
    by_task_b = _group_by_task(candidate.outcomes)
    shared = sorted(set(by_task_a) & set(by_task_b))
    report.only_in_baseline = sorted(set(by_task_a) - set(by_task_b))
    report.only_in_candidate = sorted(set(by_task_b) - set(by_task_a))

    for key, (name, unit, lower_better) in METRICS.items():
        diffs: List[float] = []
        a_values: List[float] = []
        b_values: List[float] = []
        wins = losses = ties = 0
        for task_key in shared:
            a_mean = mean([float(getattr(o, key) or 0) for o in by_task_a[task_key]])
            b_mean = mean([float(getattr(o, key) or 0) for o in by_task_b[task_key]])
            a_values.append(a_mean)
            b_values.append(b_mean)
            diff = b_mean - a_mean
            diffs.append(diff)
            if diff == 0:
                ties += 1
            elif (diff < 0) == lower_better:
                wins += 1
            else:
                losses += 1

        if not diffs:
            continue
        report.paired[key] = PairedMetric(
            name=name, unit=unit, n_pairs=len(diffs),
            baseline_mean=mean(a_values), candidate_mean=mean(b_values),
            mean_diff=mean(diffs), diff_interval=paired_bootstrap_ci(diffs),
            wins=wins, losses=losses, ties=ties,
        )
    return report


def _group_by_task(outcomes: Sequence[RunOutcome]) -> Dict[str, List[RunOutcome]]:
    grouped: Dict[str, List[RunOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.task_key, []).append(outcome)
    return grouped


# ==================== Pareto 前沿 ====================

class ParetoPoint(BaseModel):
    """Pareto 图上的一个点：成功率 vs 成本。"""

    suite_id: str
    label: str
    success_rate: float
    cost_per_task: float
    cost_per_success: float
    n: int
    dominated_by: Optional[str] = None

    @property
    def on_frontier(self) -> bool:
        return self.dominated_by is None


def pareto_points(suites: Sequence[SuiteResult]) -> List[ParetoPoint]:
    """把多个 suite 变成 Pareto 点（目标是：成功率越高越好、成本越低越好）。"""
    points = [
        ParetoPoint(
            suite_id=summary.suite_id,
            label=summary.label or summary.suite_id[:8],
            success_rate=summary.success_rate.point,
            cost_per_task=summary.metrics["cost_usd"].mean if "cost_usd" in summary.metrics else 0.0,
            cost_per_success=summary.cost_per_success,
            n=summary.n,
        )
        for summary in (summarize_suite(s) for s in suites)
    ]

    # 支配关系：A 支配 B ⇔ A 成功率 ≥ B 且 A 成本 ≤ B，且至少一个严格更优
    for point in points:
        for other in points:
            if other is point:
                continue
            no_worse = other.success_rate >= point.success_rate and other.cost_per_task <= point.cost_per_task
            strictly_better = (
                other.success_rate > point.success_rate or other.cost_per_task < point.cost_per_task
            )
            if no_worse and strictly_better:
                point.dominated_by = other.suite_id
                break
    return points


def group_by_task_set(
        suites: Sequence[SuiteResult],
        *,
        min_runs: int = 1,
) -> Dict[Tuple[str, ...], List[SuiteResult]]:
    """按"覆盖的任务集合"给 suite 分组。

    == 为什么这是必需的（踩过的坑）==
    初版直接把全部 suite 丢到一张 Pareto 表上，结果一个 **n=3 的冒烟 suite**
    因为“成功率高、成本低”而“支配”了 **n=60 的正式基线**。
    这不是统计学问题，而是**问题本身不同**：任务集不同、规模不同，
    根本不能放在同一张图里比。

    所以：只有**任务集完全相同**的 suite 才能比；不同任务集各画一张表。
    同时用 min_runs 拦住样本太少的点 —— n=3 的估计值不配上 Pareto 图。
    """
    groups: Dict[Tuple[str, ...], List[SuiteResult]] = {}
    for suite in suites:
        if len(suite.outcomes) < min_runs:
            continue
        task_set = tuple(sorted({o.task_uid for o in suite.outcomes}))
        groups.setdefault(task_set, []).append(suite)
    return groups


def render_pareto_groups(
        suites: Sequence[SuiteResult],
        *,
        min_runs: int = 10,
) -> str:
    """按任务集分组渲染 Pareto 表（每组一张）。"""
    groups = group_by_task_set(suites, min_runs=min_runs)
    skipped = [s for s in suites if len(s.outcomes) < min_runs]

    lines: List[str] = []
    if not groups:
        lines.append(f"（没有 n >= {min_runs} 的 suite 可比较）")
    for task_set, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        sizes = sorted({len(s.outcomes) for s in group})
        lines.append(f"### 任务集：{len(task_set)} 个任务（{'/'.join(t.split('/')[-1] for t in task_set[:3])}"
                     f"{'…' if len(task_set) > 3 else ''}）")
        lines.append("")
        lines.append(f"组内 suite 数：{len(group)}，规模：{sizes}")
        if len(group) < 2:
            lines.append("")
            lines.append("> 该任务集下只有一个 suite，**没有可比的对照点**。")
            lines.append("")
        lines.append(render_pareto(pareto_points(group)))
        lines.append("")

    if skipped:
        lines.append(f"> 已排除 {len(skipped)} 个规模 < {min_runs} 的 suite："
                     f"{[(s.suite_id[:8], len(s.outcomes)) for s in skipped][:5]}")
        lines.append("> n 太小的估计值不应该出现在 Pareto 图上（它会因为运气而“支配”一切）。")
    return "\n".join(lines)


def render_pareto(points: Sequence[ParetoPoint]) -> str:
    """文本形式的 Pareto 表（图在这个项目里不可用）。"""
    lines: List[str] = []
    lines.append("| suite | 规模 | 成功率 | 成本/任务 | **成本/成功** | 前沿 | 被谁支配 |")
    lines.append("|---|---|---|---|---|---|---|")
    for point in sorted(points, key=lambda p: (-p.success_rate, p.cost_per_task)):
        mark = "★" if point.on_frontier else ""
        dominated = point.dominated_by[:8] if point.dominated_by else "—"
        lines.append(
            f"| `{point.suite_id[:8]}` {point.label} | n={point.n} | {point.success_rate:.1%} "
            f"| ${point.cost_per_task:.4f} | **${point.cost_per_success:.4f}** | {mark} | {dominated} |"
        )
    frontier = [p for p in points if p.on_frontier]
    lines.append("")
    if len(frontier) <= 1:
        lines.append(
            "> 前沿上只有一个点：**其它配置都被它支配**（成功率不更低、成本不更高）。"
            "要画出真正的 Pareto 曲线，需要跑多组**有意做不同取舍**的配置"
            "（例如限制迭代次数 / 上下文压缩开关 / 不同模型）。"
        )
    else:
        lines.append(f"> 前沿上有 {len(frontier)} 个点，它们之间**不可比** —— "
                     "挑哪个取决于你更在意成功率还是成本。")
    lines.append("> 注意：`成本/成功` 把\"多少钱\"和\"做对没有\"合成一个数，"
                 "它比\"成本/任务\"更能反映真实代价（便宜但总失败是最贵的）。")
    return "\n".join(lines)


def render_comparison(report: ComparisonReport) -> str:
    """渲染两个 suite 的对比。"""
    a, b = report.baseline, report.candidate
    lines: List[str] = []
    lines.append(f"## 对比：`{a.suite_id[:8]}` → `{b.suite_id[:8]}`")
    lines.append("")
    lines.append(f"- A: {a.label}（n={a.n}）")
    lines.append(f"- B: {b.label}（n={b.n}）")
    lines.append("")

    lines.append("### 总体")
    lines.append("")
    lines.append("| 指标 | A | B | 变化 |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| 成功率 | {a.success_rate.point:.1%} [{a.success_rate.low:.1%}, {a.success_rate.high:.1%}] "
        f"| {b.success_rate.point:.1%} [{b.success_rate.low:.1%}, {b.success_rate.high:.1%}] "
        f"| {report.success_rate_delta:+.1%} |"
    )
    for key, paired in report.paired.items():
        lines.append(
            f"| {paired.name} | {paired.baseline_mean:,.2f}{paired.unit} "
            f"| {paired.candidate_mean:,.2f}{paired.unit} "
            f"| {paired.relative_change:+.1%} |"
        )
    lines.append("")

    lines.append("### 配对对比（按任务配对，消掉任务难度差异）")
    lines.append("")
    lines.append("| 指标 | 配对差值 | bootstrap 95% 区间 | 相对变化 | 胜/负/平 | 方向可信? |")
    lines.append("|---|---|---|---|---|---|")
    for paired in report.paired.values():
        interval = paired.diff_interval
        if interval.low is None:
            span = "—"
        else:
            span = f"[{interval.low:,.1f}, {interval.high:,.1f}]"
        lines.append(
            f"| {paired.name} | {paired.mean_diff:+,.1f} | {span} | {paired.relative_change:+.1%} "
            f"| {paired.wins}/{paired.losses}/{paired.ties} | {'✅ 不跨 0' if paired.significant else '❌ 跨 0'} |"
        )
    lines.append("")

    lines.append("### 重尾诊断（t 区间 vs bootstrap 区间）")
    lines.append("")
    lines.append("| 指标 | 均值 | 中位数 | P95 | t 半宽 | bootstrap 半宽 | 重尾? |")
    lines.append("|---|---|---|---|---|---|---|")
    for metric in b.metrics.values():
        lines.append(
            f"| {metric.name} | {metric.mean:,.1f}{metric.unit} | {metric.median:,.1f} "
            f"| {metric.p95:,.1f} | ±{metric.t_interval.half_width or 0:,.1f} "
            f"| ±{metric.bootstrap_interval.half_width or 0:,.1f} "
            f"| {'⚠️ 是' if metric.heavy_tailed else '否'} |"
        )
    lines.append("")
    lines.append("> 中位数远低于均值 + bootstrap 区间明显宽于 t 区间 = 分布重尾，"
                 "**均值不是个好代表**，报告应该同时给中位数与 P95。")
    lines.append("")

    lines.append("### 可信度与失败模式")
    lines.append("")
    lines.append("| 项 | A | B |")
    lines.append("|---|---|---|")
    lines.append(f"| SUT 自述成功 | {a.self_reported_successes}/{a.n} | {b.self_reported_successes}/{b.n} |")
    lines.append(f"| 虚报成功（自述成功但判定失败） | {a.false_positives} | {b.false_positives} |")
    lines.append(f"| 误报失败（自述失败但产物合格） | {a.false_negatives} | {b.false_negatives} |")
    lines.append(f"| 失败归因 | {a.error_types or '无'} | {b.error_types or '无'} |")
    lines.append(f"| 过程问题 | {a.process_flags or '无'} | {b.process_flags or '无'} |")
    lines.append(f"| **成本/成功任务** | ${a.cost_per_success:.4f} | ${b.cost_per_success:.4f} |")
    lines.append("")

    if report.only_in_baseline or report.only_in_candidate:
        lines.append(f"> 只在 A 里出现的任务: {report.only_in_baseline}；"
                     f"只在 B 里出现的任务: {report.only_in_candidate}（这些不参与配对）")
        lines.append("")

    lines.append("### ⚠️ 混淆声明")
    lines.append("")
    lines.append(report.confound_note)
    return "\n".join(lines)
