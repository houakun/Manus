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

from lab.api import LabConfigError, default_trace_store, run_task_sync
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
    run_parser.set_defaults(func=_cmd_run)

    trace_parser = sub.add_parser("trace", help="查看一次运行的轨迹树")
    trace_parser.add_argument("task_id", nargs="?", default=None, help="任务 id（默认取最近一次，支持短 id 前缀）")
    trace_parser.add_argument("--top", type=int, default=5, help="汇总里展示最慢的前 N 个操作")
    trace_parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    trace_parser.set_defaults(func=_cmd_trace)

    stats_parser = sub.add_parser("stats", help="跨运行聚合指标")
    stats_parser.add_argument("--sut", default=None, help="按 SUT 名字过滤（如 manus-planner-react[fast]）")
    stats_parser.set_defaults(func=_cmd_stats)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
