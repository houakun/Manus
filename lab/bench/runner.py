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
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from lab.api import run_task
from lab.bench.stats import Interval, TaskLevelRates, mean_ci, wilson_interval
from lab.bench.task import BenchTask, load_all_tasks
from lab.bench.verifier import CheckResult, evaluate_process, run_checks
from lab.infra.local_sandbox import LocalSandbox


class ArmConfig(BaseModel):
    """交错 A/B 的一"臂"：一套完整的实验条件。

    == 一个直白的提醒：交错是**唯一**能分离时间混淆的办法 ==
    两组跑在不同时段时，服务端整体变快/变慢会同时影响两边，伪装成"方案差异"。
    本项目实测过这个坑（v1 → v3 的成本 -22%，机制上说不通，最可能就是时段漂移）。
    把 A 与 B 交替排（A,B,A,B…）之后，漂移对两边的期望影响相同。

    == 为什么要 `label` 而不是用 guard label ==
    噪声地板实验需要两组**完全相同的配置**（唯一的差别是"哪一组"）。
    如果臂的身份靠配置标签区分，那两组会归并成同一组，实验就做不成。
    """

    label: str
    # 原始 spec（用于把复现命令一字不差地写进报告）
    guard_spec: str = ""
    guard_label: str = "all"
    fault_spec: str = ""
    replay: Optional[str] = None
    # 实际对象。用 Any 是为了不把 guard / faults 的导入链拉进本模块
    # （runner 已经被 api 依赖，再往下拉容易形成循环）。
    guard_config: Any = None
    fault_rules: Any = None

    def slug(self) -> str:
        """目录/文件名用的安全名字。

        必须稳定且**互不相同**：工作区是按它分目录的，重名会让两个臂
        写同一份产物 —— 那正是"实验污染"，而且不会报错，只会让成功率虚高。
        """
        return arm_slug(self.label)

    def describe(self) -> str:
        parts = [f"guard={self.guard_label or 'all'}"]
        if self.fault_spec:
            parts.append(f"fault={self.fault_spec}")
        if self.replay:
            parts.append(f"replay={self.replay}")
        return f"{self.label}[{' '.join(parts)}]"

    def to_metadata(self) -> Dict[str, Any]:
        """可 JSON 序列化的元数据（落库用；不含那两个实际对象）。"""
        return {
            "label": self.label,
            "guard_spec": self.guard_spec,
            "guard_label": self.guard_label,
            "fault_spec": self.fault_spec,
            "replay": self.replay,
        }


def arm_slug(label: str) -> str:
    """臂名 → 文件系统安全的名字（工作区目录 + 配对对比时用作身份）。

    统一成一个函数而不是各写一份：`for_arm()` 需要把"目录里的 slug"
    与"元数据里的 label"对应上，两处的规范化规则一旦不一样，
    拆臂时会安静地拆出空集 —— 而那看起来就像"那个臂全失败了"。
    """
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", str(label)).strip("-")
    return text or "arm"


