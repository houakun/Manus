#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""lab 命令行入口。

为什么 Step 1 就要有 CLI：
- handoff 第 10 节要求"每个模块完成后你能自己讲一遍" —— 有一个能亲手跑的命令
  是"我真的跑通了"的唯一证据；
- CI 需要它：退出码非 0 表示任务失败（Step 4 的 harness 直接复用这个约定）；
- 未来加 `lab trace` / `lab bench` 子命令时，CLI 形状已经定好了。

用法：
    cd mooc-manus
    python -m lab run "把 1 到 100 求和，结果写入 /home/ubuntu/result.txt"
    python -m lab run "..." --json          # 输出完整结构化结果
    python -m lab run "..." --max-seconds 60
    python -m lab trace                      # 看最近一次运行的轨迹树
    python -m lab trace <task_id> --json     # 导出机器可读的轨迹
    python -m lab stats                      # 跨运行聚合指标
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from lab.api import LabConfigError, default_trace_store, fault_rules_from_spec, run_task_sync
from lab.bootstrap import ensure_runs_dir
from lab.bench.workspace import (
    UNKNOWN,
    check_freshness,
    compare_digest,
    render_digest_check,
    render_freshness,
)
from lab.sut.base import TaskResult
from lab.trace.view import render_trace


def _fix_console_encoding() -> None:
    """把 stdout/stderr 切到 UTF-8，避免在 Windows 控制台（代码页 936）崩掉。

    为什么必须做：`render_validations` 打印的 `✓` 在 GBK 里没有对应字符，
    于是 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'` ——
    而 README 的「三条命令快速开始」第一条和 CI 第二步都是
    `python -m lab bench validate`。命令本身跑完了，却崩在最后一行输出上。

    为什么不能只 `reconfigure(encoding="utf-8")`：GBK 控制台会按 GBK 解释
    这些字节，中文直接变乱码（比崩还难查）。所以**先切控制台代码页，再切流编码**。

    为什么还要 `errors="replace"`：输出被重定向、或 `SetConsoleOutputCP` 因权限失败时，
    宁可把一个字符换成 `?`，也不要让整条命令崩在收尾的打印上 ——
    「结果对了但命令报错退出」是最容易误诊的一类失败。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # 65001 = UTF-8
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        # getattr 而不是直接访问：`TextIO` 在类型上不保证有 reconfigure
        # （pytest 捕获的流就没有），而这里必须是"能改就改，不能改就跳过"。
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 被 pytest 捕获的流可能不支持 reconfigure；不能用就此放弃整个命令。
            pass


def _print_human(result: TaskResult) -> None:
    """人类可读输出：先给结论，再给证据。"""
    print()
    print("=" * 72)
    print(result.summary_line())
    print("=" * 72)

    if result.plan_title:
        print(f"计划标题 : {result.plan_title}")
    if result.plan_goal:
        print(f"计划目标 : {result.plan_goal}")
    print(
        f"步骤     : 总 {result.steps.total} / 已结束 {result.steps.done} "
        f"/ 成功 {result.steps.succeeded} / 失败 {result.steps.failed}"
    )
    print(f"工具序列 : {' -> '.join(result.tool_sequence) if result.tool_sequence else '(无)'}")
    print(
        f"LLM 调用 : {result.llm_usage.llm_calls} 次（失败 {result.llm_usage.llm_errors} 次），"
        f"耗时 {result.llm_usage.llm_latency_ms} ms"
    )
    print(
        f"Token    : 输入 {result.llm_usage.prompt_tokens} + 输出 {result.llm_usage.completion_tokens} "
        f"= {result.llm_usage.total_tokens}"
    )
    print(f"成本     : ${result.cost_usd:.6f}")
    print(f"耗时     : {result.elapsed_ms} ms")
    _print_replay(result)

    # 加固层观察结果（Step 3）：observe 模式下这些全是"本会怎样"，不改变实际行为
    _print_guard(result)

    if result.attachments:
        print(f"交付文件 : {', '.join(result.attachments)}")
    if result.error:
        print(f"失败原因 : [{result.error_type}] {result.error}")

    if result.answer:
        print("-" * 72)
        print("最终答复：")
        print(result.answer)

    print("-" * 72)
    print(f"产物目录 : {result.workspace}")
    print("（可以直接进这个目录查看 Agent 实际写了哪些文件）")
    if result.trace_path:
        print(f"轨迹数据库: {result.trace_path}")
        print(f"查看轨迹 : python -m lab trace {result.task_id}")
    print()


def _print_replay(result: TaskResult) -> None:
    """打印 LLM 录制回放状态。

    为什么必须每次都打印：回放运行里的 `成本` / `耗时` 是**等价量**，
    不是本次真实消费。不把这件事印在屏幕上，那两个数字就会被读成
    "这次真的花了这么多、这么快"。
    """
    replay = result.replay or {}
    mode = replay.get("mode")
    if not mode or mode == "off":
        return
    hits, misses = replay.get("hits", 0), replay.get("misses", 0)
    writes = replay.get("writes", 0)
    offline = "✅ 全程离线" if (mode == "replay" and not misses) else ""
    print(f"LLM 缓存 [{mode}] : 命中 {hits} / 未命中 {misses} / 写入 {writes} {offline}")
    print(
        f"          等价成本 ${replay.get('equivalent_cost_usd', 0.0):.6f}"
        f"，**实际消费 ${replay.get('actual_spend_usd', 0.0):.6f}**"
        f"（缓存库 {replay.get('cache_path', '')}）"
    )
    if replay.get("conflicts"):
        print(f"          ⚠️ 发现 {replay['conflicts']} 个请求**同一请求不同响应**（服务端非确定性，温度 0 也消不掉）")


def _print_guard(result: TaskResult) -> None:
    """打印加固层观察结果。

    observe 模式下最有价值的两个量是 `would_stop` / `would_degrade`：
    它们让我们在**不改变 SUT 行为**的前提下，预演切换到 enforce/degrade 的后果。
    """
    guard = result.guard or {}
    budget = guard.get("budget")
    if budget:
        violated = budget.get("violated_metrics") or []
        flag = "⚠️" if violated else "✓"
        print(
            f"预算[{budget.get('mode')}]  : {flag} 越界维度 {violated or '无'} "
            f"(检查 {budget.get('checks')} 次；本会中止={budget.get('would_stop')})"
        )
        if budget.get("final"):
            final = budget["final"]
            print(
                f"          最终值 tokens={final.get('tokens')} cost=${final.get('cost_usd')} "
                f"elapsed={final.get('elapsed_s')}s steps={final.get('steps')} tools={final.get('tool_calls')}"
            )

    loop = guard.get("loop") or {}
    if loop:
        print(
            f"循环检测 : 最长连续重复 {loop.get('max_consecutive_repeat')} 次，"
            f"熔断候选 {loop.get('repeat_trips')} 次，动作多样性 {loop.get('action_diversity')}"
        )

    retries = guard.get("retries") or {}
    if retries.get("total"):
        print(f"重试     : 共 {retries['total']} 次，原因 {retries.get('by_reason')}")

    faults = guard.get("faults_observed") or {}
    if faults:
        print(f"故障注入 : {faults}")

    warnings = (guard.get("postconditions") or {}).get("warnings") or []
    if warnings:
        print(f"后置校验 : ⚠️ {len(warnings)} 条告警")
        for warning in warnings[:3]:
            print(f"           - {warning[:100]}")


def _cmd_run(args: argparse.Namespace) -> int:
    """执行 `lab run`。"""
    try:
        result = run_task_sync(
            args.goal,
            workspace=Path(args.workspace) if args.workspace else None,
            max_seconds=args.max_seconds,
            max_iterations=args.max_iterations,
            temperature=args.temperature,
            exec_timeout=args.exec_timeout,
            trace=not args.no_trace,
            fault_rules=fault_rules_from_spec(
                args.fault,
                tool=args.fault_tool,
                rate=args.fault_rate,
                latency_s=args.fault_latency,
                stable=args.fault_stable,
            ),
            replay=args.replay,
            replay_cache=Path(args.replay_cache) if args.replay_cache else None,
        )
    except LabConfigError as e:
        # 使用错误（例如没配 key）：打印可操作的提示，不要抛栈
        print(f"[lab] 配置错误：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[lab] 已被用户中断", file=sys.stderr)
        return 130

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        _print_human(result)

    # 退出码约定：0 成功 / 1 任务失败（CI 直接消费）
    return 0 if result.ok else 1


def _cmd_trace(args: argparse.Namespace) -> int:
    """执行 `lab trace`：渲染一次运行的轨迹树。

    为什么默认不传 task_id 也能用：A/B 对比时人会连续跑很多次，
    每次拷贝 uuid 太容易出错；默认取最近一次，需要指定时再传。
    """
    store = default_trace_store()
    task_id = args.task_id or store.latest_task_id()
    if not task_id:
        print('[lab] 还没有任何运行记录，先跑一次：python -m lab run "..."', file=sys.stderr)
        return 2

    # 支持短 id（唯一匹配才解析），拿不准就报错而不是猜
    if not store.load_task(task_id):
        resolved = store.resolve_task_id(task_id)
        if not resolved:
            print(f"[lab] 未找到任务 {task_id}（若是短 id，请确认唯一性）", file=sys.stderr)
            return 2
        task_id = resolved

    spans = store.load_spans(task_id)
    if not spans:
        print(f"[lab] 未找到任务 {task_id} 的轨迹", file=sys.stderr)
        return 2

    if args.json:
        task = store.load_task(task_id)
        print(
            json.dumps(
                {"task": task, "spans": [s.model_dump(mode="json") for s in spans]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(render_trace(store.load_task(task_id), spans, top_n=args.top))
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    """执行 `lab stats`：跨运行聚合指标（handoff 第 8 节指标模板的最小实现）。"""
    store = default_trace_store()
    stats = store.stats(sut_name=args.sut)

    if stats["runs"] == 0:
        print("[lab] 还没有任何运行记录", file=sys.stderr)
        return 2

    print()
    print("=" * 72)
    print(f"运行次数   : {stats['runs']}（成功 {stats['ok_runs']}，成功率 {stats['success_rate']:.1%}）")
    print(f"tokens/任务: 均值 {stats['avg_tokens']:.0f}（min {stats['min_tokens']} / max {stats['max_tokens']}）")
    print(f"成本/任务  : ${stats['avg_cost_usd']:.4f}")
    print(f"耗时/任务  : 均值 {stats['avg_elapsed_ms']:.0f}ms，P95 {stats['p95_elapsed_ms']}ms")
    print(f"工具调用/任务: {stats['avg_tool_calls']:.1f}")
    print(f"失败归因   : {stats['by_error_type']}")
    print("=" * 72)
    print("注意：当前只有 min/max/均值，尚未计置信区间。")
    print("      同一任务两次跑的 token 差异实测达到 34%（见 docs/step1-completion-report.md），")
    print("      所以 n<5 的数字不要拿来下结论。Step 4 会补置信区间。")
    print()
    return 0


def _cmd_replay_stats(args: argparse.Namespace) -> int:
    """`lab replay stats`：看缓存里有什么，以及**服务端非确定性**有多大。

    这个命令存在的意义不只是"看缓存大小"：
    `record` 模式每次都真的联网，所以同一个请求会被反复记录 ——
    只要两次响应不同，那就是"温度设成 0 也消不掉的那部分噪声"的实测值。
    它直接决定了"重跑一次"能带多少不可控变化，也决定了 A/B 的差异
    有多大成分只是采样噪声。
    """
    from lab.replay.cache import LLMCache
    from lab.replay.llm import resolve_cache_path

    path = resolve_cache_path(Path(args.cache) if args.cache else None)
    if not path.exists():
        print(f"缓存库不存在：{path}")
        print('（用 `python -m lab run "..." --replay record` 录一次就会出现）')
        return 2

    stats = LLMCache(path).stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    print()
    print("=" * 72)
    print(f"缓存库   : {stats['path']}")
    print(f"条目数   : {stats['entries']}（被回放命中总计 {stats['served_total']} 次）")
    print(f"录制区间 : {stats['first_recorded_at']} → {stats['last_recorded_at']}")
    print(f"等价成本 : ${stats['equivalent_cost_usd']:.4f}（录这些请求当时花了这么多）")
    print("=" * 72)
    if stats["entries"]:
        print(
            f"服务端非确定性：**{stats['conflict_keys']}/{stats['entries']}"
            f" = {stats['nondeterminism_rate']:.1%}** 的请求，同一个入参给过**不同回答**"
        )
        print(f"  （共 {stats['conflict_responses']} 条不同的替代响应）")
        if stats["conflict_keys"] == 0:
            print("  → 已观测范围内服务端是确定的。注意：这只说明**已记录过的**请求是确定的。")
        else:
            print(
                "  → 这部分差异**无法通过「固定温度」消除**。它决定了 A/B 对比的噪声地板：小于它的 delta 不能当结论。"
            )
    print()
    if stats["by_model"]:
        print("| 模型 | 条目 | 输入 token | 输出 token | 等价成本 | 价格表命中 |")
        print("|---|---|---|---|---|---|")
        for row in stats["by_model"]:
            print(
                f"| `{row['model']}` | {row['entries']} | {row['prompt_tokens']:,} "
                f"| {row['completion_tokens']:,} | ${row['equivalent_cost_usd']:.4f} "
                f"| {'✓' if row['priced'] else '✗（价格未知，成本记 0）'} |"
            )
    print()
    return 0


def _cmd_bench_validate(args: argparse.Namespace) -> int:
    """`lab bench validate`：任务集三步自检（不花钱）。"""
    import asyncio

    from lab.bench.task import load_all_tasks
    from lab.bench.validate import render_validations, validate_all

    tasks = load_all_tasks(group=args.group)
    if not tasks:
        print("[lab] 没有找到任务", file=sys.stderr)
        return 2

    results = asyncio.run(validate_all(tasks))
    print(render_validations(results))
    failed = [item for item in results if not item.ok]
    print(f"\n结论: {len(results) - len(failed)}/{len(results)} 任务通过自检")
    if failed:
        print("\n未通过的任务（先修任务集，再跑评测 —— 否则基线数字不可信）：")
        for item in failed:
            print(f"  - {item.uid}: {'; '.join(item.problems) or '自检步骤未按预期'}")
    return 1 if failed else 0


def _cmd_bench_tasks(args: argparse.Namespace) -> int:
    """`lab bench tasks`：列出任务集。"""
    from lab.bench.task import load_all_tasks, summarize_tasks

    tasks = load_all_tasks(group=args.group)
    print(f"共 {len(tasks)} 个任务")
    for task in tasks:
        print(f"  [{task.group}] {task.key:24} {task.title}")
        print(f"      目标: {task.goal.strip().splitlines()[0][:66]}...")
    summary = summarize_tasks(tasks)
    print(f"\n分组: {summary['by_group']}")
    print(f"标签: {summary['by_tag']}")
    return 0


def _confirm_cost(
    store: Any,
    tasks: List[Any],
    runs_per_task: int,
    arm_count: int,
    *,
    faulted: bool,
    replay: Optional[str],
    yes: bool,
    what: str = "模型调用",
) -> tuple:
    """打印规模与预估成本，必要时要求 `--yes`。返回 (是否继续, 预估成本)。

    抽成函数是因为它已经有两个调用点（`bench run` / `bench noise-floor`）。
    两处各写一份的下场是：某一处忘了把"交错多臂要乘臂数"算进去，
    而人多跑一倍的钱。
    """
    runs = runs_per_task * len(tasks) * arm_count
    groups = {task.group for task in tasks}
    historical = [
        cost for cost in (store.avg_cost_for_group(name, faulted=faulted) for name in groups) if cost is not None
    ]
    if historical:
        per_run = max(historical)  # 多分组时取最贵的，偏保守
        basis = f"库中同组历史均值（{'/'.join(sorted(groups))}，{'带故障注入' if faulted else '无故障'}）"
    else:
        per_run = 0.15 if not faulted else 0.35
        basis = f"保守默认值 ${per_run:.2f}/次（库里还没有此类历史；故障格实测约是基线的 2~3 倍）"

    scale = f" × {arm_count} 臂" if arm_count > 1 else ""
    print(f"即将运行 {len(tasks)} 个任务 × {runs_per_task} 次{scale} = **{runs} 次{what}**")
    if str(replay or "off") == "replay":
        # 严格回放不联网 → 真花钱为 0，也**不需要 --yes 确认**
        print("回放模式 **replay**（严格离线）：不消费 API，预估成本 $0.00")
        return True, 0.0

    estimate = runs * per_run
    print(f"预估成本 ≈ ${estimate:.2f}（依据：{basis}，单次 ≈ ${per_run:.3f}）")
    if estimate > 1.0 and not yes:
        print("\n[lab] 预估成本超过 $1.00，请确认后加 --yes 重跑。", file=sys.stderr)
        print("      先用小规模验证链路：--limit 2 --runs 1", file=sys.stderr)
        return False, estimate
    return True, estimate


def _build_arms(args: argparse.Namespace, fault_rules: Any) -> Optional[List[Any]]:
    """把 `--ab-guard` / `--ab-label` 转成臂列表（不传则返回 None = 单臂）。

    故障规则**在所有臂之间共享**：加固对照实验的自变量是"有没有加固"，
    故障是固定条件。如果每个臂各带一套故障，两组差异就分不清是谁造成的了。
    """
    from lab.bench.runner import ArmConfig, fault_spec_of
    from lab.guard.config import GuardConfig

    specs = list(args.ab_guard or [])
    if not specs:
        return None
    labels = list(args.ab_label or [])
    fault_spec = fault_spec_of(fault_rules)

    arms: List[Any] = []
    for index, spec in enumerate(specs):
        config = GuardConfig.from_spec(spec)
        if index < len(labels):
            label = labels[index]
        else:
            # 多臂时名字里带 guard，单臂时直接用 guard label（噪音最小）
            label = f"guard={config.label}" if len(specs) > 1 else config.label
        arms.append(
            ArmConfig(
                label=label,
                guard_spec=spec,
                guard_label=config.label,
                guard_config=config,
                fault_rules=fault_rules,
                fault_spec=fault_spec,
                replay=args.replay,
            )
        )
    return arms


def _purpose_counts(tasks) -> str:
    """把任务集按 purpose 汇总成一行（空集诊断用）。"""
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.purpose] = counts.get(task.purpose, 0) + 1
    return "，".join(f"{name}={n}" for name, n in sorted(counts.items()))


def _report_empty_tasks(args: argparse.Namespace, *, in_scope: list, generic: str) -> int:
    """任务集为空时的**可操作**诊断（返回退出码 2）。

    为什么值得抽成一个函数（两个子命令共用）：空集最常见的成因不是"任务集是空的"，
    而是 purpose 过滤把整组筛掉了（`--group ci` 的任务全是 capability，
    而 `--purpose regression` 一个都不剩）。只说"没有任务"会把人赶去查任务集，
    而问题其实在参数上 —— 诊断信息本身是产品（见 lab/README.md「我错在哪」第 5 条）。
    """
    purpose = getattr(args, "purpose", None)
    if purpose and purpose != "all" and in_scope:
        counts = _purpose_counts(in_scope)
        if counts:
            print(
                f"[lab] 没有任务可测：--group {args.group or '（全部）'} 下有 "
                f"{len(in_scope)} 个任务，但 --purpose={purpose} 一个都不剩（{counts}）。",
                file=sys.stderr,
            )
            print(
                "      改用 `--purpose all`（全部）或 `--purpose capability`（只测能力边界任务）。",
                file=sys.stderr,
            )
            return 2
    print(generic, file=sys.stderr)
    return 2


def _cmd_bench_run(args: argparse.Namespace) -> int:
    """`lab bench run`：跑评测并出报告。"""
    import asyncio

    from lab.api import default_trace_store, fault_rules_from_spec
    from lab.bench.report import render_report
    from lab.bench.runner import run_suite
    from lab.bench.task import load_all_tasks
    from lab.guard.budget import BudgetMode, BudgetPolicy
    from lab.guard.config import GuardConfig

    tasks = load_all_tasks(group=args.group)
    # purpose 口径（Step 9/T1）：与 noise-floor 对齐。
    # 默认是 all（= 不过滤），所以**不改变既有行为**；
    # 对照实验要排掉不稳定任务时用 `--purpose regression`。
    if args.purpose and args.purpose != "all":
        tasks = [t for t in tasks if t.purpose == args.purpose]
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t.key in wanted or t.uid in wanted]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        return _report_empty_tasks(args, in_scope=load_all_tasks(group=args.group), generic="[lab] 没有找到任务")

    store = default_trace_store()

    # 故障规则先建出来：臂要用它，成本估算也要用它
    fault_rules = fault_rules_from_spec(
        args.fault,
        tool=args.fault_tool,
        rate=args.fault_rate,
        latency_s=args.fault_latency,
        stable=args.fault_stable,
    )
    arms = _build_arms(args, fault_rules)
    arm_count = len(arms) if arms else 1

    # 花钱的事要先说清楚。
    # 早期版本用一个写死的系数（$0.045/次）估算，而那个系数来自冒烟测试的简单任务 ——
    # 实测 semireal 任务的平均成本是它的 3 倍（预估 $1.8 / 实际 $5.0）。
    # 花钱的事上估错 3 倍不可接受，所以改成：优先用库里同组任务的实测均值，
    # 没历史时用一个保守值（宁可高估也不要让用户被意外收费）。
    #
    # ⚠️ 交错多臂时运行数是 **× 臂数** 的：预估里不提这一项，
    # 人会以为 --runs 3 --ab-guard A --ab-guard B 只跑 3 次。
    proceed, _ = _confirm_cost(
        store,
        tasks,
        args.runs,
        arm_count,
        faulted=bool(args.fault),
        replay=args.replay,
        yes=args.yes,
    )
    if not proceed:
        return 2

    budget_policy = BudgetPolicy.from_env()
    if args.budget_mode:
        budget_policy = budget_policy.model_copy(update={"mode": BudgetMode(args.budget_mode)})

    # 加固开关：对照实验的前提（没有它 → 加固永远是开的 → 做不出"无加固"对照组）
    guard_config = GuardConfig.from_env()
    if args.guard is not None:
        guard_config = GuardConfig.from_spec(args.guard)

    if arms:
        print(f"\n交错 A/B：{len(arms)} 个臂，按轮次交替运行（{'、'.join(arm.describe() for arm in arms)}）")
        print("  自变量是加固配置，故障规则在所有臂之间**共享**。")
        if args.no_interleave:
            print("  ⚠️ --no-interleave：按臂分组顺序跑，**会重新引入时间混淆**。仅在确认交错调度本身有问题时才用。")
        if len({arm.slug() for arm in arms}) != len(arms):
            print(f"[lab] 臂名必须互不相同（工作区按它分目录）：{[a.label for a in arms]}", file=sys.stderr)
            return 2
    else:
        print(f"\n加固配置：{guard_config.describe()}")
        if guard_config.label != "all":
            print(
                "  ⚠️ 注意：loop_guard / budget 在 observe 模式下只观测不干预，"
                "真正影响成败的是 retry 与 postconditions。"
            )

    # 故障规则必须**打印出来**：否则日志里看不见注入了什么，
    # 事后只能靠猜（实测踩过：一个脚本忘了传 --label，日志里连区分信息都没有）。
    if fault_rules:
        print("故障注入：" + "; ".join(rule.describe() for rule in fault_rules))

    if args.replay:
        print(f"LLM 回放模式：{args.replay}" + ("（严格离线，不需要 API Key）" if args.replay == "replay" else ""))

    store = default_trace_store()

    # 阈值来源：默认优先用**历史分位数（per-task）**，没有历史才用环境变量/默认值。
    # 为什么默认这样：实测同一个全局阈值在 semireal 上的越界率高达 60%
    # （任务之间成本差 13 倍以上）；按任务分位数校准后越界率回到定义值 ~5%。
    policy_by_task = None
    if not args.budget_defaults:
        from lab.bench.policy import describe_policies, policies_from_history

        policy_by_task = policies_from_history(store, tasks)
        if policy_by_task:
            print(f"\n已按历史分位数推导 {len(policy_by_task)}/{len(tasks)} 个任务的预算阈值：")
            print(describe_policies(policy_by_task, tasks))
        else:
            print("\n[lab] 库里还没有足够的同类历史 → 本次用默认阈值（跑完一轮后就会开始用历史校准）")

    suite = asyncio.run(
        run_suite(
            runs_per_task=args.runs,
            group=args.group,
            limit=args.limit,
            keys=args.task,
            # ⚠️ 口径必须往下传：run_suite 会自己重新 load 任务集，
            # 不传的话上面按 purpose 筛过的 tasks 只影响成本预估，实际跑的仍是全集
            # （这个坑在 noise-floor 里踩过：`--purpose regression` 完全没生效）。
            purpose=(args.purpose if args.purpose and args.purpose != "all" else None),
            sut_kind=args.sut,
            label=args.label,
            bench_root=ensure_runs_dir() / "bench",
            concurrency=args.concurrency,
            temperature=args.temperature,
            budget_policy=budget_policy,
            budget_policy_by_task=policy_by_task,
            guard_config=guard_config,
            fault_rules=fault_rules,
            arms=arms,
            interleave=not args.no_interleave,
            replay=args.replay,
            replay_ignore_volatile=args.replay_ignore_volatile or None,
            replay_cache=args.replay_cache,
            # 增量落库：长评测（40 次运行 ≈ 半小时）被中断的途径很多
            # （Ctrl-C、机器休眠、终端关闭、远程会话断开），
            # 每次运行完成就落库 → 最坏情况只丢一次运行，而不是丢掉整份数据。
            on_start=store.save_suite_header,
            on_outcome=store.save_outcome,
            on_finish=store.finalize_suite,
        )
    )

    print()
    print(
        f"评测完成: {suite.successes}/{suite.total_runs} 成功"
        f"（{suite.success_rate.point:.1%}，95% 区间 [{suite.success_rate.low:.1%}, "
        f"{suite.success_rate.high:.1%}]），耗时 {suite.elapsed_s}s"
    )
    if suite.is_multi_arm:
        # 交错跑完必须**马上**把两臂分开看：混在一起的成功率没有任何意义
        print()
        print("分臂结果（交错跑出来的一个 suite 拆开看；不要当成两次独立评测）：")
        for arm_label in suite.arm_labels:
            sub = suite.for_arm(arm_label)
            rate = sub.success_rate
            print(
                f"  - {arm_label:28} {sub.successes}/{sub.total_runs} "
                f"= {rate.point:.1%} [{rate.low:.1%}, {rate.high:.1%}]"
            )
        print(
            f'\n  配对对比：python -m lab bench compare "{suite.suite_id[:8]}"'
            f' --arm "{suite.arm_labels[0]}" --arm "{suite.arm_labels[1]}"'
        )

    if args.report:
        report = render_report(suite)
        out_path = _write_report(suite.suite_id[:8], report)
        print(f"报告已写入: {out_path}")
    print(f"重新出报告: python -m lab bench report {suite.suite_id[:8]}")
    return 0


def _write_report(suite_id: str, report: str) -> Path:
    reports_dir = ensure_runs_dir() / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{suite_id}.md"
    path.write_text(report, encoding="utf-8")
    return path


def _cmd_bench_report(args: argparse.Namespace) -> int:
    """`lab bench report`：从历史评测重新生成报告（不重跑）。"""
    from lab.api import default_trace_store
    from lab.bench.report import render_report

    store = default_trace_store()
    suite_id = args.suite_id
    if not suite_id:
        suites = store.list_suites(limit=1)
        if not suites:
            print("[lab] 还没有任何评测记录", file=sys.stderr)
            return 2
        suite_id = suites[0]["suite_id"]
    else:
        # 统一用 _resolve_suite_id（支持短 id **与 label 片段**）。
        # 之前这里只支持 id 前缀，而 compare/gate 支持 label —— 同一个工具里
        # 两种解析规则会让人反复猜（实测踩过：`bench report "D 静默"` 直接报找不到）。
        resolved = _resolve_suite_id(store, suite_id)
        if resolved is None:
            print(f"[lab] 无法唯一确定 suite: {suite_id}", file=sys.stderr)
            return 2
        suite_id = resolved

    suite = store.load_suite(suite_id)
    if suite is None:
        print(f"[lab] 未找到评测: {suite_id}", file=sys.stderr)
        return 2

    report = render_report(suite)
    if args.out:
        path = Path(args.out)
        path.write_text(report, encoding="utf-8")
        print(f"报告已写入: {path}")
    else:
        print(report)
    return 0


def _cmd_bench_recover(args: argparse.Namespace) -> int:
    """`lab bench recover`：从工作区 + 轨迹库重建一次评测（崩溃后救数据）。"""
    import asyncio

    from lab.api import default_trace_store
    from lab.bench.recover import recover_suite
    from lab.bench.report import render_report

    store = default_trace_store()
    suite = asyncio.run(
        recover_suite(
            bench_root=ensure_runs_dir() / "bench",
            store=store,
            suite_id=args.suite_id,
            label=args.label,
            runs_per_task=args.runs,
            note=args.note or "",
            group=args.group,
        )
    )

    print(
        f"已重建评测: {suite.total_runs} 次运行，"
        f"{suite.successes} 成功（{suite.success_rate.point:.1%}，"
        f"95% 区间 [{suite.success_rate.low:.1%}, {suite.success_rate.high:.1%}]）"
    )
    if args.report:
        out_path = _write_report(suite.suite_id[:8], render_report(suite))
        print(f"报告已写入: {out_path}")
    return 0


# ==================== 读轨迹入口（T2 / 审计 §4.8） ====================
#
# 为什么需要它们：项目一直强调"读 transcript 才能发现机制"，但原来只有 `lab trace`
# （只渲染**最近一次**运行），既不能"按 suite/任务/臂定位某一次运行"，
# 也不能"从库里挑几条失败来读"。而失败归因靠的就是把轨迹摊开看。


def _render_artifacts(
    workspace: str,
    *,
    run_time: Optional[str] = None,
    stored_digest: Optional[str] = None,
    max_files: int = 4,
    max_chars: int = 300,
) -> List[str]:
    """列出该次运行工作区里的**关键产物**（读轨迹时用来核对 Agent 到底交了什么）。

    只看 `home/ubuntu` 下、排除 `input/`：那是 fixtures（题面输入），不是产物。

    给了 `run_time` 就先做**新鲜度检查**：工作区可能已被后来的运行覆盖
    （见 `lab/bench/workspace.py`），此时读到的产物属于**另一次运行**。
    必须喊出来 —— 否则“判定通过 + 产物是别人的”会被当成正常结果一路读下去。
    """
    head: List[str] = []
    if run_time:
        head.extend(render_freshness(check_freshness(workspace, run_time)))
    if stored_digest is not None:
        # 内容摘要比 mtime 可靠：mtime 会被复制/恢复/时钟调整骗过，摘要不会。
        # 历史行没存过摘要（空串）→ 只能报“无法校验”，不能当成“一致”。
        head.extend(render_digest_check(compare_digest(workspace, stored_digest)))
    if not workspace:
        return head + ["    （未记录工作区路径 —— 旧数据可能没有）"]
    root = Path(workspace) / "home" / "ubuntu"
    if not root.is_dir():
        return head + [f"    （工作区不存在：{root}）"]
    files = sorted(
        p
        for p in root.rglob("*")
        # `__pycache__` 是**运行残留**，不是 Agent 的交付物：
        # 实测它的 `.pyc` 会占满 `max_files` 名额，把真正要看的产物挤出去。
        if p.is_file() and "input" not in p.parts and "__pycache__" not in p.parts
    )
    if not files:
        return head + ["    （工作区里没有产物）"]

    lines: List[str] = list(head)
    for path in files[:max_files]:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        lines.append(f"    /home/ubuntu/{path.relative_to(root).as_posix()}  ({size}B)")
        if size <= max_chars * 4:  # 只对够小的文件展示内容（大文件看几行没意义）
            try:
                data = path.read_bytes()[: max_chars * 4]
            except OSError:
                continue
            if b"\x00" in data:
                # 二进制产物（.pyc / 图片 / zip …）当文本打出来只有一屏乱码，
                # 比不打印更糟：它会把真正要看的几行顶出去。只看大小就够。
                lines.append("      （二进制内容 —— 只看大小，不打印）")
                continue
            text = data.decode("utf-8", errors="replace")[:max_chars]
            for line in text.replace("\r\n", "\n").splitlines()[:6]:
                lines.append(f"      {line}")
    if len(files) > max_files:
        lines.append(f"    …（另有 {len(files) - max_files} 个文件）")
    return lines


def _render_tool_sequence(raw: Optional[str], *, top: int = 12) -> str:
    """把 `tasks.tool_sequence`（JSON 列表）渲染成一行箭头链。

    这是"工具调用次数从事件流算"那一列的原始形态 —— 看它比看计数有用得多：
    实测靠它才看出"20 步里 10 步在追行尾"这类模式。
    """
    if not raw:
        return "（未记录）"
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)[:200]
    if not isinstance(items, list) or not items:
        return str(raw)[:200]
    head = " → ".join(str(item) for item in items[:top])
    if len(items) > top:
        head += f" → …（共 {len(items)} 次）"
    return head


def _run_digest_lines(
    *,
    label: str,
    ok: bool,
    suite_id: str,
    metrics: str,
    error_type: Optional[str] = None,
    error: Optional[str] = None,
    failed_checks: Optional[List[str]] = None,
    process_flags: Optional[List[str]] = None,
    tool_sequence: Optional[str] = None,
    self_reported_ok: Optional[bool] = None,
    workspace: str = "",
    run_time: Optional[str] = None,
    stored_digest: Optional[str] = None,
    artifacts: int = 4,
    top: int = 6,
) -> List[str]:
    """一次运行的摘要块（`bench show` 与 `bench sample-failures` 共用）。"""
    lines = ["─" * 78, f"[{'OK  ' if ok else 'FAIL'}] {label}   suite={suite_id[:8]}"]
    if self_reported_ok is not None:
        lines.append(
            f"  自述成功={self_reported_ok}"
            + ("（⚠️ 与判定器不一致：只看自述会被骗）" if bool(self_reported_ok) != ok else "")
        )
    lines.append(f"  {metrics}")
    if error_type or error:
        lines.append(f"  错误：error_type={error_type or '—'}  {str(error or '')[:160]}")
    if failed_checks:
        lines.append(f"  失败检查（{len(failed_checks)} 条）：")
        lines.extend(f"    {item}" for item in failed_checks[:top])
    if process_flags:
        lines.append(f"  过程问题：{', '.join(str(flag) for flag in process_flags[:top])}")
    lines.append(f"  工具序列：{_render_tool_sequence(tool_sequence, top=top * 2)}")
    lines.append("  关键产物：")
    lines.extend(
        _render_artifacts(workspace, run_time=run_time, stored_digest=stored_digest, max_files=artifacts)
    )
    return lines


def _safe_json_list(raw: Any) -> List[str]:
    """把库里的 JSON 列安全解成字符串列表（损坏/旧格式都不该让 CLI 崩）。

    为什么需要它：这些列是**历史数据**（旧版本写过什么格式不由现在决定），
    而一条坏行不该让"读轨迹"这条命令整个挂掉 —— 那恰恰是最需要它的时候。
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)[:200]]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)[:200]]


