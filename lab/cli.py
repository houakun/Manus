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
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 被 pytest 捕获的流可能不支持 reconfigure；不能用就此放弃整个命令。
            pass


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
    print("")


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
    print(f"          等价成本 ${replay.get('equivalent_cost_usd', 0.0):.6f}"
          f"，**实际消费 ${replay.get('actual_spend_usd', 0.0):.6f}**"
          f"（缓存库 {replay.get('cache_path', '')}）")
    if replay.get("conflicts"):
        print(f"          ⚠️ 发现 {replay['conflicts']} 个请求**同一请求不同响应**"
              f"（服务端非确定性，温度 0 也消不掉）")


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
        print("（用 `python -m lab run \"...\" --replay record` 录一次就会出现）")
        return 2

    stats = LLMCache(path).stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    print("")
    print("=" * 72)
    print(f"缓存库   : {stats['path']}")
    print(f"条目数   : {stats['entries']}（被回放命中总计 {stats['served_total']} 次）")
    print(f"录制区间 : {stats['first_recorded_at']} → {stats['last_recorded_at']}")
    print(f"等价成本 : ${stats['equivalent_cost_usd']:.4f}"
          f"（录这些请求当时花了这么多）")
    print("=" * 72)
    if stats["entries"]:
        print(f"服务端非确定性：**{stats['conflict_keys']}/{stats['entries']}"
              f" = {stats['nondeterminism_rate']:.1%}** 的请求，同一个入参给过**不同回答**")
        print(f"  （共 {stats['conflict_responses']} 条不同的替代响应）")
        if stats["conflict_keys"] == 0:
            print("  → 已观测范围内服务端是确定的。注意：这只说明**已记录过的**请求是确定的。")
        else:
            print("  → 这部分差异**无法通过「固定温度」消除**。"
                  "它决定了 A/B 对比的噪声地板：小于它的 delta 不能当结论。")
    print("")
    if stats["by_model"]:
        print("| 模型 | 条目 | 输入 token | 输出 token | 等价成本 | 价格表命中 |")
        print("|---|---|---|---|---|---|")
        for row in stats["by_model"]:
            print(f"| `{row['model']}` | {row['entries']} | {row['prompt_tokens']:,} "
                  f"| {row['completion_tokens']:,} | ${row['equivalent_cost_usd']:.4f} "
                  f"| {'✓' if row['priced'] else '✗（价格未知，成本记 0）'} |")
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
        cost for cost in (store.avg_cost_for_group(name, faulted=faulted) for name in groups)
        if cost is not None
    ]
    if historical:
        per_run = max(historical)  # 多分组时取最贵的，偏保守
        basis = f"库中同组历史均值（{'/'.join(sorted(groups))}，{'带故障注入' if faulted else '无故障'}）"
    else:
        per_run = 0.15 if not faulted else 0.35
        basis = (f"保守默认值 ${per_run:.2f}/次（库里还没有此类历史；"
                 f"故障格实测约是基线的 2~3 倍）")

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
        arms.append(ArmConfig(
            label=label, guard_spec=spec, guard_label=config.label,
            guard_config=config, fault_rules=fault_rules, fault_spec=fault_spec,
            replay=args.replay,
        ))
    return arms


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
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t.key in wanted or t.uid in wanted]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("[lab] 没有找到任务", file=sys.stderr)
        return 2

    store = default_trace_store()

    # 故障规则先建出来：臂要用它，成本估算也要用它
    fault_rules = fault_rules_from_spec(
        args.fault, tool=args.fault_tool, rate=args.fault_rate, latency_s=args.fault_latency
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
        store, tasks, args.runs, arm_count,
        faulted=bool(args.fault), replay=args.replay, yes=args.yes,
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
        print(f"\n交错 A/B：{len(arms)} 个臂，按轮次交替运行"
              f"（{'、'.join(arm.describe() for arm in arms)}）")
        print("  自变量是加固配置，故障规则在所有臂之间**共享**。")
        if args.no_interleave:
            print("  ⚠️ --no-interleave：按臂分组顺序跑，**会重新引入时间混淆**。"
                  "仅在确认交错调度本身有问题时才用。")
        if len({arm.slug() for arm in arms}) != len(arms):
            print(f"[lab] 臂名必须互不相同（工作区按它分目录）："
                  f"{[a.label for a in arms]}", file=sys.stderr)
            return 2
    else:
        print(f"\n加固配置：{guard_config.describe()}")
        if guard_config.label != "all":
            print("  ⚠️ 注意：loop_guard / budget 在 observe 模式下只观测不干预，"
                  "真正影响成败的是 retry 与 postconditions。")

    # 故障规则必须**打印出来**：否则日志里看不见注入了什么，
    # 事后只能靠猜（实测踩过：一个脚本忘了传 --label，日志里连区分信息都没有）。
    if fault_rules:
        print("故障注入：" + "; ".join(rule.describe() for rule in fault_rules))

    if args.replay:
        print(f"LLM 回放模式：{args.replay}"
              + ("（严格离线，不需要 API Key）" if args.replay == "replay" else ""))

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
    ))

    print("")
    print(f"评测完成: {suite.successes}/{suite.total_runs} 成功"
          f"（{suite.success_rate.point:.1%}，95% 区间 [{suite.success_rate.low:.1%}, "
          f"{suite.success_rate.high:.1%}]），耗时 {suite.elapsed_s}s")
    if suite.is_multi_arm:
        # 交错跑完必须**马上**把两臂分开看：混在一起的成功率没有任何意义
        print("")
        print("分臂结果（交错跑出来的一个 suite 拆开看；不要当成两次独立评测）：")
        for arm_label in suite.arm_labels:
            sub = suite.for_arm(arm_label)
            rate = sub.success_rate
            print(f"  - {arm_label:28} {sub.successes}/{sub.total_runs} "
                  f"= {rate.point:.1%} [{rate.low:.1%}, {rate.high:.1%}]")
        print(f"\n  配对对比：python -m lab bench compare \"{suite.suite_id[:8]}\""
              f" --arm \"{suite.arm_labels[0]}\" --arm \"{suite.arm_labels[1]}\"")

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
        noise_floor_path,
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
        print("[lab] 没有任务可测", file=sys.stderr)
        return 2
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
        args.fault, tool=args.fault_tool, rate=args.fault_rate, latency_s=args.fault_latency
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

    print(f"\n噪声地板实验：**同一配置复制成两个臂**交错跑（这是关键，不是分两段跑）")
    print(f"  加固配置：{guard_config.describe()}")
    if fault_rules:
        print(f"  故障注入：{fault_spec}")
    print(f"  任务集  ：{len(tasks)} 个（purpose={args.purpose}）")

    store = default_trace_store()
    proceed, _ = _confirm_cost(
        store, tasks, args.runs, 2,
        faulted=bool(args.fault), replay=args.replay, yes=args.yes,
    )
    if not proceed:
        return 2

    def _arm(label: str) -> ArmConfig:
        return ArmConfig(
            label=label, guard_spec=args.guard or "", guard_label=guard_config.label,
            guard_config=guard_config, fault_rules=fault_rules, fault_spec=fault_spec,
            replay=args.replay,
        )

    arms = [_arm(args.label_a), _arm(args.label_b)]
    suite = asyncio.run(run_suite(
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
    ))

    floor = measure_noise_floor(
        suite,
        label=args.label or "噪声地板（同配置双臂交错）",
        include_uids=include_uids if (args.purpose and args.purpose != "all") else None,
    )
    path = save_noise_floor(floor, Path(args.file) if args.file else None)

    print("")
    print(render_noise_floor(floor))
    print(f"已写入：{path}")
    print("（`bench gate` 会自动读它，把「测量分辨率」附在每个门禁旁边；"
          "加 --auto-thresholds 则直接把阈值抬到地板之上）")
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
    # 硬冲突（模型/温度/回放/任务集不同）用非 0 退出码表达：
    # 它能安静地跟 CI 里，防止"拿两个不同实验比出的 delta"被当成回归。
    return 3 if report.hard_confounds else 0


