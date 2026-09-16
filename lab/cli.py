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

from lab.api import LabConfigError, default_trace_store, fault_rules_from_spec, run_task_sync
from lab.bootstrap import ensure_runs_dir
from lab.sut.base import TaskResult
from lab.trace.view import render_trace


def _print_human(result: TaskResult) -> None:
    """人类可读输出：先给结论，再给证据。"""
    print("")
    print("=" * 72)
    print(result.summary_line())
    print("=" * 72)

    if result.plan_title:
        print(f"计划标题 : {result.plan_title}")
    if result.plan_goal:
        print(f"计划目标 : {result.plan_goal}")
    print(f"步骤     : 总 {result.steps.total} / 已结束 {result.steps.done} "
          f"/ 成功 {result.steps.succeeded} / 失败 {result.steps.failed}")
    print(f"工具序列 : {' -> '.join(result.tool_sequence) if result.tool_sequence else '(无)'}")
    print(f"LLM 调用 : {result.llm_usage.llm_calls} 次（失败 {result.llm_usage.llm_errors} 次），"
          f"耗时 {result.llm_usage.llm_latency_ms} ms")
    print(f"Token    : 输入 {result.llm_usage.prompt_tokens} + 输出 {result.llm_usage.completion_tokens} "
          f"= {result.llm_usage.total_tokens}")
    print(f"成本     : ${result.cost_usd:.6f}")
    print(f"耗时     : {result.elapsed_ms} ms")

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
    print("")


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
        print(f"预算[{budget.get('mode')}]  : {flag} 越界维度 {violated or '无'} "
              f"(检查 {budget.get('checks')} 次；本会中止={budget.get('would_stop')})")
        if budget.get("final"):
            final = budget["final"]
            print(f"          最终值 tokens={final.get('tokens')} cost=${final.get('cost_usd')} "
                  f"elapsed={final.get('elapsed_s')}s steps={final.get('steps')} tools={final.get('tool_calls')}")

    loop = guard.get("loop") or {}
    if loop:
        print(f"循环检测 : 最长连续重复 {loop.get('max_consecutive_repeat')} 次，"
              f"熔断候选 {loop.get('repeat_trips')} 次，动作多样性 {loop.get('action_diversity')}")

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
                args.fault, tool=args.fault_tool, rate=args.fault_rate, latency_s=args.fault_latency
            ),
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
        print("[lab] 还没有任何运行记录，先跑一次：python -m lab run \"...\"", file=sys.stderr)
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
        print(json.dumps(
            {"task": task, "spans": [s.model_dump(mode="json") for s in spans]},
            ensure_ascii=False, indent=2,
        ))
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

    print("")
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
    print("")
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


def _cmd_bench_run(args: argparse.Namespace) -> int:
    """`lab bench run`：跑评测并出报告。"""
    import asyncio

    from lab.api import default_trace_store, fault_rules_from_spec
    from lab.bench.report import render_report
    from lab.bench.runner import run_suite
    from lab.bench.task import load_all_tasks
    from lab.guard.budget import BudgetMode, BudgetPolicy

    tasks = load_all_tasks(group=args.group)
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t.key in wanted or t.uid in wanted]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("[lab] 没有找到任务", file=sys.stderr)
        return 2

    # 花钱的事要先说清楚。估算基于"n=5 基线里最贵的任务"，属于上限估计。
    runs = args.runs * len(tasks)
    estimate = runs * 0.045
    print(f"即将运行 {len(tasks)} 个任务 × {args.runs} 次 = **{runs} 次真模型调用**")
    print(f"预估成本上限 ≈ ${estimate:.2f}（基于历史上最贵的单次任务估算）")
    if estimate > 1.0 and not args.yes:
        print("\n[lab] 预估成本超过 $1.00，请确认后加 --yes 重跑。", file=sys.stderr)
        print("      先用小规模验证链路：--limit 2 --runs 1", file=sys.stderr)
        return 2

    budget_policy = BudgetPolicy.from_env()
    if args.budget_mode:
        budget_policy = budget_policy.model_copy(update={"mode": BudgetMode(args.budget_mode)})

    suite = asyncio.run(run_suite(
        runs_per_task=args.runs,
        group=args.group,
        limit=args.limit,
        keys=args.task,
        label=args.label,
        bench_root=ensure_runs_dir() / "bench",
        concurrency=args.concurrency,
        temperature=args.temperature,
        budget_policy=budget_policy,
        fault_rules=fault_rules_from_spec(
            args.fault, tool=args.fault_tool, rate=args.fault_rate
        ),
    ))

    store = default_trace_store()
    store.save_suite(suite)
    print("")
    print(f"评测完成: {suite.successes}/{suite.total_runs} 成功"
          f"（{suite.success_rate.point:.1%}，95% 区间 [{suite.success_rate.low:.1%}, "
          f"{suite.success_rate.high:.1%}]），耗时 {suite.elapsed_s}s")

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
    elif len(suite_id) < 36:
        # 支持短 id：取唯一匹配
        matched = [row for row in store.list_suites(limit=200) if row["suite_id"].startswith(suite_id)]
        if len(matched) != 1:
            print(f"[lab] 无法唯一确定 suite: {suite_id}（匹配 {len(matched)} 个）", file=sys.stderr)
            return 2
        suite_id = matched[0]["suite_id"]

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


