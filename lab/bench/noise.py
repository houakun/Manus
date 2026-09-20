#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""噪声地板：**同一个配置跑两次能差多少**。

== 为什么这是整个评测体系的地基 ==
`docs/step5-report.md §2.4` 把这一条列为"最大遗留缺口"：

> 没测过同一份代码跑两次差多少，所以 22% 的差异无法判断是否超过测量分辨率。

没有这个数，所有 A/B 结论都少了分母：你只能说"B 比 A 便宜 22%"，
**不能说"B 更便宜"** —— 因为可能同一份代码跑两次本来就能差 25%。

== 怎么测（关键：必须交错，不能分两段跑）==
把**同一套配置**做成两个臂（只有名字不同），交错跑：

    第 0 轮: noise-a, noise-b
    第 1 轮: noise-b, noise-a
    第 2 轮: noise-a, noise-b

两臂之间**没有任何因果差异**，所以它们之间观测到的差值，
除了纯粹采样噪声之外别无解释。那个差值就是测量分辨率。

如果分两段跑（先跑完 A 再跑 B），测出来的"地板"里会混进**时间漂移**
（服务端在某段时间整体变慢），地板会被高估 —— 高估的地板会让你
把真实的退化当成噪声放过去，方向恰恰是危险的那一边。

== 两个数字，别搞混 ==
- `delta`   ：两臂之间**实际观察到**的差（点估计）。理想情况下接近 0；
              它有多大，就说明"运气"有多大。
- `resolution`：配对 bootstrap 区间的**半宽**。含义是「小于这个的 delta 看不出来」，
              所以它才是门槛的下界。**报告里应该用这一个。**
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from lab.bench.runner import SuiteResult
from lab.bench.stats import mean, paired_bootstrap_ci

NOISE_FLOOR_FILE = "noise_floor.json"


class NoiseMetric(BaseModel):
    """一个指标的噪声地板。"""

    name: str                       # 机器可读名（success_pt / cost_pct / ...）
    label: str                      # 显示名
    unit: str = ""
    delta: float = 0.0              # 观测到的两臂差值（点估计）
    resolution: float = 0.0         # 95% 配对区间半宽 = "小于它看不出来"
    per_task: List[float] = Field(default_factory=list)  # 逐任务差值（看有没有被个别任务主导）

    @property
    def worst_task(self) -> float:
        """绝对值最大的那个任务级差值。

        为什么给这个数：如果地板完全由一个任务贡献，那说明**那个任务本身不稳定**，
        而不是"整个测量系统抖"。两者的处理方式完全不同
        （前者修任务，后者放弃这个指标的细粒度结论）。
        """
        return max(self.per_task, key=abs) if self.per_task else 0.0


class NoiseFloor(BaseModel):
    """一次噪声地板测量。"""

    suite_id: str
    label: str = ""
    arms: List[str] = Field(default_factory=list)
    runs_per_task: int = 1
    task_count: int = 0
    model_name: str = ""
    temperature: float = 0.0
    guard_config: str = ""
    fault_spec: str = ""
    interleaved: bool = False
    created_at: str = ""
    metrics: List[NoiseMetric] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)

    def get(self, name: str) -> Optional[NoiseMetric]:
        return next((item for item in self.metrics if item.name == name), None)

    def resolution(self, name: str, default: float = 0.0) -> float:
        metric = self.get(name)
        return metric.resolution if metric else default