def _cmd_bench_show(args: argparse.Namespace) -> int:
    """`lab bench show`：把一个**具体运行**摊开读。

    为什么不直接 `lab trace`：那只给你"最近一次"，而读轨迹的需求总是
    "**那一次**失败的运行到底怎么了"（哪个 suite / 哪个任务 / 哪个臂 / 第几次）。
    """
    from lab.api import default_trace_store
    from lab.trace.view import render_trace

    store = default_trace_store()
    suite_id = _resolve_suite_id(store, args.suite)
    if suite_id is None:
        print(f"[lab] 无法唯一确定 suite：{args.suite}", file=sys.stderr)
        return 2
    suite = store.load_suite(suite_id)
    if suite is None:
        print("[lab] 加载 suite 失败", file=sys.stderr)
        return 2

    wanted = {args.task}
    matches = [o for o in suite.outcomes if o.task_key in wanted or o.task_uid in wanted]
    if args.run is not None:
        matches = [o for o in matches if o.run_index == args.run]
    if args.arm:
        matches = [o for o in matches if o.arm == args.arm]
    if not matches:
        where = f"task={args.task}"
        if args.run is not None:
            where += f" run={args.run}"
        if args.arm:
            where += f" arm={args.arm}"
        print(f"[lab] 该 suite 里没有匹配的运行（{where}）", file=sys.stderr)
        print(f"      可用任务：{'、'.join(sorted({o.task_key for o in suite.outcomes}))}", file=sys.stderr)
        return 2

    for outcome in matches[: args.limit]:
        label = f"{outcome.task_key} run{outcome.run_index}" + (f" arm={outcome.arm}" if outcome.arm else "")
        metrics = (
            f"tools={outcome.tool_calls} tokens={outcome.tokens} "
            f"cost=${outcome.cost_usd:.4f} 耗时={outcome.elapsed_ms / 1000:.1f}s "
            f"guard={outcome.guard_config} faults={outcome.faults_injected}"
            + (f"（{outcome.fault_spec}）" if outcome.fault_spec else "")
            + (f" 回放未命中={outcome.replay_misses}" if outcome.replay_misses else "")
        )
        task = store.load_task(outcome.task_id) if outcome.task_id else None
        for line in _run_digest_lines(
            label=label,
            ok=outcome.ok,
            suite_id=outcome.suite_id,
            metrics=metrics,
            error_type=outcome.error_type,
            error=outcome.error,
            failed_checks=outcome.failed_checks,
            process_flags=outcome.process_flags,
            tool_sequence=(task or {}).get("tool_sequence"),
            self_reported_ok=outcome.self_reported_ok,
            # ⚠️ workspace 要从 `tasks` 表取：`bench_runs` 里**没有**这一列，
            # 所以 `load_suite` 重建出来的 outcome.workspace 是空串 ——
            # 直接用它会让"关键产物"永远显示"未记录工作区路径"。
            workspace=str((task or {}).get("workspace") or outcome.workspace),
            # 运行时间用于新鲜度检查（`started_at` 实际是**结果落库**的时刻，在 SUT 跑完之后）
            run_time=str((task or {}).get("started_at") or ""),
            stored_digest=str((task or {}).get("artifact_digest") or ""),
            artifacts=args.artifacts,
            top=args.top,
        ):
            print(line)
        if not args.no_trace and outcome.task_id:
            print()
            print(render_trace(task, store.load_spans(outcome.task_id), top_n=args.top))
        print()
    return 0