def _cmd_bench_list(args: argparse.Namespace) -> int:
    """`lab bench list`：列出历史评测。"""
    from lab.api import default_trace_store
    from lab.bench.report import render_suite_list

    print(render_suite_list(default_trace_store().list_suites(limit=args.limit)))
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
        "--fault", action="append", default=None,
        help="注入故障（可多次/逗号分隔）：timeout, transient_error, permanent_error, malformed_result, "
             "empty_result, truncated_result, silent_wrong_result, partial_write, latency_spike, flaky",
    )
    run_parser.add_argument("--fault-tool", default="*", help="故障只作用于匹配的工具名（支持 glob，如 'shell_*'）")
    run_parser.add_argument("--fault-rate", type=float, default=1.0, help="故障命中概率 0~1")
    run_parser.add_argument("--fault-latency", type=float, default=2.0, help="latency_spike 注入的额外延迟（秒）")
    run_parser.set_defaults(func=_cmd_run)

    trace_parser = sub.add_parser("trace", help="查看一次运行的轨迹树")
    trace_parser.add_argument("task_id", nargs="?", default=None, help="任务 id（默认取最近一次，支持短 id 前缀）")
    trace_parser.add_argument("--top", type=int, default=5, help="汇总里展示最慢的前 N 个操作")
    trace_parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    trace_parser.set_defaults(func=_cmd_trace)

    stats_parser = sub.add_parser("stats", help="跨运行聚合指标")
    stats_parser.add_argument("--sut", default=None, help="按 SUT 名字过滤（如 manus-planner-react[fast]）")
    stats_parser.set_defaults(func=_cmd_stats)

    # ==================== 评测 harness ====================
    bench_parser = sub.add_parser("bench", help="评测 harness（任务集 / 执行器 / 判定器 / 报告）")
    bench_sub = bench_parser.add_subparsers(dest="bench_command", required=True)

    bench_tasks = bench_sub.add_parser("tasks", help="列出任务集")
    bench_tasks.add_argument("--group", default=None, help="只列某个分组")
    bench_tasks.set_defaults(func=_cmd_bench_tasks)

    bench_validate = bench_sub.add_parser(
        "validate", help="任务集三步自检（不花钱；跑评测前必做）"
    )
    bench_validate.add_argument("--group", default=None)
    bench_validate.set_defaults(func=_cmd_bench_validate)

    bench_run = bench_sub.add_parser("run", help="跑评测（会花真钱）")
    bench_run.add_argument("--runs", type=int, default=1, help="每个任务重复次数（n）")
    bench_run.add_argument("--group", default=None)
    bench_run.add_argument("--limit", type=int, default=None, help="只跑前 N 个任务（先验证链路）")
    bench_run.add_argument("--task", action="append", default=None,
                           help="只跑指定的任务 key（可多次；用于补跑失败任务或单任务对照）")
    bench_run.add_argument("--label", default="", help="给本次评测起个名字")
    bench_run.add_argument("--concurrency", type=int, default=1, help="并发度（>1 会让耗时不可比）")
    bench_run.add_argument("--temperature", type=float, default=None)
    bench_run.add_argument("--budget-mode", default=None, choices=["observe", "degrade", "enforce"])
    bench_run.add_argument("--fault", action="append", default=None, help="注入故障（可多次/逗号分隔）")
    bench_run.add_argument("--fault-tool", default="*")
    bench_run.add_argument("--fault-rate", type=float, default=1.0)
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

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