def measure_noise_floor(
        suite: SuiteResult,
        *,
        label: str = "",
        include_uids: Optional[Sequence[str]] = None,
) -> NoiseFloor:
    """从"两个同配置臂交错跑出来"的 suite 计算噪声地板。

    :param include_uids: 只统计这些任务。
        为什么要这个参数（而不是要求调用方先把 suite 筛好）：
        `--purpose regression` 的本意是把 capability 任务排除在外，
        而那是一个**分析口径**决定。把口径决定放在分析阶段，
        就能在同一份数据上免费重算，也避开了"跑的时候漏传过滤条件"
        这种安静的错误 —— 本功能就刚踩过一次（跑出了 8 个任务才发现）。
    """
    excluded: List[str] = []
    if include_uids is not None:
        keep = set(include_uids)
        excluded = sorted({o.task_key for o in suite.outcomes if o.task_uid not in keep})
        if excluded:
            suite = suite.only_tasks(keep)

    arms = suite.arm_labels
    if not arms:
        raise ValueError(
            "分析口径与 suite 内容不匹配：筛完之后一个任务都不剩。"
            f"（suite 里有 {len({o.task_uid for o in suite.outcomes})} 个任务，"
            "而 include_uids 指向了另一批）—— 检查 --group / --purpose 是否一致。"
        )
    if len(arms) != 2:
        raise ValueError(
            f"噪声地板需要**恰好 2 个臂**（同一配置、不同名字），当前是 {len(arms)} 个：{arms}。"
            "用法：--ab-guard all --ab-guard all --ab-label noise-a --ab-label noise-b"
        )

    left, right = suite.for_arm(arms[0]), suite.for_arm(arms[1])
    floor = NoiseFloor(
        suite_id=suite.suite_id,
        label=label or suite.label,
        arms=arms,
        runs_per_task=suite.runs_per_task,
        task_count=len({o.task_uid for o in left.outcomes}),
        model_name=suite.model_name,
        temperature=suite.temperature,
        guard_config=suite.guard_config,
        fault_spec=suite.fault_spec,
        interleaved=suite.interleaved,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )

    # ---- 成功率（百分点）----
    rate = _paired_rate_diff(left, right)
    floor.metrics.append(NoiseMetric(
        name="success_pt", label="成功率", unit="pt",
        delta=rate["delta"], resolution=rate["resolution"], per_task=rate["per_task"],
    ))

    # ---- 连续量（相对变化 %）----
    for field, name, unit in (
            ("cost_usd", "cost_pct", "成本"),
            ("tokens", "tokens_pct", "token"),
            ("elapsed_ms", "elapsed_ms_pct", "耗时"),
            ("tool_calls", "tool_calls_pct", "工具调用"),
    ):
        metric = _paired_relative_diff(left, right, field, name, unit)
        if metric is not None:
            floor.metrics.append(metric)

    # ---- 必须跟着数字一起说的限制 ----
    if excluded:
        floor.notes.append(
            f"本次分析**排除了 {len(excluded)} 个任务**：{', '.join(excluded)}。"
            "（capability 任务的波动是「能力边界」，算进测量分辨率会让地板大到没意义）"
            "注意：排除发生在**分析**阶段，跑的时候它们在数据里 —— "
            "`task_count` 已按排除后的任务数算。"
        )
    if not suite.interleaved:
        floor.notes.append(
            "⚠️ 这次**没有交错**（两臂是分两段跑的）→ 地板里混进了时间漂移，"
            "**被高估**。高估的地板会把真实退化当噪声放过去，方向是危险的那一边。"
            "请用 `bench noise-floor`（它强制交错）重测。"
        )
    floor.notes.append(
        f"样本量是**任务数 n={floor.task_count}**（每个任务每臂 {floor.runs_per_task} 次）。"
        "想让分辨率变细必须**加任务**或加次数，不是靠多跑几轮换个说法。"
    )
    if floor.task_count < 3:
        # 小样本地板**不能写进会被门禁读的默认位置**。
        # 定向重测（--task）本来就是为了看某个任务的变化量，不是为了得到可用的阀值。
        floor.notes.append(
            f"🔴 **n={floor.task_count} 太小，这份地板不能当门禁阀值用**："
            "配对区间在半宽上根本无法稳定（且 n=1 时直接塔成 0）。"
            "它只适合用来**定向对比某个任务的变化量**。要用它当阀值请跑全套。"
        )
    floor.notes.append(
        "本测量假设\"两臂除了名字没有差别\"。若它们真的只是同一配置的两份，"
        "那 `delta` 就是纯噪声；如果 `delta` 大得离谱，先怀疑**实验装置**"
        "（例如某个臂的工作区没隔离干净），而不是先相信这个地板。"
    )
    # 离散指标的一个陷阱：所有任务的差值都是 0 时，配对区间会**塔成 0**，
    # 看起来像"完全可复现"。而 0 宽度的区间只说明"这个样本里没有任务翻转"。
    success = floor.get("success_pt")
    if success is not None and success.resolution == 0 and success.delta == 0:
        floor.notes.append(
            "⚠️ 成功率的分辨率是 **±0.00pt**，但**不要**读成「成功率完全可复现」："
            "这是离散指标在「本样本内没有任何任务翻转」时的正常现象 —— 配对区间会塔成 0。"
            f"两臂各 {floor.task_count * floor.runs_per_task} 次都没翻过，在 90%+ 成功率的系统上是运气，不是证明。"
            "真实的成功率不确定性应该看 Wilson 区间（报告里的 [x%, y%]）。"
        )
    return floor


def _paired_rate_diff(left: SuiteResult, right: SuiteResult) -> Dict[str, Any]:
    """逐任务成功率差值（百分点）+ 配对区间半宽。"""
    a = _rate_by_task(left)
    b = _rate_by_task(right)
    shared = sorted(set(a) & set(b))
    diffs = [(b[key] - a[key]) * 100 for key in shared]
    return {"delta": mean(diffs), "resolution": _half_width(diffs), "per_task": diffs}