def _cmd_bench_sample_failures(args: argparse.Namespace) -> int:
    """`lab bench sample-failures`：从库里挑几条**失败**运行来读。

    默认只给摘要 + 指向 `bench show` 的命令：一次打印 5 份完整轨迹没人读得完，
    而"先看摘要、再挑一条深挖"才是真实工作流。`--full` 可直接打印轨迹树。
    """
    from lab.api import default_trace_store
    from lab.trace.view import render_trace

    store = default_trace_store()
    suite_id = None
    if args.suite:
        suite_id = _resolve_suite_id(store, args.suite)
        if suite_id is None:
            print(f"[lab] 无法唯一确定 suite：{args.suite}", file=sys.stderr)
            return 2

    rows = store.list_failed_runs(limit=args.n, suite_id=suite_id)
    if not rows:
        print("（库里没有失败运行。）")
        print(
            "  这本身是个结论：要么任务集饱和了，要么判定器口径漏了 ——"
            "两种都值得用 `bench report` 复核，而不是当成好消息。"
        )
        return 0

    print(f"最近 {len(rows)} 条失败运行（库口径；详情：`python -m lab bench show <suite> <task> --run N [--arm A]`）")
    for row in rows:
        failed = _safe_json_list(row.get("failed_checks"))
        flags = _safe_json_list(row.get("process_flags"))
        metrics = (
            f"tools={row.get('tool_calls') or 0} cost=${row.get('cost_usd') or 0:.4f} "
            f"arm={row.get('arm') or '—'} 时间={row.get('created_at')}"
        )
        for line in _run_digest_lines(
            label=f"{row.get('task_key')} run{row.get('run_index')}",
            ok=False,
            suite_id=str(row.get("suite_id") or ""),
            metrics=metrics,
            error_type=row.get("error_type"),
            error=row.get("error"),
            failed_checks=failed,
            process_flags=flags,
            tool_sequence=row.get("tool_sequence"),
            self_reported_ok=bool(row.get("self_reported_ok")) if row.get("self_reported_ok") is not None else None,
            workspace=str(row.get("workspace") or ""),
            run_time=str(row.get("created_at") or ""),
            stored_digest=str(row.get("artifact_digest") or ""),
            artifacts=args.artifacts,
            top=args.top,
        ):
            print(line)
        task_id = row.get("task_id")
        if args.full and task_id:
            print(render_trace(store.load_task(task_id), store.load_spans(task_id), top_n=args.top))
        print(
            f"  → 看完整轨迹：python -m lab bench show {str(row.get('suite_id'))[:8]} "
            f"{row.get('task_key')} --run {row.get('run_index')}"
            + (f" --arm {row.get('arm')}" if row.get("arm") else "")
        )
        print()
    return 0


