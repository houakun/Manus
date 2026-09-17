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

    store = default_trace_store()
    # 花钱的事要先说清楚。
    # 早期版本用一个写死的系数（$0.045/次）估算，而那个系数来自冒烟测试的简单任务 ——
    # 实测 semireal 任务的平均成本是它的 3 倍（预估 $1.8 / 实际 $5.0）。
    # 花钱的事上估错 3 倍不可接受，所以改成：优先用库里同组任务的实测均值，
    # 没历史时用一个保守值（宁可高估也不要让用户被意外收费）。
    runs = args.runs * len(tasks)
    groups = {task.group for task in tasks}
    historical = [
        cost for cost in (store.avg_cost_for_group(name) for name in groups) if cost is not None
    ]
    if historical:
        per_run = max(historical)  # 多分组时取最贵的，偏保守
        basis = f"库中同组历史均值（{'/'.join(sorted(groups))}）"
    else:
        per_run = 0.15
        basis = "保守默认值 $0.15/次（库里还没有同类任务的历史）"
    estimate = runs * per_run

    print(f"即将运行 {len(tasks)} 个任务 × {args.runs} 次 = **{runs} 次真模型调用**")
    print(f"预估成本 ≈ ${estimate:.2f}（依据：{basis}，单次 ≈ ${per_run:.3f}）")
    if estimate > 1.0 and not args.yes:
        print("\n[lab] 预估成本超过 $1.00，请确认后加 --yes 重跑。", file=sys.stderr)
        print("      先用小规模验证链路：--limit 2 --runs 1", file=sys.stderr)
        return 2

    budget_policy = BudgetPolicy.from_env()
    if args.budget_mode:
        budget_policy = budget_policy.model_copy(update={"mode": BudgetMode(args.budget_mode)})

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
            print("\n[lab] 库里还没有足够的同类历史 → 本次用默认阈值"
                  "（跑完一轮后就会开始用历史校准）")

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
        budget_policy_by_task=policy_by_task,
        fault_rules=fault_rules_from_spec(
            args.fault, tool=args.fault_tool, rate=args.fault_rate
        ),
        # 增量落库：长评测（40 次运行 ≈ 半小时）被中断的途径很多
        # （Ctrl-C、机器休眠、终端关闭、远程会话断开），
        # 每次运行完成就落库 → 最坏情况只丢一次运行，而不是丢掉整份数据。
        on_start=store.save_suite_header,
        on_outcome=store.save_outcome,
        on_finish=store.finalize_suite,
    ))

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


def _cmd_bench_recover(args: argparse.Namespace) -> int:
    """`lab bench recover`：从工作区 + 轨迹库重建一次评测（崩溃后救数据）。"""
    import asyncio

    from lab.api import default_trace_store
    from lab.bench.recover import recover_suite
    from lab.bench.report import render_report

    store = default_trace_store()
    suite = asyncio.run(recover_suite(
        bench_root=ensure_runs_dir() / "bench",
        store=store,
        suite_id=args.suite_id,
        label=args.label,
        runs_per_task=args.runs,
        note=args.note or "",
        group=args.group,
    ))

    print(f"已重建评测: {suite.total_runs} 次运行，"
          f"{suite.successes} 成功（{suite.success_rate.point:.1%}，"
          f"95% 区间 [{suite.success_rate.low:.1%}, {suite.success_rate.high:.1%}]）")
    if args.report:
        out_path = _write_report(suite.suite_id[:8], render_report(suite))
        print(f"报告已写入: {out_path}")
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

    for item in inventory:
        shutil.rmtree(item["path"], ignore_errors=True)
        parent = Path(item["path"]).parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()  # 父目录是空的（只是逃逸时顺手建的），一起清掉
        except OSError:
            pass
        print(f"已删除 {item['path']}")
    return 0


def _cmd_bench_compare(args: argparse.Namespace) -> int:
    """`lab bench compare`：配对对比两个 suite（Step 5 的加固前后对比）。"""
    from lab.api import default_trace_store
    from lab.bench.compare import compare_suites, render_comparison

    store = default_trace_store()
    suites = store.list_suites(limit=200)
    if len(suites) < 2:
        print("[lab] 至少需要两个 suite 才能对比", file=sys.stderr)
        return 2

    resolved = [_resolve_suite_id(store, item) for item in (args.baseline, args.candidate)]
    if any(item is None for item in resolved):
        print(f"[lab] 无法唯一确定 suite：{args.baseline} / {args.candidate}", file=sys.stderr)
        return 2

    baseline = store.load_suite(resolved[0])
    candidate = store.load_suite(resolved[1])
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
    return 0


def _resolve_suite_id(store: Any, short: str) -> Optional[str]:
    """支持短 id（唯一匹配才解析，拿不准就不猜）。"""
    matches = [row["suite_id"] for row in store.list_suites(limit=200)
               if row["suite_id"].startswith(short) or short in (row["label"] or "")]
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
    bench_run.add_argument(
        "--budget-defaults", action="store_true",
        help="用固定的默认阈值（默认行为是按历史分位数 per-task 推导）",
    )
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

    bench_compare = bench_sub.add_parser(
        "compare", help="配对对比两个 suite（加固前后，按任务配对）"
    )
    bench_compare.add_argument("baseline", help="基准 suite（支持短 id 或 label 片段）")
    bench_compare.add_argument("candidate", help="候选 suite")
    bench_compare.add_argument("--out", default=None, help="写入文件而不是打印")
    bench_compare.set_defaults(func=_cmd_bench_compare)

    bench_pareto = bench_sub.add_parser(
        "pareto", help="成功率 vs 成本的 Pareto 表（文本；图在当前模型下不可用）"
    )
    bench_pareto.add_argument("--suite", action="append", default=None, help="只纳入指定 suite（可多次）")
    bench_pareto.add_argument("--min-runs", type=int, default=10,
                              help="忽略运行数少于此值的 suite（默认 10：n 太小会因运气而'支配'一切）")
    bench_pareto.set_defaults(func=_cmd_bench_pareto)

    bench_recover = bench_sub.add_parser(
        "recover", help="从工作区 + 轨迹库重建一次评测（评测被中断后救数据）"
    )
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

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