def _arm_slug(name: str) -> str:
    from lab.bench.runner import arm_slug

    return arm_slug(name)


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
        others = [row["suite_id"] for row in store.list_suites(limit=200)
                  if row["suite_id"] != baseline_id]
        candidate_id = others[0] if others else None
    if candidate_id is None:
        print("[lab] 无法确定候选 suite", file=sys.stderr)
        return 2

    baseline, candidate = store.load_suite(baseline_id), store.load_suite(candidate_id)
    if baseline is None or candidate is None:
        print("[lab] 加载 suite 失败", file=sys.stderr)
        return 2
    if len(candidate.outcomes) < args.min_runs:
        print(f"[lab] 候选 suite 只有 {len(candidate.outcomes)} 次运行"
              f"（< --min-runs {args.min_runs}）—— 样本太小，不参与门禁", file=sys.stderr)
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
    print("")

    # 实验条件不同 → 这不是"代码回归"，而可能是"拿两个不同实验在比"。
    # 不阻断（加固对照实验本来就靠这个），但必须最显眼。
    if report.hard_confounds:
        print(f"🔴 **实验条件不同，对比失去意义**：{report.hard_confounds}")
        print("   → 下面的门禁结论不可用。先让两边的模型/温度/回放模式/任务集一致。")
        print("")
    elif report.config_differences:
        print(f"🟡 实验条件差异（确认是有意设计的自变量）：{report.config_differences}")
        print("")

    if floor is not None:
        print(render_noise_floor(floor))
        if thresholds_from:
            print("> ✅ `--auto-thresholds`：阈值已抬到不小于噪声地板。")
        else:
            too_tight = []
            for name, threshold, unit in (
                    ("success_pt", args.max_success_drop, "pt"),
                    ("cost_pct", args.max_cost_increase, "%")):
                metric = floor.get(name)
                if metric and metric.resolution > threshold:
                    too_tight.append(f"{metric.label}(阈值 {threshold:.1f}{unit} < 地板 ±{metric.resolution:.2f}{unit})")
            if too_tight:
                print(f"> ⚠️ **阈值比噪声地板还紧**：{'；'.join(too_tight)}")
                print(">    这种门禁会因为噪声频繁变红 —— 而**频繁变红的门禁等于没有门禁**。")
                print(">    要么加 `--auto-thresholds`，要么先加任务数/次数把地板压下来。")
                print("")
    else:
        print("> ⚠️ **没测过噪声地板** → 无法判断下面的阈值是否比噪声还紧。")
        print(">    建议先跑一次：`python -m lab bench noise-floor --runs 3 --group semireal`")
        print("")

    print(render_gates(results))
    print("")

    failed = [item for item in results if not item.passed]
    if failed:
        print(f"❌ 门禁未通过（{len(failed)}/{len(results)} 项）：")
        for item in failed:
            print(f"   - {item.name}: {item.observed}（阈值 {item.threshold}）")
        return 1
    print(f"✅ 全部 {len(results)} 项门禁通过")
    print(">（注意：这不等于「没有退化」—— 它只说明本次差值没有超过阈值。"
          + ("）" if floor is not None else
             "而**噪声地板未测**，所以小于阈值的变化看不出来。）"))
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
    run_parser.add_argument(
        "--replay", default=None, choices=["off", "record", "reuse", "replay"],
        help="LLM 录制回放：off=不用缓存；record=总联网并记录（顺带测服务端非确定性）；"
             "reuse=命中即用/未命中联网（改了 prompt 只想重跑受影响的那部分）；"
             "replay=**严格离线**（未命中就失败，不需要 API Key）",
    )
    run_parser.add_argument("--replay-cache", default=None, help="缓存库路径（默认 lab/runs/llm_cache.db）")
    run_parser.add_argument(
        "--replay-ignore-volatile", action="store_true",
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
    replay_stats = replay_sub.add_parser(
        "stats", help="看缓存内容 + **同一请求不同响应**的比例（服务端非确定性）"
    )
    replay_stats.add_argument("--cache", default=None, help="缓存库路径（默认 lab/runs/llm_cache.db）")
    replay_stats.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    replay_stats.set_defaults(func=_cmd_replay_stats)

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
    bench_run.add_argument(
        "--guard", default=None,
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
    bench_run.add_argument("--fault-latency", type=float, default=2.0,
                           help="latency_spike 注入的额外延迟（秒）")
    # ---- 交错 A/B（分离时间混淆的唯一办法）----
    # 为什么用可重复的 --ab-guard 而不是新建一个 `bench ab` 命令：
    # 交错必须落在**同一个 suite** 里才能共享同一段时间，
    # 而 suite 的构造（任务集/预算/落库/报告）已经全部现成 ——
    # 另开一条命令只会把这些逻辑复制一遍，然后慢慢跑偏。
    bench_run.add_argument(
        "--ab-guard", action="append", default=None, metavar="SPEC",
        help="交错 A/B 的加固臂（可重复 2 次以上）。例："
             "--ab-guard none --ab-guard retry,postcondition,enforce。"
             "传了就按轮次交替跑（A,B,B,A,…），以消除「两组跑在不同时段」的时间混淆。"
             "`--runs N` 的含义变成**每个臂各 N 次**。传两个完全相同的 spec 就是**噪声地板实验**。",
    )
    bench_run.add_argument(
        "--ab-label", action="append", default=None, metavar="NAME",
        help="给对应的 --ab-guard 臂起名（顺序对应；不传则用 guard label）。"
             "噪声地板实验必须用它区分两个同配置的臂。",
    )
    bench_run.add_argument(
        "--no-interleave", action="store_true",
        help="多臂时不做交错（按臂分组顺序跑）—— **仅供对照**，会重新引入时间混淆",
    )
    bench_run.add_argument(
        "--replay", default=None, choices=["off", "record", "reuse", "replay"],
        help="LLM 录制回放模式（见 `lab run --help`）；replay 模式不花钱、不需要 API Key",
    )
    bench_run.add_argument("--replay-cache", default=None, help="缓存库路径")
    bench_run.add_argument(
        "--replay-ignore-volatile", action="store_true",
        help="算键前抹平 UUID / 时间戳（见 `lab run --help`）；"
             "解决「SUT 提示词里带随机 id」导致的必然未命中",
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

    bench_compare = bench_sub.add_parser(
        "compare", help="配对对比两个 suite（加固前后，按任务配对）"
    )
    bench_compare.add_argument("baseline", help="基准 suite（支持短 id 或 label 片段）")
    bench_compare.add_argument(
        "candidate", nargs="?", default=None,
        help="候选 suite（用 --arm 拆臂对比时不需要）",
    )
    bench_compare.add_argument(
        "--arm", action="append", default=None, metavar="NAME",
        help="从**同一个交错 suite** 里拆两个臂来对比（恰好传两次）。"
             "例：--arm \"guard=none\" --arm \"guard=all\"",
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
        "--task", action="append", default=None, metavar="KEY",
        help="只测指定任务（可多次）。用于**定向重测**主导项：全套 ~$5，单任务 ~$0.9",
    )
    bench_noise.add_argument(
        "--purpose", default="regression", choices=["capability", "regression", "all"],
        help="默认只用 regression 任务：capability 任务的波动是「能力边界」（能力问题），"
             "把它算进测量分辨率会让地板大到没有意义",
    )
    bench_noise.add_argument("--guard", default=None, help="要在哪个加固配置下测地板（默认 all）")
    bench_noise.add_argument("--fault", action="append", default=None)
    bench_noise.add_argument("--fault-tool", default="*")
    bench_noise.add_argument("--fault-rate", type=float, default=1.0)
    bench_noise.add_argument("--fault-latency", type=float, default=2.0)
    bench_noise.add_argument("--label", default="", help="给本次测量起名")
    bench_noise.add_argument("--label-a", default="noise-a", help="第一个臂的名字")
    bench_noise.add_argument("--label-b", default="noise-b", help="第二个臂的名字")
    bench_noise.add_argument(
        "--from-suite", default=None, metavar="SUITE_ID|LABEL",
        help="**不重跑、$0**：从已有的交错 suite 重算地板（修正分析口径时用）",
    )
    bench_noise.add_argument("--file", default=None, help="地板文件路径（默认 lab/runs/noise_floor.json）")
    bench_noise.add_argument("--concurrency", type=int, default=1)
    bench_noise.add_argument("--temperature", type=float, default=None)
    bench_noise.add_argument("--replay", default=None, choices=["off", "record", "reuse", "replay"])
    bench_noise.add_argument("--replay-cache", default=None, help="缓存库路径")
    bench_noise.add_argument(
        "--replay-ignore-volatile", action="store_true",
        help="算键前抹平 UUID / 时间戳（解决「SUT 提示词带随机 id」导致的必然未命中）",
    )
    bench_noise.add_argument("--yes", action="store_true", help="确认可能较高的成本")
    bench_noise.set_defaults(func=_cmd_bench_noise_floor)

    bench_pareto = bench_sub.add_parser(
        "pareto", help="成功率 vs 成本的 Pareto 表（文本；图在当前模型下不可用）"
    )
    bench_pareto.add_argument("--suite", action="append", default=None, help="只纳入指定 suite（可多次）")
    bench_pareto.add_argument("--min-runs", type=int, default=10,
                              help="忽略运行数少于此值的 suite（默认 10：n 太小会因运气而'支配'一切）")
    bench_pareto.set_defaults(func=_cmd_bench_pareto)

    bench_gate = bench_sub.add_parser(
        "gate", help="回归门禁：delta 超阈值 → 退出码非 0（可用于 CI）"
    )
    bench_gate.add_argument("--baseline", required=True, help="基准 suite（短 id 或 label 片段）")
    bench_gate.add_argument("--candidate", default=None, help="候选 suite（默认取最近一个）")
    bench_gate.add_argument("--max-success-drop", type=float, default=5.0, help="运行级成功率最多下降多少百分点")
    bench_gate.add_argument("--max-passk-drop", type=float, default=10.0, help="pass^k 最多下降多少百分点")
    bench_gate.add_argument("--max-cost-increase", type=float, default=20.0, help="成本最多上升百分之多少")
    bench_gate.add_argument("--min-runs", type=int, default=10, help="候选 suite 少于这么多次运行则不判")
    bench_gate.add_argument(
        "--allow-noisy", action="store_true",
        help="即使差值方向不可信（区间跨 0）也按阈值判定；默认不这样（否则噪声会频繁弄红 CI）",
    )
    bench_gate.add_argument(
        "--noise-floor", default="auto", metavar="PATH|auto|none",
        help="噪声地板文件（默认 auto：读 lab/runs/noise_floor.json，没有就按未测处理）。"
             "读了它，每个门禁旁会附上实测分辨率；没测过会显式提醒",
    )
    bench_gate.add_argument(
        "--auto-thresholds", action="store_true",
        help="把阈值抬到「不小于噪声地板」。默认不抬（只报告），因为抬阈值会放过小退化，"
             "这个取舍应该由人显式做",
    )
    bench_gate.set_defaults(func=_cmd_bench_gate)

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
    # 必须在 argparse **之前**：--help 与参数错误也是打印输出，同样会碰到 GBK 问题。
    _fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