def _purpose_verdict(purpose: str, clean_n: int, clean_ok: int) -> str:
    """标签 vs 实测的判定（T5）。

    规则刻意保守 —— 它只提示"值得看一眼"，不下判决：
    - `regression` 任务出现**干净失败** → ⚠️ 建议改标 capability
      （否则回归门禁会因为与代码无关的波动变红，而"频繁变红的门禁等于没有门禁"）；
    - `capability` 任务在**足够样本**上全对 → 💤 可以考虑改回 regression（先看样本量）；
    - 其余 → 一致。
    """
    clean_fail = clean_n - clean_ok
    if purpose == "regression" and clean_fail > 0:
        return "⚠️ 建议改标 capability（干净运行里有失败）"
    if purpose == "capability" and clean_n >= 5 and clean_fail == 0:
        return "💤 样本足够且全对，可考虑改回 regression"
    return "一致"


def _cmd_bench_purpose_audit(args: argparse.Namespace) -> int:
    """`lab bench purpose-audit`：标签 vs 实测的对账（$0，只读库）。

    为什么需要它：`purpose` 是某一时刻数据的快照，而任务集会变（新增 ci 组就是例子）。
    没有对账时，一个 `regression` 任务后来变得不稳定**没人会收到通知** ——
    而它会安静地把回归门禁变成"因噪声变红"。
    """
    from lab.api import default_trace_store
    from lab.bench.task import load_all_tasks

    store = default_trace_store()
    purposes = {task.key: task.purpose for task in load_all_tasks()}
    audit = {row["task_key"]: row for row in store.task_success_audit()}

    lines = [
        (
            "标签 vs 实测对账（口径：只算**干净运行** = `faults_injected=0`；"
            "故障格会人为压低成功率，不能算进「稳不稳定」）"
        ),
        "",
        "| 任务 | 标签 | 干净 n-ok | 故障 n | 判定 |",
        "|---|---|---|---|---|",
    ]
    mismatches = []
    for key in sorted(set(purposes) | set(audit)):
        purpose = purposes.get(key, "（不在任务集里）")
        row = audit.get(key)
        if row is None:
            lines.append(f"| `{key}` | {purpose} | — | — | （库里没有运行） |")
            continue
        clean_n = row["clean_n"] or 0
        clean_ok = row["clean_ok"] or 0
        verdict = _purpose_verdict(purpose, clean_n, clean_ok)
        lines.append(f"| `{key}` | {purpose} | {clean_ok}/{clean_n} | {row['fault_n'] or 0} | {verdict} |")
        if verdict != "一致":
            mismatches.append((key, verdict, row))

    print("\n".join(lines))
    print()
    if not mismatches:
        print("✅ 所有任务的标签都与实测一致。")
        return 0

    print(f"⚠️  {len(mismatches)} 个任务的标签与实测不一致：")
    print("")
    # 把这条**局限**写在输出里，而不是藏在注释里：
    # 实测（2026-09-20）5 个被标任务的干净失败全部是**已解释并已修**的历史失败
    # （期望值写错 / sut_crash / 产物逃逸），把它们读成“标签错了”就会误改标。
    print("  ⚠️ 这个判定基于**全部历史**：装置修复前（2026-09-18 之前）的失败也会计入，")
    print("     所以它只说“值得看一眼”，不说“标签错了”。判断依据是那条失败运行的")
    print("     **时间**与**失败原因**：若是已修的任务集/装置缺陷（期望值写错、sut_crash、")
    print("     产物逃逸），标签就是对的。")
    print("")
    for key, verdict, row in mismatches:
        hint = ""
        if row.get("last_clean_fail_suite"):
            short = str(row["last_clean_fail_suite"])[:8]
            run_index = row.get("last_clean_fail_run")
            command = f"python -m lab bench show {short} {key}"
            if run_index is not None:
                command += f" --run {run_index}"
            hint = f"　最近一次干净失败：`{command}`"
        print(f"  - `{key}`：{verdict}{hint}")
    return 0


def _stale_flag(workspace: str, run_time: Optional[str]) -> str:
    """新鲜度的**三态**标记：`"1"` 被覆盖 / `"0"` 新鲜 / `""` 无法判断。

    为什么留空而不是写 `"0"`：`""` 与 `"0"` 是两件事 —— 前者是"没验证过"，
    后者是"验过且没问题"。把它们混成一个值，就等于把未验证写成已验证
    （本项目自纠 #1/#8 都是这类错误的变体）。
    """
    freshness = check_freshness(workspace, run_time)
    if freshness.state == UNKNOWN:
        return ""
    return "1" if freshness.stale else "0"


# 已修缺陷的两个任务（见 `docs/task-ambiguity-lint.md` §8）：它们的**历史运行是在旧 goal 下
# 产生的**，对本任务已作废。分级抽样默认排除它们，否则金标里会混进"回答旧问题"的样本。
DEFAULT_EXCLUDED_TASKS = ("sem_markdown_toc", "syn_date_diff")


def _sample_id_of(row: dict) -> str:
    return (
        f"{str(row.get('suite_id'))[:8]}:{row.get('task_key')}:"
        f"{row.get('run_index')}:{row.get('arm') or '-'}"
    )


def _print_level_distribution(samples: List[Any]) -> None:
    """把级别分布与"负样本覆盖了几个任务"打出来 —— 抽样设计的可读性就是它的一半价值。"""
    from collections import Counter

    from lab.bench.sampling import LEVEL_POSITIVE

    by_level = Counter(sample.level for sample in samples)
    negative_tasks = Counter(sample.task_key for sample in samples if sample.level != LEVEL_POSITIVE)
    print("  级别分布：" + "、".join(f"{k}={v}" for k, v in sorted(by_level.items())))
    preview = "、".join(f"{k}({v})" for k, v in sorted(negative_tasks.items())[:8])
    print(f"  负样本覆盖 {len(negative_tasks)} 个任务：{preview}" + ("…" if len(negative_tasks) > 8 else ""))


async def _materialize_synthetic(samples: List[Any], by_key: dict, positives: dict) -> None:
    """把构造样本真正写进磁盘（mild 是"复制 + 扰动"，其余走 validate 的物化器）。"""
    from lab.bench.sampling import LEVEL_MILD, derive_mild, materialize

    for sample in samples:
        task = by_key.get(sample.task_key)
        if task is None:
            continue
        if sample.level == LEVEL_MILD:
            source = positives.get(sample.task_key)
            if source:
                # **定向降级**：按 verify 的期望值构造违反，并回跑判定器确认它**真的失败**。
                # 返回 False 时不会落盘 —— 宁可少一条，也不给“轻微错”挂空名。
                await derive_mild(task, source, sample.artifact_dir)
            continue
        await materialize(sample, task, level=sample.level)


def _balanced_sheet_rows(args: argparse.Namespace, store: Any) -> List[dict]:
    """分级抽样（P3a）：剔 stale + 排缺陷任务 + **跨任务的分级负样本**。

    为什么要造负样本而不只靠库里现成的失败：逐条核对过库里的 23 条失败样本 ——
    15 条人工认为"做完了"（10 条是任务期望值写错的假失败、3 条工作区被覆盖、2 条归因分歧）。
    也就是说"多抽失败"抽到的主要是**测量缺陷**，不是坏产物。
    """
    import asyncio

    from lab.bench.sampling import (
        LEVEL_POSITIVE,
        SYNTHETIC_LEVELS,
        Candidate,
        plan_synthetic,
        select_from_runs,
    )
    from lab.bench.task import load_all_tasks
    from lab.bench.workspace import check_freshness

    exclude = list(args.exclude_task) if args.exclude_task is not None else list(DEFAULT_EXCLUDED_TASKS)
    # 多取候选：stale 与被排除的任务会在下面被剔掉
    runs = store.sample_runs_for_judging(limit=max(args.n * 4, 200))
    candidates = []
    for row in runs:
        workspace = row.get("workspace") or ""
        freshness = check_freshness(workspace, row.get("created_at"))
        candidates.append(
            Candidate(
                sample_id=_sample_id_of(row),
                task_key=row.get("task_key") or "",
                group=row.get("group_name") or "",
                ok=bool(row.get("ok")),
                stale=freshness.stale,
                workspace=workspace,
            )
        )
    stale_dropped = sum(1 for candidate in candidates if candidate.stale)
    samples = select_from_runs(candidates, exclude_tasks=exclude, per_task=args.per_task)

    tasks = {task.key: task for task in load_all_tasks()}
    levels = list(args.level) if args.level else list(SYNTHETIC_LEVELS)
    synthetic = plan_synthetic(
        tasks, exclude_tasks=exclude, root=str(ensure_runs_dir() / "judge_samples"), levels=levels
    )
    positives = {s.task_key: s.artifact_dir for s in samples if s.level == LEVEL_POSITIVE}
    asyncio.run(_materialize_synthetic(synthetic, tasks, positives))

    # 物化可能失败（典型：某任务没有可派生的**正样本** → `mild` 造不出来）。
    # **宁可少一条，也不要在表里留一条指向不存在目录的样本** —— 那种“幽灵样本”
    # 会让 judge 去评空材料、让人去标看不见的东西，把噪声直接混进 κ。
    kept = [s for s in synthetic if Path(s.artifact_dir).is_dir()]
    dropped = [s for s in synthetic if not Path(s.artifact_dir).is_dir()]
    if dropped:
        tasks_dropped = sorted({s.task_key for s in dropped})
        print(
            f"  ⚠️ 丢弃 {len(dropped)} 条**物化失败**的构造样本（目录不存在）："
            f"{tasks_dropped[:6]}{'…' if len(tasks_dropped) > 6 else ''}"
        )
        print("     （`mild` 是从正样本产物派生的；库里没有该任务的正样本时就造不出来）")
    synthetic = kept

    print(
        f"分级抽样：候选 {len(candidates)} 条 → 剔掉 stale {stale_dropped} 条"
        f"、排除任务 {sorted(exclude)} → 真实运行 {len(samples)} 条 + 构造样本 {len(synthetic)} 条"
    )
    samples.extend(synthetic)
    _print_level_distribution(samples)

    return [
        {
            "sample_id": sample.sample_id,
            "task_key": sample.task_key,
            "group": sample.group,
            "suite_id": "" if sample.sample_id.startswith("synthetic:") else sample.sample_id.split(":")[0],
            "run_index": "",
            "arm": "",
            "self_reported_ok": "",
            "artifact_dir": sample.artifact_dir,
            "workspace_stale": "0",  # 已经过滤过了：剩下的都是新鲜的
            "level": sample.level,
            "notes": sample.note,
        }
        for sample in samples
    ]


