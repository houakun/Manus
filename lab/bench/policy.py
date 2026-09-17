#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按历史分位数推导预算阈值（per-task）。

== 为什么必须是 per-task，而不是一个全局数字 ==
实测数据（Step 4 的两组基线）：

| 任务 | tokens 均值 |
|---|---|
| `sem_sqlite_report` | 414,318 |
| `sem_refactor_config` | 320,581 |
| `syn_date_diff` | ~30,000（数量级更低） |

**任务之间的成本差异（13 倍以上）远大于任务内的方差**。
任何单一数字都必然在某些任务上太紧（误杀）、在另一些上太松（等于没有预算）。

== 分层回退（每一层都有明确理由）==
    1. 该任务的 ≥ min_samples 次历史 → 用它的 P95 / P99
    2. 否则用该任务所在分组的 P95 / P99（样本稍多但更粗）
    3. 否则用全局默认值（并标 source=default，让人知道这不是校准过的）

== 为什么阈值必须随 SUT 版本重算 ==
历史数据来自旧版本的 SUT。改动 SUT（例如加了上下文压缩）会让成本分布整体位移，
旧阈值就不再代表"正常范围"。所以策略里带 `source`，
报告里会写清楚阈值是怎么来的 —— 换版本后应该重跑基线再重新推导。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from lab.bench.task import BenchTask
from lab.guard.budget import BudgetPolicy

# 从 bench_runs 里取的列 → 阈值维度名
_METRIC_COLUMNS = {
    "tokens": "tokens",
    "cost_usd": "cost_usd",
    "elapsed_s": "elapsed_ms",
    "tool_calls": "tool_calls",
    "steps": "steps_done",
}

MIN_SAMPLES = 3  # 少于 3 次就不该谈分位数（P99 会退化成最大值）


def _metrics_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, List[float]]:
    metrics: Dict[str, List[float]] = {}
    for name, column in _METRIC_COLUMNS.items():
        values = []
        for row in rows:
            raw = row.get(column)
            if raw is None:
                continue
            value = float(raw)
            if name == "elapsed_s":
                value /= 1000.0  # 库里存毫秒
            values.append(value)
        if values:
            metrics[name] = values
    return metrics


def policies_from_history(
        store: Any,
        tasks: List[BenchTask],
        *,
        soft_percentile: float = 0.95,
        hard_percentile: float = 0.99,
        min_samples: int = MIN_SAMPLES,
) -> Dict[str, BudgetPolicy]:
    """为每个任务推导一个预算策略（只包含能推导出来的任务）。

    返回的字典只含"有足够历史"的任务；其余由调用方用分组级或默认策略兜底。
    """
    task_rows = store.load_runs_grouped_by_task()
    group_rows: Dict[str, List[Dict[str, Any]]] = {}
    for task_key, rows in task_rows.items():
        group = rows[0].get("group_name") or "unknown"
        group_rows.setdefault(group, []).extend(rows)

    policies: Dict[str, BudgetPolicy] = {}
    for task in tasks:
        rows = task_rows.get(task.key) or []
        if len(rows) >= min_samples:
            policies[task.key] = BudgetPolicy.from_metrics(
                _metrics_from_rows(rows),
                soft_percentile=soft_percentile,
                hard_percentile=hard_percentile,
                source=f"history:task({len(rows)}次)",
            )
            continue
        fallback = group_rows.get(task.group) or []
        if len(fallback) >= min_samples:
            policies[task.key] = BudgetPolicy.from_metrics(
                _metrics_from_rows(fallback),
                soft_percentile=soft_percentile,
                hard_percentile=hard_percentile,
                source=f"history:group({len(fallback)}次)",
            )
    return policies


def describe_policies(policies: Dict[str, BudgetPolicy], tasks: List[BenchTask]) -> str:
    """把推导结果渲染成可读的表（跑之前打印，让人知道阈值是从哪来的）。"""
    lines = ["| 任务 | 来源 | 样本 | 软线 tokens | 硬线 tokens | 软线工具 | 硬线工具 |",
             "|---|---|---|---|---|---|---|"]
    for task in tasks:
        policy = policies.get(task.key)
        if policy is None:
            lines.append(f"| `{task.key}` | default | — | 150,000 | 375,000 | 15 | 38 |")
            continue
        lines.append(
            f"| `{task.key}` | {policy.source} | "
            f"{policy.source.split('(')[-1].rstrip('次)') if '(' in policy.source else '-'} | "
            f"{policy.max_tokens:,.0f} | {policy.hard_tokens:,.0f} | "
            f"{policy.max_tool_calls:,.0f} | {policy.hard_tool_calls:,.0f} |"
        )
    return "\n".join(lines)
