#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""lab 的唯一对外入口：`run_task(goal) -> TaskResult`（handoff 决策 6）。

UI 只是壳，评测只看这个函数。它必须满足：
- **headless**：不依赖 FastAPI / Redis / PostgreSQL / Docker；
- **结构化返回**：失败也是数据（ok=False + error_type），不是异常；
- **可度量**：返回 token、成本、步数、耗时、工具序列；
- **可隔离**：每个任务一个独立 workspace，互不污染。

使用方式：
    from lab.api import run_task_sync
    result = run_task_sync("把 1 到 100 求和，写入 /home/ubuntu/result.txt")
    print(result.ok, result.answer)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import List, Optional

from lab.bootstrap import ensure_runs_dir, ensure_sut_on_path

ensure_sut_on_path()

from app.infrastructure.external.llm.openai_llm import OpenAILLM  # noqa: E402

from lab.config import load_agent_config, load_llm_config  # noqa: E402
from lab.faults.injector import FaultInjector  # noqa: E402
from lab.faults.kinds import FaultKind, FaultRule  # noqa: E402
from lab.guard.budget import Budget, BudgetPolicy  # noqa: E402
from lab.infra.counting_llm import CountingLLM  # noqa: E402
from lab.infra.local_sandbox import LocalSandbox  # noqa: E402
from lab.infra.nulls import NullBrowser, NullSearchEngine  # noqa: E402
from lab.middleware import ToolGuard  # noqa: E402
from lab.sut.base import TaskResult  # noqa: E402
from lab.sut.manus_adapter import ManusSUT  # noqa: E402
from lab.trace.span import TraceRecorder  # noqa: E402
from lab.trace.store import SpanStore  # noqa: E402
from lab.usage import Usage  # noqa: E402

# 评测默认的任务级时间上限（秒）。这是"终止保证"的第一道防线：
# SUT 内部只有 max_iterations（单次 invoke 的迭代上限），没有整任务的时间/成本上限。
DEFAULT_MAX_SECONDS = 300.0
# 轨迹数据库路径（Step 4 的 baseline 报告、跨运行统计都读它）
TRACE_DB_NAME = "traces.db"


def default_trace_store() -> SpanStore:
    """返回 lab 产物目录下的轨迹存储（不存在会自动建库建表）。"""
    return SpanStore(ensure_runs_dir() / TRACE_DB_NAME)


def fault_rules_from_spec(
        kinds: Optional[List[str]] = None,
        *,
        tool: str = "*",
        rate: float = 1.0,
        latency_s: float = 2.0,
) -> List[FaultRule]:
    """把命令行传的故障名转成注入规则（CLI / 临时实验用）。

    只做"每种故障各一条规则"这个最小形态。复杂的组合场景（按工具×按次数×�継发）
    直接在测试里构造 `FaultRule` 列表，不要把这个函数堆成配置语言。
    """
    rules: List[FaultRule] = []
    for raw in kinds or []:
        for name in str(raw).split(","):
            name = name.strip()
            if not name:
                continue
            rules.append(FaultRule(
                kind=FaultKind(name),
                tool=tool,
                rate=rate,
                latency_s=latency_s,
                # 这两种故障的语义是"前 N 次失败/前 N 次慢"，默认只影响第一次，
                # 让重试路径可以被确定性地验证
                fail_times=1 if FaultKind(name) == FaultKind.TRANSIENT_ERROR else None,
            ))
    return rules


class LabConfigError(RuntimeError):
    """lab 配置有问题（例如没配 API Key），属于"使用错误"，不是 SUT 缺陷。"""