def fault_spec_of(fault_rules: Any) -> str:
    """把故障规则列表压成一行规格字符串（落库 + 分组用）。

    为什么不能只存 `faults_injected` 计数：计数答不了"注入了**哪一类**"。
    做到 10 类故障 × 2 种加固的矩阵时，光靠计数，分组只能靠人工写在 label 里。
    """
    if not fault_rules:
        return ""
    return ";".join(rule.describe() for rule in fault_rules)


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
    # 加固配置标签（"all" / "none" / "retry+postconditions" ...）。
    # 必须有：否则"无加固"与"加固"两组数据落库后无法区分是谁的，报告也就无法归因。
    guard_config: str = "all"
    # 注入的故障**规格**（`partial_write@write_file(rate=0.5)`）。
    # `faults_injected` 只有次数，答不了"哪一类" —— 故障矩阵的前提是这一列。
    fault_spec: str = ""
    # 交错 A/B 里这条运行属于哪个臂
    arm: str = ""
    # LLM 录制回放模式（off / record / reuse / replay）
    replay_mode: str = "off"
    # 回放未命中的次数。**非 0 就说明这次运行不是完整复现** ——
    # 实测：严格回放未命中 6 次时，SUT 仍交付了合格产物（因为它在失败后降级继续了），
    # 判定器于是给出 OK。如果只看 ok，一次"只剩半条轨迹"的运行会被当成干净的成功。
    replay_misses: int = 0

    @property
    def replay_degraded(self) -> bool:
        return self.replay_misses > 0


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
    # ---- 实验条件（Step 6）----
    # 这三项必须在 suite 上也有：跑完十几个 suite 后要能"一条 SQL 找出同配置的几次"，
    # 而不是把几万行 runs 全读进内存再在 Python 里比。
    guard_config: str = ""
    fault_spec: str = ""
    replay_mode: str = "off"
    # 交错 A/B 的臂元数据（[{label, guard_label, fault_spec, replay}, ...]）
    arms: List[Dict[str, Any]] = Field(default_factory=list)
    interleaved: bool = False
    task_summaries: List[TaskSummary] = Field(default_factory=list)
    outcomes: List[RunOutcome] = Field(default_factory=list)
    fixture_failures: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    # 旧数据（在加 self_reported_ok 字段之前跑的）没有这个字段。
    # 用显式标志而不是把 NULL 当成 False —— 把"未知"当成"没虚报"就是造假数据。
    self_report_available: bool = True

    # ---- 实验条件与交错 ----

    @property
    def arm_labels(self) -> List[str]:
        """本 suite 里实际出现过的臂（按首次出现顺序）。"""
        seen: List[str] = []
        for outcome in self.outcomes:
            if outcome.arm and outcome.arm not in seen:
                seen.append(outcome.arm)
        return seen

    @property
    def is_multi_arm(self) -> bool:
        return len(self.arm_labels) > 1

    def for_arm(self, arm: str) -> "SuiteResult":
        """筛出一个臂的子 suite（把交错跑出来的一个 suite 拆成可对比的两份）。

        == 为什么不做成"一个臂一个 suite" ==
        交错的意义在于**同一个 suite 内**两条臂共享同一段时间。
        如果拆成两个 suite 再分别跑，就回到了"两组跑在不同时段"的老问题上。
        所以：跑的时候是一个 suite，分析的时候才拆。

        ⚠️ 拆出来的两份**不再是独立样本**（它们交错共享了时间），
        所以配对 bootstrap 的区间要按配对设计读（两臂按 task 配对），
        不能当成两次独立评测来算。
        """
        outcomes = [o for o in self.outcomes if arm_slug(o.arm) == arm_slug(arm)]
        tasks = {}
        for outcome in outcomes:
            tasks.setdefault(outcome.task_uid, None)
        summaries = []
        for uid in tasks:
            key = next(o.task_key for o in outcomes if o.task_uid == uid)
            group = next(o.group for o in outcomes if o.task_uid == uid)
            summaries.append(_summarize_by_fields(
                uid, key, group, [o for o in outcomes if o.task_uid == uid]
            ))
        import copy as _copy

        clone = _copy.deepcopy(self)
        clone.suite_id = f"{self.suite_id}#{arm_slug(arm)}"
        clone.label = f"{self.label} / arm={arm}"
        clone.outcomes = outcomes
        clone.task_summaries = summaries
        clone.arms = [item for item in self.arms if arm_slug(item.get("label", "")) == arm_slug(arm)]
        return clone

    def only_tasks(self, uids: Any) -> "SuiteResult":
        """只保留指定任务的子集（用于"只看 regression 任务"这类口径切换）。

        为什么要单独一个方法：`pass^k` 的样本量是**任务数**，
        所以"算哪几个任务"直接改变结论。这种口径切换必须在**同一份数据上重算**，
        而不是重跑一次 —— 否则你无法区分"换口径"与"这次运气不同"。
        """
        keep = set(uids)
        outcomes = [o for o in self.outcomes if o.task_uid in keep]
        summaries = [
            _summarize_by_fields(
                uid,
                next(o.task_key for o in outcomes if o.task_uid == uid),
                next(o.group for o in outcomes if o.task_uid == uid),
                [o for o in outcomes if o.task_uid == uid],
            )
            for uid in sorted({o.task_uid for o in outcomes})
        ]
        import copy as _copy

        clone = _copy.deepcopy(self)
        clone.outcomes = outcomes
        clone.task_summaries = summaries
        return clone

    def config_fingerprint(self) -> Dict[str, Any]:
        """实验条件指纹（用于判断两次评测**能不能互相比较**）。

        含任务集、模型、温度、加固、故障、回放模式。
        任意一项不同 → 两边的差异里就混了"不是同一个实验"的成分，
        而这是最容易被忽略、后果最严重的一种比较错误。
        """
        return {
            "task_set": sorted({o.task_uid for o in self.outcomes}),
            "model_name": self.model_name,
            "temperature": self.temperature,
            "guard_config": self.guard_config,
            "fault_spec": self.fault_spec,
            "replay_mode": self.replay_mode,
        }

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
        """自报成功但被判定失败的次数（即"虚报成功"）。"""
        return sum(1 for o in self.outcomes if o.self_reported_ok and not o.ok)

    @property
    def false_negative_runs(self) -> int:
        """SUT 自报失败但产物其实是合格的次数（即"误报失败"）。

        为什么这个方向也必须统计：它和虚报成功一样会让指标失真，但方向相反 ——
        只看自述会**低估**成功率，并把"做完了但最后一步报错/超时/多问了一句"
        这类情况全部当成能力不足。实测中这类占 3/38（8%），比虚报还多。
        典型的三个原因（都来自实测）：
          - 活干完了，但汇总阶段的 LLM 调用失败；
          - 活干完了，但 Agent 又去问用户问题（WaitEvent，headless 下无法继续）；
          - 活干完了，但整任务超时被砍。
        三者共同说明：**"任务完成"应该看交付物，而不是看 Agent 有没有好好收尾**。
        """
        return sum(1 for o in self.outcomes if not o.self_reported_ok and o.ok)

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

    @property
    def replay_missed_runs(self) -> int:
        """回放未命中的运行数（这些运行的轨迹**不是**完整复现）。"""
        return sum(1 for o in self.outcomes if o.replay_degraded)

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

    def task_level(self, only_uids: Optional[set] = None) -> "TaskLevelRates":
        """任务级指标：pass^k（k 次全对的任务占比）/ pass@k。

        为什么要单独一个口径：运行级成功率会把"少数任务不稳定"平摊掉 ——
        semireal v1 运行级 95%（38/40）看起来与 v3 的 100% 没区别，
        但任务级 pass^5 是 6/8 = 75%（两个任务 4/5）。
        生产上要的是"同一任务每次都做对"，那才是 pass^k 的语义。

        :param only_uids: 只统计这些任务（用于把 capability 任务从回归指标里剔除）。
        """
        from lab.bench.stats import task_level_rates

        grouped = _group_outcomes_by_task(self.outcomes)
        if only_uids is not None:
            grouped = {key: value for key, value in grouped.items() if key in only_uids}
        return task_level_rates(grouped)

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