def _paired_relative_diff(
        left: SuiteResult,
        right: SuiteResult,
        field: str,
        name: str,
        unit_label: str,
) -> Optional[NoiseMetric]:
    """逐任务相对变化（%）+ 配对区间半宽。

    用**相对变化**而不是绝对值：成本/耗时的任务间差异是十几倍，
    绝对差值的分布完全由"哪个任务大"决定，跨任务平均没有意义。
    """
    a = _mean_by_task(left, field)
    b = _mean_by_task(right, field)
    shared = [key for key in sorted(set(a) & set(b)) if a[key]]
    if not shared:
        return None
    diffs = [(b[key] - a[key]) / a[key] * 100 for key in shared]
    return NoiseMetric(
        name=name, label=f"{unit_label}（相对）", unit="%",
        delta=mean(diffs), resolution=_half_width(diffs), per_task=diffs,
    )


def _half_width(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    interval = paired_bootstrap_ci(list(values))
    half = interval.half_width
    return float(half) if half is not None else 0.0


def _rate_by_task(suite: SuiteResult) -> Dict[str, float]:
    grouped: Dict[str, List[bool]] = {}
    for outcome in suite.outcomes:
        grouped.setdefault(outcome.task_key, []).append(bool(outcome.ok))
    return {key: (sum(values) / len(values)) for key, values in grouped.items() if values}


def _mean_by_task(suite: SuiteResult, field: str) -> Dict[str, float]:
    grouped: Dict[str, List[float]] = {}
    for outcome in suite.outcomes:
        grouped.setdefault(outcome.task_key, []).append(float(getattr(outcome, field) or 0))
    return {key: mean(values) for key, values in grouped.items() if values}


# ==================== 落盘 / 读取 ====================

def noise_floor_path() -> Path:
    from lab.bootstrap import ensure_runs_dir

    return ensure_runs_dir() / NOISE_FLOOR_FILE


def save_noise_floor(floor: NoiseFloor, path: Optional[Path] = None) -> Path:
    target = Path(path) if path else noise_floor_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(floor.model_dump_json(indent=2), encoding="utf-8")
    return target


def load_noise_floor(path: Optional[Path] = None) -> Optional[NoiseFloor]:
    target = Path(path) if path else noise_floor_path()
    if not target.exists():
        return None
    try:
        return NoiseFloor.model_validate_json(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        # 地板文件坏了就当作"没测过"，而不是让 gate 崩掉。
        # 门禁是 CI 路径上的东西，"辅助信息读不出来"不该阻断判定。
        return None


def render_noise_floor(floor: NoiseFloor) -> str:
    lines: List[str] = []
    lines.append("## 噪声地板（同一配置跑两次能差多少）")
    lines.append("")
    lines.append(f"- suite: `{floor.suite_id[:8]}`（{floor.label}）")
    lines.append(f"- 臂: {' / '.join(floor.arms)}"
                 f"{'（✅ 已交错）' if floor.interleaved else '（❌ 未交错）'}")
    lines.append(f"- 规模: {floor.task_count} 任务 × 每臂 {floor.runs_per_task} 次"
                 f"，每臂 n={floor.task_count * floor.runs_per_task}")
    lines.append(f"- 配置: guard=`{floor.guard_config or 'all'}`"
                 f" fault=`{floor.fault_spec or '无'}` model=`{floor.model_name}`"
                 f" T={floor.temperature}")
    lines.append("")
    lines.append("| 指标 | 观测差值 | **测量分辨率（±）** | 单任务最大 | 被个别任务主导? |")
    lines.append("|---|---|---|---|---|")
    for metric in floor.metrics:
        dominant = "⚠️ 是" if abs(metric.worst_task) > max(abs(metric.delta) * 3, 1e-9) else "否"
        lines.append(
            f"| {metric.label} | {metric.delta:+.2f}{metric.unit} "
            f"| **±{metric.resolution:.2f}{metric.unit}** "
            f"| {metric.worst_task:+.2f}{metric.unit} | {dominant} |"
        )
    lines.append("")
    lines.append("> **怎么用**：任何小于 `±分辨率` 的 delta 都**不能**当结论 —— "
                 "它落在\"同一份代码重跑一次\"的正常波动范围内。")
    lines.append("> `观测差值` 理想情况接近 0（两臂配置相同）。它偏大说明运气有影响；"
                 "它偏大**且**分辨率也大，说明当前样本量不足以支撑细粒度结论。")
    lines.append("")
    for note in floor.notes:
        lines.append(f"> {note}")
    lines.append("")
    return "\n".join(lines)
