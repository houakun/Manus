#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从落盘证据重建一次评测（崩溃恢复）。

== 为什么这件事是**忠实**的，不是猜的 ==
恢复依赖两个事实：
1. 判定器是**纯函数**：`判定结论 = f(任务规则, 工作区里的文件)`。
   所以"重新判定一遍"与"当时判定一遍"结果完全一致（验证器不依赖时间/随机数/网络）。
2. 运行指标（token/成本/耗时/加固层观察）已经由 `run_task` 写进 `tasks` 表，
   并且 `tasks.workspace` 字段就是该次运行的工作区路径 —— 可以用它反查。

于是重建 = 遍历工作区 → 按路径反查指标 → 重新跑判定器 → 重新评过程规则。

== 什么情况下不能用它 ==
- 工作区被删了（那就真的没了）；
- 判定规则在那之后被改过（会得到"按新规则"的结论，而不是当时的结论）。

第二条是个真实的陷阱：如果你在评测之后加固了判定器（本项目就发生过：
`syn_rename_files` 的内容校验就是后来加的），恢复出来的结论会**更严格**。
所以恢复时必须把这件事写进报告备注，而不是静静地把数字换掉。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from lab.bench.runner import RunOutcome, SuiteResult, summarize_task
from lab.bench.task import BenchTask, load_all_tasks
from lab.bench.verifier import evaluate_process, run_checks
from lab.infra.local_sandbox import LocalSandbox
from lab.sut.base import StepStats, TaskResult
from lab.usage import Usage


def _workspace_layout(bench_root: Path, group: Optional[str] = None) -> List[tuple]:
    """扫描 bench_root 下所有"已完成的工作区"，返回 (task_uid, run_index, arm, workspace)。

    group 过滤不是可选项而是必需：bench_root 下同时存着多个分组的产物
    （实测踩过：重建"semireal n=5"时把之前跑过的 synthetic 工作区也扫了进去，
    结果数字里混进 4 次不属于本次评测的运行）。

    目录名有两种形态：`run3`（单臂）与 `run3-guard-none`（交错多臂）。
    后者不能靠 `int(name.replace("run", ""))` 解析 —— 那会抛 ValueError，
    而旧代码是 `continue` **静默跳过**：恢复出来的评测会少掉整条臂的样本，
    却看不出任何异常。所以这里用正则，并把臂名一并带出来。
    """
    found: List[tuple] = []
    pattern = re.compile(r"^run(\d+)(?:-(.+))?$")
    for workspace in sorted(bench_root.glob("*/*/run*/workspace")):
        if not workspace.is_dir():
            continue
        group_name = workspace.parents[2].name
        if group and group_name != group:
            continue
        key = workspace.parents[1].name
        run_dir = workspace.parent.name  # run0 / run1 / run2-guard-none ...
        match = pattern.match(run_dir)
        if match is None:
            continue
        run_index = int(match.group(1))
        arm = match.group(2) or ""
        found.append((f"{group_name}/{key}", run_index, arm, workspace))
    return found


def _task_result_from_row(row: Dict[str, Any], workspace: Path) -> TaskResult:
    """把 `tasks` 表的一行还原成足以跑过程规则的 TaskResult。

    只填过程评估真正用到的字段；其它字段留空并明确注释，
    避免读者以为这是"完整还原了一次运行"。
    """
    usage = Usage()
    usage.llm_calls = int(row.get("llm_calls") or 0)
    usage.llm_errors = int(row.get("llm_errors") or 0)
    usage.total_tokens = int(row.get("total_tokens") or 0)
    usage.tool_calls = int(row.get("tool_calls") or 0)

    guard: Dict[str, Any] = {
        "loop": {
            "tool_calls": float(row.get("tool_calls") or 0),
            "action_diversity": row.get("action_diversity"),
        },
        "budget": {
            "violated_metrics": _json_list(row.get("budget_violated_metrics")),
            "would_stop": bool(row.get("budget_would_stop")),
        },
        "postconditions": {"warnings": [""] * int(row.get("postcondition_warnings") or 0)},
    }
    return TaskResult(
        task_id=row.get("task_id") or "",
        sut_name=row.get("sut_name") or "",
        ok=bool(row.get("ok")),
        error=row.get("error"),
        error_type=row.get("error_type"),
        plan_title=row.get("plan_title") or "",
        steps=StepStats(total=int(row.get("steps_total") or 0), done=int(row.get("steps_done") or 0)),
        llm_usage=usage,
        cost_usd=float(row.get("cost_usd") or 0.0),
        elapsed_ms=int(row.get("elapsed_ms") or 0),
        workspace=str(workspace),
        guard=guard,
    )