def _cmd_bench_judge_sheet(args: argparse.Namespace) -> int:
    """`lab bench judge-sheet`：生成**待人工标注**的 CSV（T14，$0）。

    为什么要有这条命令（而不是一次性脚本）：标注表是可复现的产物 ——
    样本怎么抽的必须能被重跑，否则"κ 是多少"就不可审计。

    两种口径：
    - 默认（旧）：**失败优先，再补成功** —— 保留它是为了能复现历史金标；
    - `--balanced`（P3a）：剔 stale 工作区 + 按任务配额 + 造**跨任务分级负样本**。
      为什么必须换：旧口径产出的 100 条里 89 条三维度全 true，所有反例都来自同一个任务。
    """
    from lab.api import default_trace_store
    from lab.bench.calibrate import write_label_sheet

    store = default_trace_store()
    if args.balanced:
        rows = _balanced_sheet_rows(args, store)
    else:
        runs = store.sample_runs_for_judging(limit=args.n)
        rows = [
            {
                "sample_id": _sample_id_of(row),
                "task_key": row.get("task_key") or "",
                "group": row.get("group_name") or "",
                "suite_id": str(row.get("suite_id") or "")[:8],
                "run_index": row.get("run_index"),
                "arm": row.get("arm") or "",
                "self_reported_ok": row.get("self_reported_ok"),
                "artifact_dir": row.get("workspace") or "",
                # 抽样时就把新鲜度算出来：标注/跑 judge 之前就该知道"这条产物是不是那次的"
                "workspace_stale": _stale_flag(row.get("workspace") or "", row.get("created_at")),
                "level": "positive" if row.get("ok") else "db_negative",
            }
            for row in runs
        ]

    path = Path(args.out) if args.out else ensure_runs_dir() / "judge_gold.csv"
    count = write_label_sheet(rows, path)
    print(f"已写入 {count} 条待标注样本：{path}")
    print("  填这三列（true/false；留空 = 未标注，不会被当成 false）：")
    print("    human_task_completion / human_instruction_following / human_output_quality")
    print(f"  填完跑：python -m lab bench calibrate --gold {path}")
    return 0