def _budget_source_note(default_policy: Any, by_task: Optional[Dict[str, Any]]) -> str:
    """描述本次用的阈值是怎么来的（报告里必须有，否则数字无法归因）。"""
    if by_task:
        sources = {policy.source for policy in by_task.values()}
        return (
            f"**按任务的历史分位数推导**（{len(by_task)} 个任务有历史），"
            f"来源类型={sorted(sources)}；无历史的任务用默认值。"
            "注意：历史数据来自**旧版本 SUT**，换版本后应重跑基线再重新推导。"
        )
    if default_policy is not None:
        return f"统一使用 {default_policy.source} 策略"
    return "默认值"


def _group_outcomes_by_task(outcomes: List[RunOutcome]) -> Dict[str, List[bool]]:
    """按任务分组，每组是该任务各次运行的成败序列（按 run_index 排序）。"""
    grouped: Dict[str, List[tuple]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.task_uid, []).append((outcome.run_index, bool(outcome.ok)))
    return {key: [ok for _, ok in sorted(items)] for key, items in grouped.items()}


def _summarize_by_fields(
        task_uid: str,
        task_key: str,
        group: str,
        outcomes: List[RunOutcome],
        title: str = "",
) -> TaskSummary:
    """按字段汇总（不依赖 BenchTask 对象）。

    为什么要拆出来：`for_arm()` 要在一个已经有 outcomes 的 suite 上**重新汇总子集**，
    而那时手上只有 uid/key/group 三个字符串，没有 BenchTask。
    如果各写一份汇总逻辑，两边的口径早晚会飘（一处改了另一处没改）。
    """
    successes = sum(1 for o in outcomes if o.ok)
    flags: Dict[str, int] = {}
    for outcome in outcomes:
        for flag in outcome.process_flags:
            key = flag.split(":")[0]
            flags[key] = flags.get(key, 0) + 1

    return TaskSummary(
        task_uid=task_uid,
        task_key=task_key,
        group=group,
        title=title,
        runs=len(outcomes),
        successes=successes,
        success_rate=wilson_interval(successes, len(outcomes)),
        tokens=mean_ci([float(o.tokens) for o in outcomes]),
        cost_usd=mean_ci([float(o.cost_usd) for o in outcomes]),
        elapsed_ms=mean_ci([float(o.elapsed_ms) for o in outcomes]),
        tool_calls=mean_ci([float(o.tool_calls) for o in outcomes]),
        flag_counts=flags,
    )