def _json_list(raw: Optional[str]) -> List[str]:
    import json

    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


async def recover_suite(
        *,
        bench_root: Path,
        store: Any,
        suite_id: str,
        label: str = "",
        runs_per_task: Optional[int] = None,
        note: str = "",
        group: Optional[str] = None,
) -> SuiteResult:
    """从工作区 + 轨迹库重建一次评测，并写入 store。"""
    tasks_by_uid: Dict[str, BenchTask] = {task.uid: task for task in load_all_tasks()}

    outcomes: List[RunOutcome] = []
    skipped: List[str] = []

    for task_uid, run_index, arm, workspace in _workspace_layout(bench_root, group=group):
        task = tasks_by_uid.get(task_uid)
        if task is None:
            skipped.append(f"{task_uid}（任务已从任务集中移除）")
            continue

        # 1.反查这次运行的指标
        row = store.load_task_by_workspace(str(workspace))
        if row is None:
            # 工作区在但轨迹不在：可能被清理过。不伪造数据，直接跳过并记录。
            skipped.append(f"{task_uid} run{run_index}（找不到对应轨迹）")
            continue

        # 2.重新跑判定器（纯函数，结果与当时一致 —— 除非规则被改过）
        sandbox = LocalSandbox(workspace)
        checks = await run_checks(task.verify, sandbox)
        ok = all(check.ok for check in checks) and bool(checks)

        # 3.重新评过程规则
        result = _task_result_from_row(row, workspace)
        flags = evaluate_process(task, result)
        guard = result.guard or {}

        outcomes.append(RunOutcome(
            run_id=f"recovered-{task_uid.replace('/', '-')}-run{run_index}"
                   + (f"-{arm}" if arm else ""),
            suite_id=suite_id,
            task_uid=task_uid,
            task_key=task.key,
            group=task.group,
            run_index=run_index,
            ok=ok,
            # 旧数据没记录自述字段 → 标为未知（不能当成"没虚报"）
            self_reported_ok=bool(row.get("ok")),
            failed_checks=[c.line() for c in checks if not c.ok],
            process_flags=flags,
            task_id=row.get("task_id") or "",
            sut_name=row.get("sut_name") or "",
            tokens=int(row.get("total_tokens") or 0),
            cost_usd=float(row.get("cost_usd") or 0.0),
            elapsed_ms=int(row.get("elapsed_ms") or 0),
            llm_calls=int(row.get("llm_calls") or 0),
            tool_calls=float(row.get("tool_calls") or 0),
            steps_done=int(row.get("steps_done") or 0),
            error_type=row.get("error_type"),
            error=row.get("error"),
            budget_violated=_json_list(row.get("budget_violated_metrics")),
            budget_would_stop=bool(row.get("budget_would_stop")),
            action_diversity=row.get("action_diversity"),
            postcondition_warnings=int(row.get("postcondition_warnings") or 0),
            faults_injected=int(row.get("faults_injected") or 0),
            workspace=str(workspace),
            # 臂名从**目录名**恢复（它已被规范化成 slug）。
            # `for_arm()` 两边都过同一个 slug 函数，所以按原始 label 也能筛到。
            arm=arm,
        ))

    suite = SuiteResult(
        suite_id=suite_id,
        label=label or "恢复的评测",
        runs_per_task=runs_per_task or max((o.run_index for o in outcomes), default=0) + 1,
        started_at=datetime.now().isoformat(timespec="seconds"),
        outcomes=outcomes,
        task_summaries=[
            summarize_task(task, [o for o in outcomes if o.task_uid == uid])
            for uid, task in tasks_by_uid.items()
            if any(o.task_uid == uid for o in outcomes)
        ],
    )
    suite.notes.append(
        "本次数据由 `lab bench recover` 从工作区 + 轨迹库**重建**："
        "判定器是纯函数，所以重新判定的结论与当时一致。"
    )
    if note:
        suite.notes.append(note)
    if skipped:
        suite.notes.append(
            f"有 {len(skipped)} 个工作区无法恢复（未计入成功率）：{skipped[:5]}"
        )

    # 4.落库（走增量接口，与正常评测同一套表结构）
    store.save_suite_header(suite)
    for outcome in outcomes:
        store.save_outcome(outcome)
    store.finalize_suite(suite)
    return suite