def _cmd_bench_calibrate(args: argparse.Namespace) -> int:
    """`lab bench calibrate`：人工金标 → Cohen's κ（**低于阈值就拒绝启用**，T14，$0）。"""
    from lab.bench.calibrate import calibrate, read_gold_csv, render_calibration

    gold = read_gold_csv(Path(args.gold))
    judged: dict = {}
    if args.judged:
        try:
            judged = json.loads(Path(args.judged).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 判定文件坏了/不存在 → 说清是哪一步，而不是抛裸异常（诊断信息本身是产品）
            print(f"[lab] 读 judge 判定失败 {args.judged}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(judged, dict):
            print(
                "[lab] judge 判定必须是 {sample_id: {dimension: true/false/null}} 形式的对象",
                file=sys.stderr,
            )
            return 2

    calibration = calibrate(gold, judged)
    print(render_calibration(calibration))
    print()
    if calibration.enabled:
        print("✅ 达标：judge 可以作为**辅助**信号使用（仍然不进确定性基线）。")
    else:
        print("❌ 未达标 → **拒绝启用**。原因：" + calibration.refusal_reason())
        print('   （没有人工金标时，不得写成"已做 κ 校准"。）')
    return 0


def _cmd_bench_judge(args: argparse.Namespace) -> int:
    """`lab bench judge`：对标注表里的样本跑 LLM-judge，产出判定 JSON（T14 收尾，💰）。

    为什么要有它：`calibrate` 要算 κ 就必须有"judge 的判定"，而在它之前**没有任何入口**
    能生成这份判定 —— 于是 κ 永远算不出来（只能手写一次性脚本，不可审计）。
    这条命令把链路闭合：`judge-sheet`（抽样本）→ **`judge`（跑判定）** → `calibrate`（算 κ）。

    为什么默认不跑：每条样本要按 3 个维度**各调一次**模型（98 条 = 294 次调用）。
    按项目纪律，付费动作必须先打印预估，`--yes` 才真跑。
    """
    import asyncio

    from lab.api import default_trace_store
    from lab.bench.calibrate import read_gold_rows
    from lab.bench.judge import (
        ARTIFACT_MAX_TOTAL_CHARS,
        JUDGE_DIMENSIONS,
        JUDGE_RUBRIC_VERSION,
        collect_artifacts,
        judge_entries,
    )
    from lab.bench.task import load_all_tasks
    from lab.config import load_llm_config

    gold_path = Path(args.gold) if args.gold else ensure_runs_dir() / "judge_gold.csv"
    if not gold_path.exists():
        print(f"[lab] 找不到标注表 {gold_path}", file=sys.stderr)
        print("  先跑：python -m lab bench judge-sheet", file=sys.stderr)
        return 2
    rows = read_gold_rows(gold_path)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print(f"[lab] {gold_path} 里没有可用的样本", file=sys.stderr)
        return 2

    # 目标（goal）从任务集读，**不给期望值**：judge 的判断依据只有"目标 + 产物"。
    # 给了期望值，κ 量的就不再是"judge 会不会判"，而是"judge 会不会抄"。
    goals = {task.key: task.goal for task in load_all_tasks()}
    store = default_trace_store()
    entries: List[dict] = []
    unknown_tasks: List[str] = []
    stale: List[str] = []
    for row in rows:
        key = (row.get("task_key") or "").strip()
        goal = goals.get(key)
        if goal is None:
            unknown_tasks.append(key)
            continue
        sample_id = row["sample_id"].strip()
        workspace = row.get("artifact_dir") or ""
        # 新鲜度：产物可能已被后来的运行覆盖（见 `lab/bench/workspace.py`）。
        # 新表在抽样时就算好了（`workspace_stale`）；旧表没那一列 → 用库里的运行时间现算。
        flag = (row.get("workspace_stale") or "").strip()
        if not flag and workspace:
            # 基准必须按**运行身份**查，不能按 workspace 查 —— 后者正是被覆盖的那个东西，
            # 反查会拿到“后来那次”的时间，于是 stale 永远检不出来（实测 1 条 vs 39 条）。
            arm = (row.get("arm") or "").strip()
            try:
                run_index = int(row.get("run_index") or -1)
            except (TypeError, ValueError):
                run_index = -1
            run_time = store.run_created_at(
                (row.get("suite_id") or "").strip(),
                key,
                run_index,
                "" if arm == "-" else arm,
            )
            flag = _stale_flag(workspace, run_time)
        if flag == "1":
            stale.append(sample_id)
        entries.append(
            {
                "sample_id": sample_id,
                "goal": goal,
                "artifacts": collect_artifacts(workspace),
            }
        )
    if unknown_tasks:
        print(f"[lab] 这些任务的 goal 找不到（任务集改过？）：{sorted(set(unknown_tasks))}", file=sys.stderr)
    if stale:
        # 必须**显式喊出来**：这些样本的产物不是它要评的那一次，
        # 而 judge 会把它们当正常样本评下去（“判定通过 + 产物是别人的”可以同时成立）。
        print(f"⚠️  {len(stale)}/{len(entries)} 条样本的工作区已被后来的运行覆盖 —— 产物不是那次运行的：")
        for sample_id in stale[:5]:
            print(f"      {sample_id}")
        if len(stale) > 5:
            print(f"      …（另有 {len(stale) - 5} 条）")
        print("    judge 看到的产物因此不是它该评的那一次 —— 这些样本**不该**进 κ 的对照。")
    if not entries:
        return 2

    llm_config = load_llm_config(temperature=args.temperature)
    calls = len(entries) * len(JUDGE_DIMENSIONS)
    out_path = Path(args.out) if args.out else ensure_runs_dir() / "judge_judged.json"

    print(f"judge 预估（rubric {JUDGE_RUBRIC_VERSION}）：")
    print(f"  样本 {len(entries)} 条 × 维度 {len(JUDGE_DIMENSIONS)} 个 = **{calls} 次 LLM 调用**")
    print(
        f"  材料总量 {sum(len(e['artifacts']) for e in entries)} 字符"
        f"（每条上限 {ARTIFACT_MAX_TOTAL_CHARS}；进 prompt 前 judge 还会再截到 6000）"
    )
    print(f"  模型 {llm_config.model_name}，温度 {llm_config.temperature}")
    print(f"  输出 {out_path}")
    if not args.yes:
        print("\n这是**付费**动作（每条 3 次调用）。建议先冒烟 3 条：")
        print(f"  python -m lab bench judge --limit 3 --yes --out {out_path}")
        print("确认整批再跑：加 `--yes`。")
        return 2

    from app.infrastructure.external.llm.openai_llm import OpenAILLM
    from lab.infra.counting_llm import CountingLLM
    from lab.usage import Usage, price_of

    # 用与评测**同一套**计量代理：judge 的成本必须可报 —— 否则"跑完记录实际花费"
    # 这条纪律在这里就是空话，而这条命令恰恰是唯一要跑几百次调用的地方。
    usage = Usage()
    llm = CountingLLM(OpenAILLM(llm_config), usage)
    print()

    def _progress(done: int, total: int) -> None:
        # 逐条报进度：294 次调用要几分钟，没有进度会让人以为卡死
        print(f"  [{done}/{total}] 已完成", flush=True)

    # 增量落盘：每完成一条就写一次。294 次调用要跑十几分钟，
    # 而中断的途径很多（Ctrl-C / 休眠 / 断网）—— 只在最后写一次的话，
    # 第 293 条断掉就是"整批白跑"（与评测的增量落库同一个理由）。
    out_path.parent.mkdir(parents=True, exist_ok=True)
    collected: Dict[str, Dict[str, Optional[bool]]] = {}

    def _write() -> None:
        payload = {
            # `__meta__` 不会被 `calibrate` 当成样本：它遍历的是**金标**里的 sample_id，
            # 取判定用 `judged.get(sample_id)`。写在这里是为了让这份判定可审计
            # （哪个 rubric、哪个模型、跑了多少条、花了多少）。
            "__meta__": {
                "rubric_version": JUDGE_RUBRIC_VERSION,
                "model_name": llm_config.model_name,
                "temperature": llm_config.temperature,
                "samples": len(entries),
                "completed": len(collected),
                "stale_workspaces": len(stale),
                "dimensions": list(JUDGE_DIMENSIONS),
                "gold_csv": str(gold_path),
                # 实际用量落进判定文件：没有它，"这次 judge 花了多少"事后只能靠猜。
                "llm_calls": usage.llm_calls,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cost_usd_equivalent": usage.cost_usd(llm_config.model_name),
                "price_known": price_of(llm_config.model_name) is not None,
            },
            **collected,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _on_sample(sample_id: str, verdicts: Dict[str, Optional[bool]]) -> None:
        collected[sample_id] = verdicts
        _write()

    judged = asyncio.run(
        judge_entries(
            llm,
            entries,
            concurrency=args.concurrency,
            on_progress=_progress,
            on_sample=_on_sample,
        )
    )

    cost = usage.cost_usd(llm_config.model_name)
    print()
    print(
        f"实际用量：{usage.llm_calls} 次调用，"
        f"输入 {usage.prompt_tokens} + 输出 {usage.completion_tokens} = {usage.total_tokens} tokens"
    )
    if price_of(llm_config.model_name) is None:
        # 价格表查不到时必须**说出来**，否则成本会静默记成 $0 ——
        # 那比报错更危险（数字看起来合理，只是全错）。
        print(f"  ⚠️ 价格表里没有 {llm_config.model_name} → 成本按 0 记（**不是**真的免费）")
    else:
        print(f"  等价成本 ${cost:.4f}（按列表价算；订阅制下边际成本为 $0，这个数字用于横向对比，**不是**账单金额）")

    _write()  # 最终落盘（与逐条增量写的是同一个函数，避免两份逻辑走偏）

    # 逐维度统计：Unknown 比例高说明 judge 比人更保守（材料不足时不敢下结论）。
    # 那不是错误，但它会直接减少参与 κ 的对照样本 —— 所以必须看得见。
    print()
    print("各维度判定分布：")
    for dimension in JUDGE_DIMENSIONS:
        n_true = n_false = n_unknown = 0
        for sample_id in judged:
            value = (judged.get(sample_id) or {}).get(dimension)
            # 三态必须分开数：judge 答 Unknown（None）**不等于** false。
            # 先判 None 再判真值，而不是写 `value is True` —— 后者是合法惯用法，
            # 但会撞上 ast-grep 的 `no-identity-literal`（那条规则针对 `x is 1` 这种真 bug）。
            if value is None:
                n_unknown += 1
            elif value:
                n_true += 1
            else:
                n_false += 1
        print(f"  {dimension:22} true={n_true:3}  false={n_false:3}  unknown={n_unknown:3}")
    print(f"\n判定已写入 {out_path}")
    print(f"算 κ：python -m lab bench calibrate --gold {gold_path} --judged {out_path}")
    return 0


def _cmd_bench_list(args: argparse.Namespace) -> int:
    """`lab bench list`：列出历史评测。"""
    from lab.api import default_trace_store
    from lab.bench.report import render_suite_list

    print(render_suite_list(default_trace_store().list_suites(limit=args.limit)))
    return 0


def _cmd_sandbox_clean(args: argparse.Namespace) -> int:
    """`lab sandbox clean-escapes`：清理脚本逃逸到工作区外的残留。

    为什么默认 dry-run：它在删工作区**外面**的东西。
    即使路径是 `<盘>:/home/ubuntu` 这种几乎不可能是用户数据的目录，
    删除前也应该让人看一眼清单。
    """
    import shutil

    from lab.infra.local_sandbox import describe_escape_roots

    inventory = describe_escape_roots()
    if not inventory:
        print("没有检测到工作区外的残留（各盘的 home/ubuntu 都不存在）")
        return 0

    files = sum(item["files"] for item in inventory)
    total = sum(item["bytes"] for item in inventory)
    print(f"检测到 {len(inventory)} 个位置存在工作区外残留，共 {files} 个文件 / {total / 1048576:.2f} MB：")
    for item in inventory:
        print(f"  {item['path']}  {item['files']} 个文件 / {item['bytes'] / 1048576:.2f} MB")
    print("\n来源：脚本里写 `/home/ubuntu/x` 绝对路径时，会被解析到 `<当前盘>:/home/ubuntu`。")
    print("      沙箱路径映射只作用于文件工具与命令行，管不到脚本内容。")

    if not args.yes:
        print("\n（dry-run，什么都没删）加 --yes 才会真的删除。")
        return 0

    failed: List[str] = []
    for item in inventory:
        try:
            shutil.rmtree(item["path"])
        except OSError as exc:
            # 删不掉就说删不掉。**不能无条件打印"已删除"** —— 那会让人以为
            # 清理干净了，而残留还在（本项目纪律：失败要显式失败，不假装成功）。
            failed.append(f"{item['path']}（{exc}）")
            continue
        parent = Path(item["path"]).parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()  # 父目录是空的（只是逃逸时顺手建的），一起清掉
        except OSError:
            pass
        print(f"已删除 {item['path']}")
    if failed:
        print(f"\n⚠️ 有 {len(failed)} 个位置未能删除：")
        for line in failed:
            print(f"  {line}")
        return 1
    return 0


def _cmd_bench_noise_floor(args: argparse.Namespace) -> int:
    """`lab bench noise-floor`：测"同一配置跑两次能差多少"。

    做法：把**同一套配置**做成两个只有名字不同的臂，交错跑（A,B,B,A…）。
    两臂之间没有任何因果差异，所以观测到的差值除了采样噪声别无解释 ——
    那就是**测量分辨率**。

    为何不直接"把同一个 suite 跑两遍然后对比"：那两组会落在不同时段，
    测出来的"地板"里混进了服务端时间漂移 → 地板被高估 →
    把真实退化当噪声放过去。方向恰好是危险的那一边。
    """
    import asyncio

    from lab.api import default_trace_store, fault_rules_from_spec
    from lab.bench.noise import (
        measure_noise_floor,
        render_noise_floor,
        save_noise_floor,
    )
    from lab.bench.runner import ArmConfig, fault_spec_of, run_suite
    from lab.bench.task import load_all_tasks
    from lab.guard.config import GuardConfig

    tasks = load_all_tasks(group=args.group)
    if args.purpose and args.purpose != "all":
        tasks = [t for t in tasks if t.purpose == args.purpose]
    if args.task:
        # 定向重测（例如只测那个主导地板的量头任务）。
        # 它存在的理由是**归因成本**：全套重测 ~$5，单任务只 ~$0.9，
        # 而"这次修复到底把主导项按住了吗"用单任务就能回答。
        wanted = set(args.task)
        tasks = [t for t in tasks if t.key in wanted or t.uid in wanted]
        missing = wanted - {t.key for t in tasks} - {t.uid for t in tasks}
        if missing:
            print(f"[lab] 未找到指定的任务: {sorted(missing)}", file=sys.stderr)
            return 2
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        return _report_empty_tasks(args, in_scope=load_all_tasks(group=args.group), generic="[lab] 没有任务可测")
    if len(tasks) < 2 and not args.task:
        # 单任务只有在**显式 --task** 时才允许：那是定向重测，不是为了拿阀值。
        # （不加这个分支的话，--limit 1 会安静地产出一份没法用的地板）
        print("[lab] 任务太少，测不出噪声地板", file=sys.stderr)
        return 2

    # 分析口径：只统计这批任务。
    # ⚠️ 必须把 `include_uids` 传给 `measure_noise_floor`，而不是只依赖
    # "跑的时候已经把任务筛好了" —— 初版就是这么写的，而 `run_suite` 会自己
    # 重新 load 任务集，于是 `--purpose regression` **完全没生效**
    # （跑完 8 个任务才发现，里面包含本该排除的 capability 任务）。
    # 口径决定放在分析阶段，就不会被"忘了往下传"这种错安静地抹掉。
    include_uids = [task.uid for task in tasks]

    guard_config = GuardConfig.from_spec(args.guard) if args.guard else GuardConfig.from_env()
    fault_rules = fault_rules_from_spec(
        args.fault,
        tool=args.fault_tool,
        rate=args.fault_rate,
        latency_s=args.fault_latency,
        stable=args.fault_stable,
    )
    fault_spec = fault_spec_of(fault_rules)

    # ---- 只重算、不重跑：`--from-suite` ----
    # 存在的理由很具体：初版漏传了 purpose 过滤，跑完后才发现。
    # 如果只能重跑才能修正口径，那就得再花一次钱买**已经有**的数据。
    # 分析口径本来就是分析阶段的事，所以必须有一个免费的重算入口。
    if args.from_suite:
        store = default_trace_store()
        resolved = _resolve_suite_id(store, args.from_suite)
        if resolved is None:
            print(f"[lab] 无法唯一确定 suite：{args.from_suite}", file=sys.stderr)
            return 2
        existing = store.load_suite(resolved)
        if existing is None:
            print("[lab] 加载 suite 失败", file=sys.stderr)
            return 2
        floor = measure_noise_floor(
            existing,
            label=args.label or existing.label or "噪声地板（重算）",
            include_uids=include_uids if (args.purpose and args.purpose != "all") else None,
        )
        path = save_noise_floor(floor, Path(args.file) if args.file else None)
        print(render_noise_floor(floor))
        print(f"已重算并写入：{path}")
        print("（**未重跑任何任务、$0**：口径决定放在分析阶段，所以可以免费修正）")
        return 0

    print("\n噪声地板实验：**同一配置复制成两个臂**交错跑（这是关键，不是分两段跑）")
    print(f"  加固配置：{guard_config.describe()}")
    if fault_rules:
        print(f"  故障注入：{fault_spec}")
    print(f"  任务集  ：{len(tasks)} 个（purpose={args.purpose}）")

    store = default_trace_store()
    proceed, _ = _confirm_cost(
        store,
        tasks,
        args.runs,
        2,
        faulted=bool(args.fault),
        replay=args.replay,
        yes=args.yes,
    )
    if not proceed:
        return 2

    def _arm(label: str) -> ArmConfig:
        return ArmConfig(
            label=label,
            guard_spec=args.guard or "",
            guard_label=guard_config.label,
            guard_config=guard_config,
            fault_rules=fault_rules,
            fault_spec=fault_spec,
            replay=args.replay,
        )

    arms = [_arm(args.label_a), _arm(args.label_b)]
    suite = asyncio.run(
        run_suite(
            runs_per_task=args.runs,
            group=args.group,
            limit=None if args.task else args.limit,
            keys=args.task or None,
            # 把 purpose 也传给 run_suite，让"跑"与"算"的口径一致。
            purpose=(args.purpose if args.purpose and args.purpose != "all" else None),
            label=args.label or "噪声地板（同配置双臂交错）",
            bench_root=ensure_runs_dir() / "bench",
            concurrency=args.concurrency,
            temperature=args.temperature,
            guard_config=guard_config,
            fault_rules=fault_rules,
            arms=arms,
            interleave=True,
            replay=args.replay,
            replay_ignore_volatile=args.replay_ignore_volatile or None,
            replay_cache=args.replay_cache,
            on_start=store.save_suite_header,
            on_outcome=store.save_outcome,
            on_finish=store.finalize_suite,
        )
    )

    floor = measure_noise_floor(
        suite,
        label=args.label or "噪声地板（同配置双臂交错）",
        include_uids=include_uids if (args.purpose and args.purpose != "all") else None,
    )
    path = save_noise_floor(floor, Path(args.file) if args.file else None)

    print()
    print(render_noise_floor(floor))
    print(f"已写入：{path}")
    print(
        "（`bench gate` 会自动读它，把「测量分辨率」附在每个门禁旁边；加 --auto-thresholds 则直接把阈值抬到地板之上）"
    )
    return 0


def _cmd_bench_compare(args: argparse.Namespace) -> int:
    """`lab bench compare`：配对对比两个 suite（Step 5 的加固前后对比）。"""
    from lab.api import default_trace_store
    from lab.bench.compare import compare_suites, render_comparison

    store = default_trace_store()
    suites = store.list_suites(limit=200)
    if len(suites) < 1:
        print("[lab] 没有任何 suite 可对比", file=sys.stderr)
        return 2

    # ---- 拆臂模式：`compare <suite> --arm A --arm B` ----
    # 交错 A/B 跑出来的是**一个** suite（它才能共享同一段时间）。
    # 所以对比时先按臂拆，而不是要求用户去库里翻两个 suite。
    if args.arm:
        if len(args.arm) != 2:
            print("[lab] --arm 必须恰好传两次（要对比的两个臂）", file=sys.stderr)
            return 2
        resolved = _resolve_suite_id(store, args.baseline)
        if resolved is None:
            print(f"[lab] 无法唯一确定 suite：{args.baseline}", file=sys.stderr)
            return 2
        whole = store.load_suite(resolved)
        if whole is None:
            print("[lab] 加载 suite 失败", file=sys.stderr)
            return 2
        available = whole.arm_labels
        missing = [name for name in args.arm if _arm_slug(name) not in [_arm_slug(a) for a in available]]
        if missing:
            print(f"[lab] suite 里没有这些臂：{missing}；可选：{available}", file=sys.stderr)
            return 2
        baseline = whole.for_arm(args.arm[0])
        candidate = whole.for_arm(args.arm[1])
    else:
        if not args.candidate:
            print("[lab] 需要 candidate（或改用 --arm A --arm B 拆臂对比）", file=sys.stderr)
            return 2
        baseline_id = _resolve_suite_id(store, args.baseline)
        candidate_id = _resolve_suite_id(store, args.candidate)
        if baseline_id is None or candidate_id is None:
            print(f"[lab] 无法唯一确定 suite：{args.baseline} / {args.candidate}", file=sys.stderr)
            return 2
        baseline = store.load_suite(baseline_id)
        candidate = store.load_suite(candidate_id)
        if baseline is None or candidate is None:
            print("[lab] 加载 suite 失败", file=sys.stderr)
            return 2

    report = compare_suites(baseline, candidate)
    rendered = render_comparison(report)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"对比报告已写入: {args.out}")
    else:
        print(rendered)
    # 硬冲突（模型/温度/回放/任务集不同）用非 0 退出码表达：
    # 它能安静地跟 CI 里，防止"拿两个不同实验比出的 delta"被当成回归。
    return 3 if report.hard_confounds else 0


def _arm_slug(name: str) -> str:
    from lab.bench.runner import arm_slug

    return arm_slug(name)


def _resolve_suite_id(store: Any, short: str) -> Optional[str]:
    """支持短 id（唯一匹配才解析，拿不准就不猜）。"""
    matches = [
        row["suite_id"]
        for row in store.list_suites(limit=200)
        if row["suite_id"].startswith(short) or short in (row["label"] or "")
    ]
    return matches[0] if len(matches) == 1 else None


def _cmd_bench_pareto(args: argparse.Namespace) -> int:
    """`lab bench pareto`：成功率 vs 成本的 Pareto 表（文本形式）。"""
    from lab.api import default_trace_store
    from lab.bench.compare import render_pareto_groups

    store = default_trace_store()
    rows = store.list_suites(limit=200)
    if args.suite:
        wanted = [_resolve_suite_id(store, item) for item in args.suite]
        if any(item is None for item in wanted):
            print(f"[lab] 无法唯一确定 suite：{args.suite}", file=sys.stderr)
            return 2
        rows = [row for row in rows if row["suite_id"] in wanted]

    suites = []
    for row in rows:
        suite = store.load_suite(row["suite_id"])
        if suite and suite.outcomes:
            suites.append(suite)
    if not suites:
        print("[lab] 没有可用的 suite", file=sys.stderr)
        return 2

    print(render_pareto_groups(suites, min_runs=args.min_runs))
    return 0


def _cmd_bench_gate(args: argparse.Namespace) -> int:
    """`lab bench gate`：把对比结果变成**退出码**（回归看门狗）。

    为什么需要它：`bench compare` 只是把差异打印出来，得靠人去看。
    而"回归看门狗"要能一句话演示：改 prompt/工具后跑一条命令，delta 超阈值直接 CI 失败。
    """
    from lab.api import default_trace_store
    from lab.bench.compare import compare_suites, evaluate_gates, render_gates
    from lab.bench.noise import load_noise_floor, render_noise_floor

    store = default_trace_store()
    baseline_id = _resolve_suite_id(store, args.baseline)
    if baseline_id is None:
        print(f"[lab] 无法唯一确定基准 suite：{args.baseline}", file=sys.stderr)
        return 2

    if args.candidate:
        candidate_id = _resolve_suite_id(store, args.candidate)
    else:
        # 不传 candidate → 取最近一个不是基准的 suite
        others = [row["suite_id"] for row in store.list_suites(limit=200) if row["suite_id"] != baseline_id]
        candidate_id = others[0] if others else None
    if candidate_id is None:
        print("[lab] 无法确定候选 suite", file=sys.stderr)
        return 2

    baseline, candidate = store.load_suite(baseline_id), store.load_suite(candidate_id)
    if baseline is None or candidate is None:
        print("[lab] 加载 suite 失败", file=sys.stderr)
        return 2
    if len(candidate.outcomes) < args.min_runs:
        print(
            f"[lab] 候选 suite 只有 {len(candidate.outcomes)} 次运行"
            f"（< --min-runs {args.min_runs}）—— 样本太小，不参与门禁",
            file=sys.stderr,
        )
        return 2

    # 口径：门禁只看 regression 任务。
    # capability 任务的波动是「能力边界」，项目自己定义过它不进回归门禁
    # （实测：一个 8.3pt 的差异完全由 sem_markdown_toc 一个任务的 3 次运行决定）。
    # 注意：`bench compare`（给人看的）仍然用全量口径，两者用途不同 ——
    # 所以口径是**显式参数**，而不是"两边各自默默取一个默认值"。
    report = compare_suites(baseline, candidate, scope="regression")

    # ---- 噪声地板：门禁阈值的**分母** ----
    # 没有它，"阈值 5pt"到底是紧是松无法判断；而阈值的紧松直接决定
    # 门禁会不会因为噪声频繁变红（而频繁变红的门禁等于没有门禁）。
    floor = None
    if args.noise_floor != "none":
        floor = load_noise_floor(None if args.noise_floor == "auto" else Path(args.noise_floor))
    thresholds_from = args.auto_thresholds and floor is not None

    results = evaluate_gates(
        report,
        max_success_drop_pt=args.max_success_drop,
        max_passk_drop_pt=args.max_passk_drop,
        max_cost_increase_pct=args.max_cost_increase,
        require_significant=not args.allow_noisy,
        noise_floor=floor,
        auto_thresholds=thresholds_from,
    )

    print(f"基准: {baseline.label or baseline_id[:8]}（n={len(baseline.outcomes)}）")
    print(f"候选: {candidate.label or candidate_id[:8]}（n={len(candidate.outcomes)}）")
    print()

    # 实验条件不同 → 这不是"代码回归"，而可能是"拿两个不同实验在比"。
    # 不阻断（加固对照实验本来就靠这个），但必须最显眼。
    if report.hard_confounds:
        print(f"🔴 **实验条件不同，对比失去意义**：{report.hard_confounds}")
        print("   → 下面的门禁结论不可用。先让两边的模型/温度/回放模式/任务集一致。")
        print()
    elif report.config_differences:
        print(f"🟡 实验条件差异（确认是有意设计的自变量）：{report.config_differences}")
        print()

    if floor is not None:
        print(render_noise_floor(floor))
        if thresholds_from:
            print("> ✅ `--auto-thresholds`：阈值已抬到不小于噪声地板。")
        else:
            too_tight = []
            for name, threshold, unit in (
                ("success_pt", args.max_success_drop, "pt"),
                ("cost_pct", args.max_cost_increase, "%"),
            ):
                metric = floor.get(name)
                if metric and metric.resolution > threshold:
                    too_tight.append(
                        f"{metric.label}(阈值 {threshold:.1f}{unit} < 地板 ±{metric.resolution:.2f}{unit})"
                    )
            if too_tight:
                print(f"> ⚠️ **阈值比噪声地板还紧**：{'；'.join(too_tight)}")
                print(">    这种门禁会因为噪声频繁变红 —— 而**频繁变红的门禁等于没有门禁**。")
                print(">    要么加 `--auto-thresholds`，要么先加任务数/次数把地板压下来。")
                print()
    else:
        print("> ⚠️ **没测过噪声地板** → 无法判断下面的阈值是否比噪声还紧。")
        print(">    建议先跑一次：`python -m lab bench noise-floor --runs 3 --group semireal`")
        print()

    print(render_gates(results))
    print()

    failed = [item for item in results if not item.passed]
    if failed:
        print(f"❌ 门禁未通过（{len(failed)}/{len(results)} 项）：")
        for item in failed:
            print(f"   - {item.name}: {item.observed}（阈值 {item.threshold}）")
        return 1
    print(f"✅ 全部 {len(results)} 项门禁通过")
    print(
        ">（注意：这不等于「没有退化」—— 它只说明本次差值没有超过阈值。"
        + ("）" if floor is not None else "而**噪声地板未测**，所以小于阈值的变化看不出来。）")
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab",
        description="agent-reliability-lab：Agent 可靠性评测实验台",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="执行一个任务并输出结构化结果")
    run_parser.add_argument("goal", help="自然语言任务目标")
    run_parser.add_argument("--workspace", default=None, help="自定义任务工作区目录")
    run_parser.add_argument("--max-seconds", type=float, default=300.0, help="任务级硬超时（秒）")
    run_parser.add_argument("--exec-timeout", type=int, default=60, help="单条命令超时（秒）")
    run_parser.add_argument("--max-iterations", type=int, default=None, help="覆盖单次 invoke 迭代上限")
    run_parser.add_argument("--temperature", type=float, default=None, help="覆盖温度（默认 0.0 可复现）")
    run_parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    run_parser.add_argument("--no-trace", action="store_true", help="不采集轨迹（测纯执行速度时用）")
    run_parser.add_argument(
        "--fault",
        action="append",
        default=None,
        help="注入故障（可多次/逗号分隔）：timeout, transient_error, permanent_error, malformed_result, "
        "empty_result, truncated_result, silent_wrong_result, partial_write, latency_spike, flaky",
    )
    run_parser.add_argument("--fault-tool", default="*", help="故障只作用于匹配的工具名（支持 glob，如 'shell_*'）")
    run_parser.add_argument("--fault-rate", type=float, default=1.0, help="故障命中概率 0~1")
    run_parser.add_argument("--fault-latency", type=float, default=2.0, help="latency_spike 注入的额外延迟（秒）")
    run_parser.add_argument(
        "--fault-stable",
        action="store_true",
        help="按**目标**（文件路径）而非调用序号注入：同一文件全程保持同一种损坏",
    )
    run_parser.add_argument(
        "--replay",
        default=None,
        choices=["off", "record", "reuse", "replay"],
        help="LLM 录制回放：off=不用缓存；record=总联网并记录（顺带测服务端非确定性）；"
        "reuse=命中即用/未命中联网（改了 prompt 只想重跑受影响的那部分）；"
        "replay=**严格离线**（未命中就失败，不需要 API Key）",
    )
    run_parser.add_argument("--replay-cache", default=None, help="缓存库路径（默认 lab/runs/llm_cache.db）")
    run_parser.add_argument(
        "--replay-ignore-volatile",
        action="store_true",
        help="算键前抹平 UUID / 时间戳（有些 SUT 会把随机计划 id 放进提示词，"
        "不抹平就永远未命中）。**不需要重新录制**：副键在录制时已一并存下。"
        "建议先看 `--replay replay` 的未命中诊断，确认确实只是 UUID 再开",
    )
    run_parser.set_defaults(func=_cmd_run)

    trace_parser = sub.add_parser("trace", help="查看一次运行的轨迹树")
    trace_parser.add_argument("task_id", nargs="?", default=None, help="任务 id（默认取最近一次，支持短 id 前缀）")
    trace_parser.add_argument("--top", type=int, default=5, help="汇总里展示最慢的前 N 个操作")
    trace_parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    trace_parser.set_defaults(func=_cmd_trace)

    stats_parser = sub.add_parser("stats", help="跨运行聚合指标")
    stats_parser.add_argument("--sut", default=None, help="按 SUT 名字过滤（如 manus-planner-react[fast]）")
    stats_parser.set_defaults(func=_cmd_stats)

    # ==================== LLM 录制回放 ====================
    replay_parser = sub.add_parser("replay", help="LLM 录制回放（离线复现 / 服务端非确定性度量）")
    replay_sub = replay_parser.add_subparsers(dest="replay_command", required=True)
    replay_stats = replay_sub.add_parser("stats", help="看缓存内容 + **同一请求不同响应**的比例（服务端非确定性）")
    replay_stats.add_argument("--cache", default=None, help="缓存库路径（默认 lab/runs/llm_cache.db）")
    replay_stats.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    replay_stats.set_defaults(func=_cmd_replay_stats)

    # ==================== 评测 harness ====================
    bench_parser = sub.add_parser("bench", help="评测 harness（任务集 / 执行器 / 判定器 / 报告）")
    bench_sub = bench_parser.add_subparsers(dest="bench_command", required=True)

    bench_tasks = bench_sub.add_parser("tasks", help="列出任务集")
    bench_tasks.add_argument("--group", default=None, help="只列某个分组")
    bench_tasks.set_defaults(func=_cmd_bench_tasks)

    bench_validate = bench_sub.add_parser("validate", help="任务集三步自检（不花钱；跑评测前必做）")
    bench_validate.add_argument("--group", default=None)
    bench_validate.set_defaults(func=_cmd_bench_validate)

    bench_run = bench_sub.add_parser("run", help="跑评测（会花真钱）")
    bench_run.add_argument("--runs", type=int, default=1, help="每个任务重复次数（n）")
    bench_run.add_argument(
        "--sut",
        default="manus",
        choices=["manus", "minimal-react"],
        help="被测对象：manus（课程版 PlannerReAct，默认）/ minimal-react（本仓库自研的最小 loop，T10）。"
        "两条 loop 复用同一套工具与加固中间件，所以差异只在控制流。",
    )
    bench_run.add_argument("--group", default=None)
    bench_run.add_argument(
        "--purpose",
        default="all",
        choices=["capability", "regression", "all"],
        help="只跑某一类任务：capability（能力边界，允许不稳定）/ regression（回归门禁）/ "
        "all（默认，全部）。默认 all 是为了**不改变既有行为**；"
        "做对照实验要排掉不稳定任务时才用 regression。",
    )
    bench_run.add_argument("--limit", type=int, default=None, help="只跑前 N 个任务（先验证链路）")
    bench_run.add_argument(
        "--task", action="append", default=None, help="只跑指定的任务 key（可多次；用于补跑失败任务或单任务对照）"
    )
    bench_run.add_argument("--label", default="", help="给本次评测起个名字")
    bench_run.add_argument("--concurrency", type=int, default=1, help="并发度（>1 会让耗时不可比）")
    bench_run.add_argument("--temperature", type=float, default=None)
    bench_run.add_argument("--budget-mode", default=None, choices=["observe", "degrade", "enforce"])
    bench_run.add_argument(
        "--budget-defaults",
        action="store_true",
        help="用固定的默认阈值（默认行为是按历史分位数 per-task 推导）",
    )
    bench_run.add_argument(
        "--guard",
        default=None,
        help="加固开关（对照实验用）：all / none / retry,postcondition,loop,budget；"
        "不传则读环境变量 LAB_GUARD，都没有则 all",
    )
    bench_run.add_argument("--fault", action="append", default=None, help="注入故障（可多次/逗号分隔）")
    bench_run.add_argument("--fault-tool", default="*")
    bench_run.add_argument("--fault-rate", type=float, default=1.0)
    # 注意：子命令的每个参数都必须在**本子命令**上声明。
    # 实测踩过两次：`ensure_runs_dir` 没 import、`--fault-latency` 只声明在顶层 run 上 ——
    # 都是"代码引用了子命令里不存在的属性"，只有真跑到那条分支才会炸。
    # 所以 test_cli.py 里每加一个子命令就要有一条端到端调用它的测试。
    bench_run.add_argument("--fault-latency", type=float, default=2.0, help="latency_spike 注入的额外延迟（秒）")
    bench_run.add_argument(
        "--fault-stable",
        action="store_true",
        help="按**目标**（文件路径）而非调用序号注入：同一文件全程保持同一种损坏（让重读自证失效）",
    )
    # ---- 交错 A/B（分离时间混淆的唯一办法）----
    # 为什么用可重复的 --ab-guard 而不是新建一个 `bench ab` 命令：
    # 交错必须落在**同一个 suite** 里才能共享同一段时间，
    # 而 suite 的构造（任务集/预算/落库/报告）已经全部现成 ——
    # 另开一条命令只会把这些逻辑复制一遍，然后慢慢跑偏。
    bench_run.add_argument(
        "--ab-guard",
        action="append",
        default=None,
        metavar="SPEC",
        help="交错 A/B 的加固臂（可重复 2 次以上）。例："
        "--ab-guard none --ab-guard retry,postcondition,enforce。"
        "传了就按轮次交替跑（A,B,B,A,…），以消除「两组跑在不同时段」的时间混淆。"
        "`--runs N` 的含义变成**每个臂各 N 次**。传两个完全相同的 spec 就是**噪声地板实验**。",
    )
    bench_run.add_argument(
        "--ab-label",
        action="append",
        default=None,
        metavar="NAME",
        help="给对应的 --ab-guard 臂起名（顺序对应；不传则用 guard label）。噪声地板实验必须用它区分两个同配置的臂。",
    )
    bench_run.add_argument(
        "--no-interleave",
        action="store_true",
        help="多臂时不做交错（按臂分组顺序跑）—— **仅供对照**，会重新引入时间混淆",
    )
    bench_run.add_argument(
        "--replay",
        default=None,
        choices=["off", "record", "reuse", "replay"],
        help="LLM 录制回放模式（见 `lab run --help`）；replay 模式不花钱、不需要 API Key",
    )
    bench_run.add_argument("--replay-cache", default=None, help="缓存库路径")
    bench_run.add_argument(
        "--replay-ignore-volatile",
        action="store_true",
        help="算键前抹平 UUID / 时间戳（见 `lab run --help`）；解决「SUT 提示词里带随机 id」导致的必然未命中",
    )
    bench_run.add_argument("--report", action="store_true", help="跑完直接生成 Markdown 报告")
    bench_run.add_argument("--yes", action="store_true", help="确认可能较高的成本")
    bench_run.set_defaults(func=_cmd_bench_run)

    bench_report = bench_sub.add_parser("report", help="从历史评测重新生成报告（不重跑）")
    bench_report.add_argument("suite_id", nargs="?", default=None, help="支持短 id")
    bench_report.add_argument("--out", default=None, help="写入文件而不是打印")
    bench_report.set_defaults(func=_cmd_bench_report)

    bench_list = bench_sub.add_parser("list", help="列出历史评测")
    bench_list.add_argument("--limit", type=int, default=10)
    bench_list.set_defaults(func=_cmd_bench_list)

    bench_audit = bench_sub.add_parser("purpose-audit", help="标签 vs 实测对账（regression 任务出现干净失败就告警）")
    bench_audit.set_defaults(func=_cmd_bench_purpose_audit)

    bench_sheet = bench_sub.add_parser("judge-sheet", help="生成待人工标注的 CSV（LLM-judge 校准用；$0）")
    bench_sheet.add_argument("--n", type=int, default=100, help="抽多少条（失败优先，再补成功）")
    bench_sheet.add_argument("--out", default=None, help="输出路径（默认 lab/runs/judge_gold.csv）")
    # P3a 分级抽样：排除 stale / 缺陷任务，并补上跨任务的分级负样本
    bench_sheet.add_argument(
        "--balanced",
        action="store_true",
        help="分级抽样：剔 stale 工作区 + 按任务配额 + 造分级负样本（默认：旧的“失败优先”）",
    )
    bench_sheet.add_argument(
        "--exclude-task",
        action="append",
        default=None,
        help="排除某个任务（可重复；--balanced 下默认排除两个已修缺陷的任务）",
    )
    bench_sheet.add_argument("--per-task", type=int, default=2, help="每个任务最多取几条正/负样本（--balanced）")
    bench_sheet.add_argument(
        "--level",
        action="append",
        default=None,
        help="造哪几级负样本（可重复：mild / wrong_solution / empty；--balanced）",
    )
    bench_sheet.set_defaults(func=_cmd_bench_judge_sheet)

    bench_cal = bench_sub.add_parser("calibrate", help="用人工金标算 judge 的 Cohen's κ（不达标就拒绝启用；$0）")
    bench_cal.add_argument("--gold", required=True, help="填好的人工金标 CSV")
    bench_cal.add_argument(
        "--judged", default=None, help="judge 的判定 JSON（可选；缺它就只报「无法计算」，不会编数字）"
    )
    bench_cal.set_defaults(func=_cmd_bench_calibrate)

    bench_judge = bench_sub.add_parser("judge", help="对标注表跑 LLM-judge，产出判定 JSON（calibrate 的输入；💰）")
    bench_judge.add_argument("--gold", default=None, help="标注表 CSV（默认 lab/runs/judge_gold.csv）")
    bench_judge.add_argument("--out", default=None, help="判定 JSON 输出路径（默认 lab/runs/judge_judged.json）")
    bench_judge.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    bench_judge.add_argument("--concurrency", type=int, default=4, help="并发数（默认 4）")
    bench_judge.add_argument("--temperature", type=float, default=0.0, help="judge 的温度（默认 0.0：要可复现）")
    bench_judge.add_argument("--yes", action="store_true", help="确认付费（不带它只打印预估）")
    bench_judge.set_defaults(func=_cmd_bench_judge)

    bench_show = bench_sub.add_parser("show", help="读一次具体运行的轨迹（工具序列 + 失败检查 + 关键产物 + 轨迹树）")
    bench_show.add_argument("suite", help="suite（支持短 id 或 label 片段）")
    bench_show.add_argument("task", help="任务 key 或 uid")
    bench_show.add_argument("--run", type=int, default=None, help="第几次运行（不传则展示全部匹配，最多 --limit 个）")
    bench_show.add_argument("--arm", default=None, help="交错 A/B 的臂名")
    bench_show.add_argument("--limit", type=int, default=3, help="最多展示几个匹配运行")
    bench_show.add_argument("--top", type=int, default=6, help="轨迹树里展示最慢的前 N 项")
    bench_show.add_argument("--artifacts", type=int, default=4, help="展示几个关键产物")
    bench_show.add_argument("--no-trace", action="store_true", help="只给摘要，不打印轨迹树")
    bench_show.set_defaults(func=_cmd_bench_show)

    bench_sample = bench_sub.add_parser("sample-failures", help="从库里挑几条失败运行来读（读 transcript 的入口）")
    bench_sample.add_argument("--suite", default=None, help="只在这个 suite 里挑（支持短 id）")
    bench_sample.add_argument("--n", type=int, default=5, help="挑几条")
    bench_sample.add_argument("--full", action="store_true", help="同时打印轨迹树（很长）")
    bench_sample.add_argument("--top", type=int, default=6)
    bench_sample.add_argument("--artifacts", type=int, default=3)
    bench_sample.set_defaults(func=_cmd_bench_sample_failures)

    bench_compare = bench_sub.add_parser("compare", help="配对对比两个 suite（加固前后，按任务配对）")
    bench_compare.add_argument("baseline", help="基准 suite（支持短 id 或 label 片段）")
    bench_compare.add_argument(
        "candidate",
        nargs="?",
        default=None,
        help="候选 suite（用 --arm 拆臂对比时不需要）",
    )
    bench_compare.add_argument(
        "--arm",
        action="append",
        default=None,
        metavar="NAME",
        help='从**同一个交错 suite** 里拆两个臂来对比（恰好传两次）。例：--arm "guard=none" --arm "guard=all"',
    )
    bench_compare.add_argument("--out", default=None, help="写入文件而不是打印")
    bench_compare.set_defaults(func=_cmd_bench_compare)

    bench_noise = bench_sub.add_parser(
        "noise-floor",
        help="测噪声地板：**同一配置**复制成两个臂交错跑，得到「小于它看不出来」的门槛",
    )
    bench_noise.add_argument("--runs", type=int, default=3, help="每个臂每个任务跑几次")
    bench_noise.add_argument("--group", default=None)
    bench_noise.add_argument("--limit", type=int, default=None)
    bench_noise.add_argument(
        "--task",
        action="append",
        default=None,
        metavar="KEY",
        help="只测指定任务（可多次）。用于**定向重测**主导项：全套 ~$5，单任务 ~$0.9",
    )
    bench_noise.add_argument(
        "--purpose",
        default="regression",
        choices=["capability", "regression", "all"],
        help="默认只用 regression 任务：capability 任务的波动是「能力边界」（能力问题），"
        "把它算进测量分辨率会让地板大到没有意义",
    )
    bench_noise.add_argument("--guard", default=None, help="要在哪个加固配置下测地板（默认 all）")
    bench_noise.add_argument("--fault", action="append", default=None)
    bench_noise.add_argument("--fault-tool", default="*")
    bench_noise.add_argument("--fault-rate", type=float, default=1.0)
    bench_noise.add_argument("--fault-latency", type=float, default=2.0)
    bench_noise.add_argument("--fault-stable", action="store_true")
    bench_noise.add_argument("--label", default="", help="给本次测量起名")
    bench_noise.add_argument("--label-a", default="noise-a", help="第一个臂的名字")
    bench_noise.add_argument("--label-b", default="noise-b", help="第二个臂的名字")
    bench_noise.add_argument(
        "--from-suite",
        default=None,
        metavar="SUITE_ID|LABEL",
        help="**不重跑、$0**：从已有的交错 suite 重算地板（修正分析口径时用）",
    )
    bench_noise.add_argument("--file", default=None, help="地板文件路径（默认 lab/runs/noise_floor.json）")
    bench_noise.add_argument("--concurrency", type=int, default=1)
    bench_noise.add_argument("--temperature", type=float, default=None)
    bench_noise.add_argument("--replay", default=None, choices=["off", "record", "reuse", "replay"])
    bench_noise.add_argument("--replay-cache", default=None, help="缓存库路径")
    bench_noise.add_argument(
        "--replay-ignore-volatile",
        action="store_true",
        help="算键前抹平 UUID / 时间戳（解决「SUT 提示词带随机 id」导致的必然未命中）",
    )
    bench_noise.add_argument("--yes", action="store_true", help="确认可能较高的成本")
    bench_noise.set_defaults(func=_cmd_bench_noise_floor)

    bench_pareto = bench_sub.add_parser("pareto", help="成功率 vs 成本的 Pareto 表（文本；图在当前模型下不可用）")
    bench_pareto.add_argument("--suite", action="append", default=None, help="只纳入指定 suite（可多次）")
    bench_pareto.add_argument(
        "--min-runs", type=int, default=10, help="忽略运行数少于此值的 suite（默认 10：n 太小会因运气而'支配'一切）"
    )
    bench_pareto.set_defaults(func=_cmd_bench_pareto)

    bench_gate = bench_sub.add_parser("gate", help="回归门禁：delta 超阈值 → 退出码非 0（可用于 CI）")
    bench_gate.add_argument("--baseline", required=True, help="基准 suite（短 id 或 label 片段）")
    bench_gate.add_argument("--candidate", default=None, help="候选 suite（默认取最近一个）")
    bench_gate.add_argument("--max-success-drop", type=float, default=5.0, help="运行级成功率最多下降多少百分点")
    bench_gate.add_argument("--max-passk-drop", type=float, default=10.0, help="pass^k 最多下降多少百分点")
    bench_gate.add_argument("--max-cost-increase", type=float, default=20.0, help="成本最多上升百分之多少")
    bench_gate.add_argument("--min-runs", type=int, default=10, help="候选 suite 少于这么多次运行则不判")
    bench_gate.add_argument(
        "--allow-noisy",
        action="store_true",
        help="即使差值方向不可信（区间跨 0）也按阈值判定；默认不这样（否则噪声会频繁弄红 CI）",
    )
    bench_gate.add_argument(
        "--noise-floor",
        default="auto",
        metavar="PATH|auto|none",
        help="噪声地板文件（默认 auto：读 lab/runs/noise_floor.json，没有就按未测处理）。"
        "读了它，每个门禁旁会附上实测分辨率；没测过会显式提醒",
    )
    bench_gate.add_argument(
        "--auto-thresholds",
        action="store_true",
        help="把阈值抬到「不小于噪声地板」。默认不抬（只报告），因为抬阈值会放过小退化，这个取舍应该由人显式做",
    )
    bench_gate.set_defaults(func=_cmd_bench_gate)

    bench_recover = bench_sub.add_parser("recover", help="从工作区 + 轨迹库重建一次评测（评测被中断后救数据）")
    bench_recover.add_argument("--suite-id", required=True, help="给重建的评测指定一个 id")
    bench_recover.add_argument("--label", default="", help="评测名字")
    bench_recover.add_argument("--runs", type=int, default=None, help="每个任务的重复次数（用于报告文案）")
    bench_recover.add_argument("--note", default="", help="附加到报告备注的说明")
    bench_recover.add_argument("--group", default=None, help="只重建指定分组（必填，否则会混入其它分组的旧工作区）")
    bench_recover.add_argument("--report", action="store_true", help="重建后直接出报告")
    bench_recover.set_defaults(func=_cmd_bench_recover)

    # ==================== 沙箱维护 ====================
    sandbox_parser = sub.add_parser("sandbox", help="沙箱维护（fast mode 的边界与残留）")
    sandbox_sub = sandbox_parser.add_subparsers(dest="sandbox_command", required=True)

    clean_parser = sandbox_sub.add_parser(
        "clean-escapes", help="清理脚本逃逸到工作区外的残留（默认只报告，加 --yes 才删）"
    )
    clean_parser.add_argument("--yes", action="store_true", help="确认删除（默认 dry-run）")
    clean_parser.set_defaults(func=_cmd_sandbox_clean)

    env_parser = sandbox_sub.add_parser("env", help="打印 Agent 会看到的环境指纹（工具面 + 短摘要）")
    env_parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    env_parser.set_defaults(func=_cmd_sandbox_env)

    return parser


def _cmd_sandbox_env(args: argparse.Namespace) -> int:
    """`lab sandbox env`：Agent 到底能看到哪些工具。

    为什么值得一条命令：实测同一个任务从 Git Bash 启动与从 PowerShell 启动，
    工具面完全不同（后者连 python 都没有）—— 而那种差异会让两次跑不可比。
    把环境变成一条可查、可落库、可 GROUP BY 的事实，而不是假设。
    """
    from lab.infra.env import environment_fingerprint, render_fingerprint

    fingerprint = environment_fingerprint()
    if args.json:
        print(json.dumps(fingerprint, ensure_ascii=False, indent=2))
    else:
        print(render_fingerprint(fingerprint))
    return 0


def main(argv=None) -> int:
    # 必须在 argparse **之前**：--help 与参数错误也是打印输出，同样会碰到 GBK 问题。
    _fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