def summarize_task(task: BenchTask, outcomes: List[RunOutcome]) -> TaskSummary:
    """把同一任务的多次 run 汇总成 TaskSummary。"""
    return _summarize_by_fields(task.uid, task.key, task.group, outcomes, task.title)


def _reset_workspace(workspace: Path) -> None:
    """把工作区恢复到“什么都没发生过”的状态。

    == 为什么必须清（真实测到的泄漏）==
    “每次运行一个独立工作区”只对**不同 run_index** 成立。
    重跑同一批任务时，`run0` 会被复用 —— 而上一次的产物、脚本、
    甚至**判定层的检查脚本**都还在里面。后果有两层：

    1. **判定污染**：上一次写对的文件会让这一次“什么都不做也通过”，
       成功率虚高而且不报错（这正是当初“每个 run 一个目录”想防的事，只是没防住重跑）；
    2. **判定标准泄漏**：实测出现过 Agent 穿越到**另一个 run 的**工作区去读
       `tmp/_lab_check_*.py`（评分脚本）—— 那就是“猜对了”而不是“改对了”。

    所以起始必须干净。只删 `workspace` 这一层里面的东西，
    且路径完全由 harness 自己构造（不会指向用户数据）。
    """
    if workspace.exists():
        for child in workspace.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                # 单个文件删不掉（被占用等）不能带走整次评测
                continue
    workspace.mkdir(parents=True, exist_ok=True)


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
        guard_config: Any = None,
        arm: Optional[ArmConfig] = None,
        arm_in_workspace: bool = False,
        replay: Optional[str] = None,
        replay_ignore_volatile: Optional[bool] = None,
        replay_cache: Any = None,
) -> RunOutcome:
    """跑一次任务：铺输入 → 真跑 SUT → 判定 → 评估过程。

    :param arm_in_workspace: 是否把臂名编进工作区目录。
        只在**交错多臂**时开：单臂时保持 `run0` 的原名，
        否则老的工作区/恢复路径/人的肌肉记忆全部错位
        （`bench recover` 靠 `run<N>` 的目录名反推运行序号）。
    """
    run_id = str(uuid.uuid4())
    # 交错 A/B 时两个臂的 run_index 是一样的，共用目录会让两个臂写同一份产物 ——
    # 实验污染，而且不会报错，只会让成功率虚高。
    arm_suffix = f"-{arm.slug()}" if (arm is not None and arm_in_workspace) else ""
    workspace = bench_root / task.group / task.key / f"run{run_index}{arm_suffix}" / "workspace"
    _reset_workspace(workspace)

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
        guard_config=guard_config,
        replay=replay,
        replay_ignore_volatile=replay_ignore_volatile,
        replay_cache=Path(replay_cache) if replay_cache else None,
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
    outcome.guard_config = str(guard.get("config") or (guard_config.label if guard_config else "all"))
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
    # 工具调用次数从**事件流**算（`result.tool_sequence`），而不是从 guard 的 loop_guard 算。
    # 为什么：`loop_guard` 是可以被 `--guard none` 关掉的能力 —— 一旦用它计数，
    # "关掉加固"的对照组就会显示 0 次工具调用，**指标被开关污染了**。
    # （实测踩过：对照组的运行全部显示 tools=0，看起来像"agent 没调工具"。）
    outcome.tool_calls = float(len(result.tool_sequence) or loop.get("tool_calls") or 0)
    outcome.steps_done = result.steps.done
    outcome.error_type = result.error_type
    outcome.error = result.error
    outcome.budget_violated = list(budget.get("violated_metrics") or [])
    outcome.budget_would_stop = bool(budget.get("would_stop"))
    outcome.action_diversity = loop.get("action_diversity")
    outcome.postcondition_warnings = len((guard.get("postconditions") or {}).get("warnings") or [])
    outcome.faults_injected = int((guard.get("faults") or {}).get("injections") or 0)
    outcome.error_types = dict(guard.get("error_types") or {})
    # ---- 实验条件的运行级元数据（没有它们，矩阵做出来也归档不了）----
    outcome.fault_spec = fault_spec_of(fault_rules)
    outcome.arm = arm.label if arm is not None else ""
    outcome.replay_mode = str((result.replay or {}).get("mode") or (replay or "off"))
    outcome.replay_misses = int((result.replay or {}).get("misses") or 0)
    if outcome.replay_degraded:
        # 把"回放不完整"变成一个**过程 flag**，而不是只埋在 replay 字典里。
        # 原因：实测过未命中 6 次仍然判定 OK 的情况（SUT 降级继续跑完了活）。
        # 只看 ok 的话，一次"只剩半条轨迹"的运行看起来跟正常成功没有区别。
        flags.append(f"replay_misses:{outcome.replay_misses}")
    return outcome