async def run_task(
        goal: str,
        *,
        workspace: Optional[Path] = None,
        fast_mode: bool = True,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        max_iterations: Optional[int] = None,
        temperature: Optional[float] = None,
        exec_timeout: int = 60,
        attachments: Optional[List[str]] = None,
        trace: bool = True,
        fault_rules: Optional[List[FaultRule]] = None,
        budget_policy: Optional[BudgetPolicy] = None,
        watch_literals: Optional[List[str]] = None,
) -> TaskResult:
    """跑一个任务，返回标准化结果。

    :param goal: 自然语言任务目标
    :param workspace: 沙箱文件系统根目录；默认 `lab/runs/<task_id>/workspace`
    :param fast_mode: True 用 LocalSandbox（无 Docker，快，适合跑量）；
                      False 需要外部注入真实沙箱（Step 1 未覆盖）
    :param max_seconds: 任务级硬超时
    :param max_iterations: 覆盖 SUT 的单次 invoke 迭代上限（用于做"预算敏感性"实验）
    :param temperature: 覆盖温度；不传则用评测默认 0.0（可复现）
    :param exec_timeout: 单条 shell 命令的超时
    :param attachments: 随任务一起传入的附件（沙箱逻辑路径）
    :param trace: 是否采集轨迹（默认开；关掉可以测"纯执行"的最快速度）
    :param fault_rules: 故障注入规则（None = 不注入，即 baseline）
    :param budget_policy: 预算策略（None = 用环境变量/默认值，默认 observe 模式不干预）
    :param watch_literals: 需要盯的"答案字面量"（bench 层用来检测硬编码作弊）
    """
    # 1.准备配置（唯一真源是 SUT 的 config.yaml，环境变量可覆盖）
    llm_config = load_llm_config(temperature=temperature)
    agent_config = load_agent_config(max_iterations=max_iterations)

    # 2.预检：API Key 缺失是最常见的"跑不起来"原因，提前给出可操作的报错，
    #   而不是让 openai SDK 在深处抛一个含糊的 AuthenticationError。
    if not llm_config.api_key:
        raise LabConfigError(
            "未配置 LLM API Key。请任选一种方式：\n"
            "  1) 在 api/config.yaml 的 llm_config.api_key 中填写；\n"
            "  2) 设置环境变量 LAB_LLM_API_KEY（推荐，避免 key 进仓库）。"
        )

    # 3.准备任务工作区（一个任务一个目录，评测之间天然隔离）
    task_id = str(uuid.uuid4())
    if workspace is None:
        workspace = ensure_runs_dir() / task_id / "workspace"
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    if not fast_mode:
        raise LabConfigError(
            "fast_mode=False 需要注入真实 Docker 沙箱，Step 1 暂未提供入口。"
            "（接缝已经留好：把 DockerSandbox 实例传给 ManusSUT 即可）"
        )

    # 4.组装依赖（组成根）：这里的顺序**就是**依赖关系，一眼可读
    usage = Usage()
    recorder = TraceRecorder(task_id, goal=goal) if trace else None
    store = default_trace_store()
    sandbox = LocalSandbox(workspace, exec_timeout=exec_timeout)

    # 4.1 预算（Step 3 默认 observe：只记录不干预）+ 故障注入 + 工具中间件
    budget = Budget(
        usage,
        model_name=llm_config.model_name,
        policy=budget_policy or BudgetPolicy.from_env(),
    )
    injector = FaultInjector(fault_rules) if fault_rules else None
    guard = ToolGuard(
        recorder=recorder, budget=budget, injector=injector, sandbox=sandbox,
        watch_literals=watch_literals,
    )

    # 4.2 LLM 代理：采集用量 + **每次 LLM 调用后**触发预算检查。
    #     为什么要在 LLM 调用后也查：token/成本是在 LLM 调用时涨的，
    #     只在工具调用前查会漏掉"不停思考、不调工具"的失控路径。
    llm = CountingLLM(OpenAILLM(llm_config), usage, sink=recorder, after_call=guard.check_budget)

    sut = ManusSUT(
        llm=llm,
        agent_config=agent_config,
        sandbox=sandbox,
        workspace=workspace,
        fast_mode=fast_mode,
        browser=NullBrowser(),
        search_engine=NullSearchEngine(),
        usage=usage,
        max_seconds=max_seconds,
        trace=recorder,
        guard=guard,
    )

    # 5.执行并补齐计量字段
    started_at = time.monotonic()
    result: Optional[TaskResult] = None
    try:
        try:
            result = await sut.run(goal, attachments=attachments)
        except Exception as e:  # noqa: BLE001
            # harness 自己的兜底：即使 SUT 适配层出未预期异常，也要产出一个"失败的 TaskResult"
            # 而不是把异常抛给调用方 —— 否则一次运行会中断整个任务集的评测。
            result = TaskResult(
                task_id=task_id,
                sut_name=sut.name,
                ok=False,
                error_type="harness_error",
                error=f"SUT 适配层异常: {type(e).__name__}: {e}",
                workspace=str(workspace),
            )
        finally:
            await sut.aclose()
    finally:
        # ⚠️ 顺序极其重要：**先补齐计量字段，再落盘**。
        # 反过来的话会把 cost_usd=0 / 偏小的 elapsed_ms 写进数据库，
        # 而且因为是“看起来合理”的数字（0 不报错），你会在做基线报告时
        # 才发现成本列全是 0 —— 这是真实踩过的 bug，已由回归测试锁住。
        if result is None:
            # 极端情况（例如 KeyboardInterrupt）：也要留下 trace，方便事后排查
            result = TaskResult(
                task_id=task_id,
                sut_name=sut.name,
                ok=False,
                error_type="interrupted",
                error="运行被中断",
                workspace=str(workspace),
            )
        result.cost_usd = usage.cost_usd(llm_config.model_name)
        result.elapsed_ms = max(result.elapsed_ms, int((time.monotonic() - started_at) * 1000))

        # 轨迹落盘放在 finally 的最外层：崩溃/超时路径的 trace 才是最需要留下的证据
        if recorder is not None and recorder.spans:
            store.save_run(result, recorder.spans, goal=goal)
            result.trace_path = str(store.path)

    return result


def run_task_sync(goal: str, **kwargs) -> TaskResult:
    """同步封装：方便 CLI、pytest 和 CI 直接调用。"""
    return asyncio.run(run_task(goal, **kwargs))
