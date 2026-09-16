#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ManusSUT：把 SUT（mooc-manus）的 PlannerReActFlow 包装成 lab 契约。

== 核心思路：依赖注入，而不是改代码 ==
PlannerReActFlow 的构造函数接收 uow_factory / llm / browser / sandbox / search_engine /
mcp_tool / a2a_tool —— **全部是 Protocol 接口**。于是 lab 只需要提供这些依赖的替代实现
（内存 UoW、本地沙箱、空浏览器……），就能让同一份 flow 代码在完全不同的环境下运行。

这条性质带来三个直接好处：
1. SUT 零侵入（本 Step 只改了 4 个 bug + 1 个可选工具注入点）；
2. 同一个 adapter 既能跑 fast mode（LocalSandbox），也能跑真沙箱（DockerSandbox）
   → Step 4 可以做"环境差异对照"实验；
3. Step 4 要写的自研 loop 只要实现同样的 `run(goal) -> TaskResult`，
   就能挂到同一套 harness 上和老项目做 A/B —— 两边共享同一把尺子。

== 不变量 ==
每个 ManusSUT 实例 = 一次任务 = 一个独立内存会话 + 一个独立 workspace。
任务之间不共享任何状态，避免评测串味。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import List, Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.event import (  # noqa: E402
    ErrorEvent,
    MessageEvent,
    PlanEvent,
    StepEvent,
    StepEventStatus,
    TitleEvent,
    ToolEvent,
    ToolEventStatus,
    WaitEvent,
)
from app.domain.models.message import Message  # noqa: E402
from app.domain.models.plan import ExecutionStatus, Plan  # noqa: E402
from app.domain.models.session import Session  # noqa: E402
from app.domain.services.flows.planner_react import PlannerReActFlow  # noqa: E402
from app.domain.services.tools.a2a import A2ATool  # noqa: E402
from app.domain.services.tools.base import BaseTool  # noqa: E402
from app.domain.services.tools.file import FileTool  # noqa: E402
from app.domain.services.tools.mcp import MCPTool  # noqa: E402
from app.domain.services.tools.message import MessageTool  # noqa: E402
from app.domain.services.tools.shell import ShellTool  # noqa: E402
from app.infrastructure.external.json_parser.repair_json_parser import RepairJSONParser  # noqa: E402

from lab.infra.memory_uow import create_uow_factory  # noqa: E402
from lab.middleware import ToolGuard  # noqa: E402
from lab.sut.base import SUT, StepStats, TaskResult  # noqa: E402
from lab.trace.span import TraceRecorder  # noqa: E402
from lab.usage import Usage  # noqa: E402