def build_schedule(
        tasks: Sequence[Any],
        runs_per_task: int,
        arms: Sequence[ArmConfig],
        *,
        interleave: bool,
) -> List[tuple]:
    """建调度表：返回 [(task, run_index, arm), ...] 的执行顺序。

    == 单臂（与改造前一致）==
        task × run 两层循环。

    == 多臂（交错）==
        按**轮次**排，每轮把臂的顺序轮转一位：
            第 0 轮: A, B     第 1 轮: B, A     第 2 轮: A, B
        轮转不是为了好看：如果永远是 A 先跑，A 就总是处在"刚启动、配额充足"的位置上，
        这会把位置效应伪装成 A 的优势。轮转让两条臂在位置上**对称**。

    抽成独立函数（而不是内联在 run_suite 里）是因为交错顺序**是实验设计本身**，
    必须有直接的单测锁住 —— 否则有人重构一下循环，实验就静默地失去交错性质，
    而所有数字看起来仍然正常。
    """
    schedule: List[tuple] = []
    if interleave and len(arms) > 1:
        for index in range(runs_per_task):
            offset = index % len(arms)
            rotated = list(arms[offset:]) + list(arms[:offset])
            for arm in rotated:
                for task in tasks:
                    schedule.append((task, index, arm))
    else:
        for task in tasks:
            for index in range(runs_per_task):
                schedule.append((task, index, arms[0]))
    return schedule


def _common_or_mixed(values: Sequence[str], *, empty: str = "mixed") -> str:
    """多臂之间该字段的公共值；不一致才是 "mixed"。

    为什么不直接写死 "mixed"：噪声地板实验的两个臂**配置完全相同**，
    写成 "mixed" 会让读报告的人以为自变量是加固 —— 那是对实验目的的彻底误读。
    """
    distinct = {value or "" for value in values}
    if len(distinct) == 1:
        return next(iter(distinct)) or ("" if empty == "" else "mixed")
    return ";".join(sorted(item for item in distinct if item)) or "mixed"


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
        budget_policy_by_task: Optional[Dict[str, Any]] = None,
        guard_config: Any = None,
        purpose: Optional[str] = None,
        fault_rules: Any = None,
        max_seconds: Optional[float] = None,
        arms: Optional[Sequence[ArmConfig]] = None,
        interleave: bool = True,
        replay: Optional[str] = None,
        replay_ignore_volatile: Optional[bool] = None,
        replay_cache: Any = None,
        progress: bool = True,
        on_start: Optional[Any] = None,
        on_outcome: Optional[Any] = None,
        on_finish: Optional[Any] = None,
) -> SuiteResult:
    """跑一个完整 suite（任务集 × n 次重复）。

    == 臂（arms）与交错（interleave）==
    传 `arms` 时，一个 suite 里会同时跑多套实验条件，并按**轮转顺序**交替排：

        第 0 轮: arm-A, arm-B
        第 1 轮: arm-B, arm-A      ← 顺序反过来，抵消"谁先跑"的位置效应
        第 2 轮: arm-A, arm-B

    这样才能分离"时间相关的服务端漂移"（两组跑在不同时段时，漂移会伪装成方案差异）。
    `runs_per_task` 的含义是**每个臂各跑几次**，所以总运行数 = 任务数 × 次数 × 臂数。

    噪声地板实验就是传入**两个配置完全相同、只有 label 不同**的臂：
    它们之间的差异没有任何因果解释，于是那条差异就是**测量分辨率**。

    三个回调是"断点续跑"的接口：
        on_start(suite)    先落 suite 元信息（在跑之前）
        on_outcome(outcome) 每完成一次运行就落库
        on_finish(suite)   结束时回写汇总
    为什么不用 gather：gather 只在全部完成后一次性返回，中途被中断就全丢。
    长评测（40 次运行 ≈ 半小时、花掉几美元）被中断的途径很多：Ctrl-C、
    机器休眠、终端关闭、远程会话断开。增量落库让这些情况最多丢一次运行。
    """
    from datetime import datetime

    from lab.config import load_llm_config

    tasks = load_all_tasks(group=group)
    if purpose:
        # 回归门禁只跑 regression 任务；capability 任务的波动会淹没小效应
        tasks = [t for t in tasks if t.purpose == purpose]
    if keys:
        # 定向运行个别任务：调试、只补跑失败任务、做单任务对照实验都用得上
        wanted = set(keys)
        tasks = [t for t in tasks if t.key in wanted or t.uid in wanted]
        missing = wanted - {t.key for t in tasks} - {t.uid for t in tasks}
        if missing:
            raise ValueError(f"未找到指定的任务: {sorted(missing)}")
    if limit:
        tasks = tasks[:limit]

    # ---- 臂：不传就是"单臂"，行为与改造前逐字节一致（向后兼容）----
    effective_arms: List[ArmConfig] = list(arms) if arms else [ArmConfig(
        label=label or "run",
        guard_label=getattr(guard_config, "label", "all"),
        guard_config=guard_config,
        fault_rules=fault_rules,
        fault_spec=fault_spec_of(fault_rules),
        replay=replay,
    )]
    if len(effective_arms) < 2:
        interleave = False
    slugs = [arm.slug() for arm in effective_arms]
    if len(set(slugs)) != len(slugs):
        raise ValueError(
            f"臂的名字必须互不相同（工作区按它分目录）：{[a.label for a in effective_arms]}"
        )

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
        # 两臂**配置相同**时不能写 "mixed"：那会让人以为自变量是加固。
        # 噪声地板实验正好就是这种情况（两个臂的 guard 一模一样），
        # 写成 mixed 会让后来读 `noise_floor.json` 的人完全误判这个实验在测什么。
        guard_config=_common_or_mixed([arm.guard_label for arm in effective_arms]),
        fault_spec=_common_or_mixed(
            [arm.fault_spec for arm in effective_arms], empty=""
        ),
        replay_mode=str(replay or "off"),
        arms=[arm.to_metadata() for arm in effective_arms],
        interleaved=interleave,
    )

    started = time.monotonic()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(task: BenchTask, index: int, arm: ArmConfig) -> RunOutcome:
        async with semaphore:
            if progress:
                tag = f" [{arm.label}]" if interleave else ""
                print(f"  → {task.uid} run{index + 1}/{runs_per_task}{tag}", flush=True)
            # 预算策略按任务选：优先用该任务自己的历史分位数推导出来的策略，
            # 没有历史就回退到传入的全局策略。
            # （任务之间的成本差异远大于任务内方差，一个全局数字必然对部分任务是错的）
            task_policy = (budget_policy_by_task or {}).get(task.key, budget_policy)
            return await run_once(
                task, suite_id=suite_id, run_index=index, bench_root=bench_root,
                model_name=llm_config.model_name, temperature=temperature,
                budget_policy=task_policy, fault_rules=arm.fault_rules,
                max_seconds=max_seconds, guard_config=arm.guard_config,
                arm=arm, arm_in_workspace=interleave, replay=arm.replay,
                replay_ignore_volatile=replay_ignore_volatile,
                replay_cache=replay_cache,
            )

    # 先落元信息：这样即使进程马上被杀，也能在库里看到"有一次评测开过、跑到哪了"
    if on_start is not None:
        on_start(suite)

    # ---- 调度顺序 ----
    # 单臂：task × run（与改造前一致）。
    # 多臂：按轮次交错，每轮把臂的顺序轮转一位 ——
    #       这样既让两条臂共享同一段时间，又抵消了"谁总是先跑"的位置效应。
    schedule = build_schedule(tasks, runs_per_task, effective_arms, interleave=interleave)

    # 用 as_completed 而不是 gather，才能在**每次运行完成时**立即回调（增量落库）
    pending = [asyncio.create_task(_one(task, index, arm)) for task, index, arm in schedule]

    collected: List[RunOutcome] = []
    for future in asyncio.as_completed(pending):
        try:
            outcome = await future
        except Exception as exc:  # noqa: BLE001
            # 单次运行出错不能带走整个 suite（否则一次网络抖动就毁掉整份评测）
            suite.fixture_failures.append(f"{type(exc).__name__}: {exc}")
            continue
        collected.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)

    suite.outcomes = collected
    suite.task_summaries = [
        summarize_task(task, [o for o in collected if o.task_uid == task.uid])
        for task in tasks
    ]
    suite.elapsed_s = round(time.monotonic() - started, 1)
    suite.notes.append(
        f"预算阈值来源：{_budget_source_note(budget_policy, budget_policy_by_task)}"
    )
    if interleave:
        arm_desc = " / ".join(arm.describe() for arm in effective_arms)
        suite.notes.append(
            f"**交错 A/B**：本 suite 内含 {len(effective_arms)} 个臂，按轮次交替运行（{arm_desc}）。"
            "交错是分离「时间相关的服务端漂移」的唯一办法；"
            "分析时用 `for_arm()` 拆开对比，**不能**当成两次独立评测算区间。"
        )
    else:
        suite.notes.append(
            f"加固配置：**{effective_arms[0].guard_label or 'all'}**"
            f"{('（' + effective_arms[0].guard_config.describe() + '）') if effective_arms[0].guard_config is not None else ''}"
            "。注意：`loop_guard` / `budget` 在 observe 模式下**只观测不干预**，"
            "真正影响成败的是 `retry` 与 `postconditions`。"
        )
    if effective_arms[0].fault_spec:
        suite.notes.append(
            f"故障注入：`{suite.fault_spec}`（写在**运行级** `fault_spec` 列，可 SQL 分组）。"
        )
    if str(replay or "off") != "off":
        suite.notes.append(
            f"⚠️ LLM 回放模式：**{replay}**。此时 `cost_usd` / 耗时是**等价量**，"
            "不是本次真实消费 —— 报告里的成本不能读成「这次花了这么多」。"
        )
        missed = suite.replay_missed_runs
        if missed:
            # 注：报告里另有一块专门渲染这件事（带数值与处置建议）。
            # 这里只留一行：`bench compare` 等读 notes 的地方也需要看到这个信号。
            suite.notes.append(
                f"🔴 回放不完整：{missed}/{suite.total_runs} 次运行在未命中后降级继续 —— "
                "这些样本的轨迹不是录制时那条，**不能用于对比**。"
            )
    if concurrency > 1:
        suite.notes.append(
            f"本次以并发 {concurrency} 运行：**耗时指标不可横向比较**"
            "（并行会互相竞争模型配额与本机资源）。"
        )
    if suite.fixture_failures:
        suite.notes.append(
            f"有 {len(suite.fixture_failures)} 次运行在执行前就失败（环境/网络问题），"
            "**未计入成功率** —— 否则会把环境问题当成 Agent 能力问题。"
        )
    if on_finish is not None:
        on_finish(suite)
    return suite