class ManusSUT(SUT):
    """把 SUT 的 PlannerReActFlow 适配成 lab 可测量的被测系统。"""

    def __init__(
            self,
            *,
            llm,
            agent_config,
            sandbox,
            workspace: Path,
            fast_mode: bool = True,
            browser=None,
            search_engine=None,
            usage: Optional[Usage] = None,
            max_seconds: float = 300.0,
            trace: Optional[TraceRecorder] = None,
            guard: Optional[ToolGuard] = None,
    ) -> None:
        """构造函数：所有依赖都由外部注入，adapter 自己不 new 任何具体实现。"""
        self._llm = llm
        self._agent_config = agent_config
        self._sandbox = sandbox
        self._workspace = Path(workspace)
        self._fast_mode = fast_mode
        self._browser = browser
        self._search_engine = search_engine
        self._usage = usage or Usage()
        self._max_seconds = max_seconds
        # 名字里带模式标记：A/B 报告里一眼能看出这条曲线来自哪种配置
        self.name = f"manus-planner-react[{'fast' if fast_mode else 'full'}]"

        # 轨迹记录器：不传则建一个本地临时的，保证后续代码路径统一（不用到处判空）
        self._trace = trace or TraceRecorder("local-untracked")
        self._trace.label(sut_name=self.name)
        # 用 step.id / tool_call_id 关联"开始"与"结束"事件：
        # 事件流并不保证严格 FIFO 成对，用 id 关联才能知道该关哪个 span。
        self._step_handles: dict = {}
        self._tool_handles: dict = {}
        self._plan_revisions = 0

        # 工具层中间件（Step 3）：不传就现建一个 observe 模式的，
        # 保证"预算/循环"至少是被观测的（不传就啥都看不见，很容易漏掉问题）。
        self._guard = guard or ToolGuard(recorder=self._trace, sandbox=self._sandbox)

    # ==================== 工具集构造 ====================

    def _build_tools(self) -> Optional[List[BaseTool]]:
        """构造注入给 flow 的工具集。

        fast mode 刻意只保留 file / shell / message 三类：
        - file + shell 覆盖计算、文件 IO、文本处理（Step 1/4 的确定性任务所需的全部能力）；
        - message 负责"向用户播报进度/提问"，缺了它 Agent 的交互协议就不完整；
        - browser / search / mcp / a2a 全部去掉 —— 不只是"让它失败"，而是
          从 LLM 的工具清单里彻底移除（工具 schema 会进 prompt，
          留着用不了的工具会诱导模型反复尝试，白烧迭代次数和 token）。

        返回 None 表示"用 SUT 默认的全量工具集"，此时必须注入真实沙箱与浏览器。
        """
        if not self._fast_mode:
            return None
        tools = [
            FileTool(sandbox=self._sandbox),
            ShellTool(sandbox=self._sandbox),
            MessageTool(),
        ]
        # Step 3：整列表包一层 GuardedTool 代理。
        # SUT 对工具只用 .name / .get_tools() / .has_tool() / .invoke() 四个成员，
        # 所以代理这四个就够了 —— 这就是"零 SUT 改动接入中间件"的全部代价。
        return self._guard.wrap_tools(tools)

    # ==================== 主流程 ====================

    async def run(
            self,
            goal: str,
            attachments: Optional[List[str]] = None,
    ) -> TaskResult:
        """执行一个任务，把 SUT 的事件流汇总成 TaskResult。"""
        # task_id 以 trace 为准，保证"span 表里的 task_id"与"任务表里的 task_id"一致。
        # （Step 1 的教训：两处各自生成 uuid 会让 trace 和结果对不上号。）
        task_id = self._trace.task_id
        started_at = time.monotonic()
        self._trace.label(goal=goal)

        # 1.建立内存会话。flow.invoke() 的第一件事就是 get_by_id，
        #   拿不到会话会直接抛 "会话不存在"，所以必须先建好。
        uow_factory = create_uow_factory()
        session_id = str(uuid.uuid4())
        uow = uow_factory()
        async with uow:
            await uow.session.save(Session(id=session_id))

        # 2.组装 flow：把 lab 版本的依赖全部注入进去
        flow = PlannerReActFlow(
            uow_factory=uow_factory,
            llm=self._llm,
            agent_config=self._agent_config,
            session_id=session_id,
            json_parser=RepairJSONParser(),
            browser=self._browser,
            sandbox=self._sandbox,
            search_engine=self._search_engine,
            mcp_tool=MCPTool(),
            a2a_tool=A2ATool(),
            tools=self._build_tools(),
        )

        # 3.消费事件流并汇总
        answer = ""
        plan: Optional[Plan] = None
        tool_sequence: List[str] = []
        delivered_files: List[str] = []
        error: Optional[str] = None
        error_type: Optional[str] = None
        error_chain: List[str] = []
        ok = True

        try:
            async for event in flow.invoke(Message(message=goal, attachments=list(attachments or []))):
                # 3.1 任务级超时：SUT 内部没有任何"整任务"的时间上限（体检第 6 项的缺口，
                #     max_iterations 只约束单次 invoke），所以 lab 必须自己守这道门，
                #     否则一个死循环任务会挂住整个评测。
                if time.monotonic() - started_at > self._max_seconds:
                    ok, error_type = False, "task_timeout"
                    error = f"任务超过最大执行时长 {self._max_seconds}s，已中断"
                    break

                if isinstance(event, TitleEvent):
                    self._trace.task_span.attrs["title"] = event.title
                elif isinstance(event, PlanEvent):
                    plan = event.plan
                    self._plan_revisions += 1
                elif isinstance(event, StepEvent):
                    # 把 step 事件映射成 span（压栈/出栈由 recorder 负责，
                    # 于是该步内部的 LLM 调用与工具调用会自动挂到它下面）
                    self._on_step_event(event)
                elif isinstance(event, MessageEvent):
                    if event.role == "assistant" and event.message:
                        answer = event.message
                    # 汇总阶段交付的附件（File 对象）
                    for file in event.attachments or []:
                        if file.filepath and file.filepath not in delivered_files:
                            delivered_files.append(file.filepath)
                elif isinstance(event, ToolEvent):
                    self._on_tool_event(event)
                    # 只统计 CALLING，避免 CALLED 被重复计数
                    if event.status == ToolEventStatus.CALLING:
                        tool_sequence.append(event.function_name)
                        self._usage.tool_calls += 1
                elif isinstance(event, WaitEvent):
                    # "等待人类输入"在 headless 场景下是**合法终止态**，不是崩溃：
                    # 评测里它意味着"这一步 Agent 无法自主完成"，应单独归因。
                    ok, error_type = False, "wait_user_input"
                    error = "Agent 请求人类输入，headless 模式无法继续"
                    break
                elif isinstance(event, ErrorEvent):
                    # 关键：只记录**首个**错误作为根因。
                    # SUT 的错误是级联的：LLM 挂了 → 计划为空 → 任务终止。
                    # 如果每个 ErrorEvent 都覆盖 error，最终拿到的会是"任务终止"这种
                    # 毫无信息量的末端描述，根因被藏起来了（实测踩过）。
                    error_chain.append(event.error)
                    if error is None:
                        ok, error_type = False, "agent_error"
                        error = event.error
                    # 不 break：flow 自己会走完 COMPLETED 分支并关闭
        except Exception as e:  # noqa: BLE001
            # SUT 崩溃不能把整个评测台带走 —— harness 必须能"记录失败并继续跑下一个任务"。
            # 这类异常应被视为 SUT 的缺陷（high severity），Step 2 会连同轨迹一起落盘。
            ok, error_type = False, "sut_crash"
            error = f"SUT 运行时异常: {type(e).__name__}: {e}"
            error_chain.append(error)

        # 4.汇总计划步骤统计与附件
        if plan is not None:
            for step in plan.steps:
                for path in step.attachments or []:
                    if path and path not in delivered_files:
                        delivered_files.append(path)

        # 5.收口 trace：把未关闭的 span（超时/break/异常路径残留）标成 error。
        #   恰恰是这些路径最需要看清楚 —— 崩溃时的 trace 是唯一的一手材料。
        steps = self._step_stats(plan)
        self._trace.finish_task(
            ok=ok,
            error=error,
            error_type=error_type,
            steps=f"{steps.succeeded}/{steps.total}",
            plan_revisions=self._plan_revisions,
            tool_calls=len(tool_sequence),
            delivered_files=len(delivered_files),
            # 记录错误链长度：级联很深（一次失败爆出好几条错误）本身就是个信号，
            # 说明 SUT 的失败处理把同一个根因反复包装了
            error_chain_len=len(error_chain),
            # 加固层观察汇总（预算/循环/重试/后置校验）
            guard_summary=len(self._guard.postcondition_warnings),
        )

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        return TaskResult(
            task_id=task_id,
            sut_name=self.name,
            ok=ok,
            answer=answer,
            attachments=delivered_files,
            error=error,
            error_type=error_type,
            error_chain=error_chain,
            plan_title=plan.title if plan else "",
            plan_goal=plan.goal if plan else "",
            steps=steps,
            tool_sequence=tool_sequence,
            llm_usage=self._usage,
            elapsed_ms=elapsed_ms,
            workspace=str(self._workspace),
            guard=self._guard.report(),
        )

    # ==================== 事件 → span 的映射 ====================

    def _on_step_event(self, event: StepEvent) -> None:
        """把 StepEvent 映射成 step span。

        为什么用字典存句柄，而不是"收到 STARTED 就压栈、收到 COMPLETED 就弹栈"：
        STARTED/COMPLETED 是按 step.id 配对的，但异常路径下可能收到未成对的结束事件
        （或根本收不到）。用 id 关联才能做到"谁开始谁结束"，不会把嵌套关系弄乱。
        """
        step = event.step
        if event.status == StepEventStatus.STARTED:
            self._step_handles[step.id] = self._trace.open_step(step.description, step.id)
            # 通知中间件"进入新步骤"：预算里的步数 +1，循环检测重置连续计数
            self._guard.note_step()
            return

        handle = self._step_handles.pop(step.id, None)
        if handle is None:  # 防御性：不要因为异常事件顺序就把 trace 搞崩
            return
        failed = event.status == StepEventStatus.FAILED
        handle.end(
            status="error" if failed else "ok",
            error=step.error,
            error_type="step_failed" if failed else None,
            success=bool(step.success),
            result_chars=len(step.result or ""),
            attachments=len(step.attachments or []),
        )

    def _on_tool_event(self, event: ToolEvent) -> None:
        """把 ToolEvent 映射成 tool span，并记录失败类型与重试次数。

        记 attempts 的意义：D2 类问题（非幂等工具被重试导致重复副作用）
        在没有这个字段时是**看不见的** —— 只能看到"最后成功了"。
        有了 attempts，一次 write_file 显示 attempts=2 就能立刻引起怀疑。
        """
        if event.status == ToolEventStatus.CALLING:
            self._tool_handles[event.tool_call_id] = self._trace.open_tool(
                event.function_name, event.tool_name, event.function_args
            )
            return

        handle = self._tool_handles.pop(event.tool_call_id, None)
        if handle is None:
            return

        result = event.function_result
        succeeded = bool(getattr(result, "success", False))
        handle.end(
            status="ok" if succeeded else "error",
            # 只在失败时填 error，避免成功路径被无意义的 message 噪声淹没
            error=(getattr(result, "message", None) if not succeeded else None),
            error_type=getattr(result, "error_type", None),
            success=succeeded,
            attempts=getattr(result, "attempts", 1),
            retryable=getattr(result, "retryable", False),
            result_chars=len(result.model_dump_json()) if hasattr(result, "model_dump_json") else 0,
        )

    @staticmethod
    def _step_stats(plan: Optional[Plan]) -> StepStats:
        """从计划里汇总步骤统计。"""
        if plan is None:
            return StepStats()
        steps = plan.steps
        return StepStats(
            total=len(steps),
            done=sum(1 for s in steps if s.done),
            succeeded=sum(1 for s in steps if s.success),
            failed=sum(1 for s in steps if s.status == ExecutionStatus.FAILED),
        )

    async def aclose(self) -> None:
        """释放沙箱资源（保留 workspace 里的产物）。"""
        try:
            await self._sandbox.destroy()
        except Exception:  # noqa: BLE001
            pass
