#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/16 3:16
@Author  : thezehui@gmail.com
@File    : planner_react.py
"""
import logging
from typing import AsyncGenerator, Optional, Callable, List

from app.domain.external.browser import Browser
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.models.app_config import AgentConfig
from app.domain.models.event import BaseEvent, ErrorEvent, PlanEvent, PlanEventStatus, TitleEvent, MessageEvent
from app.domain.models.event import DoneEvent
from app.domain.models.message import Message
from app.domain.models.plan import Plan, ExecutionStatus
from app.domain.models.session import SessionStatus
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.react import ReActAgent
from app.domain.services.tools.a2a import A2ATool
from app.domain.services.tools.base import BaseTool
from app.domain.services.tools.browser import BrowserTool
from app.domain.services.tools.file import FileTool
from app.domain.services.tools.mcp import MCPTool
from app.domain.services.tools.message import MessageTool
from app.domain.services.tools.search import SearchTool
from app.domain.services.tools.shell import ShellTool
from .base import BaseFlow, FlowStatus
from ...repositories.uow import IUnitOfWork

logger = logging.getLogger(__name__)


class PlannerReActFlow(BaseFlow):
    """规划与执行流"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],  # uow模块
            llm: LLM,  # 大语言模型
            agent_config: AgentConfig,  # 智能体配置
            session_id: str,  # 会话id
            json_parser: JSONParser,  # JSON解析器
            browser: Browser,  # 浏览器
            sandbox: Sandbox,  # 沙箱
            search_engine: SearchEngine,  # 搜索引擎
            mcp_tool: MCPTool,  # mcp工具
            a2a_tool: A2ATool,  # a2a远程agent
            tools: Optional[List[BaseTool]] = None,  # [lab] 可选：注入自定义工具集
    ) -> None:
        """构造函数，完成规划与执行流的初始化"""
        # 1.流初始化数据配置
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        self._session_id = session_id
        self.status = FlowStatus.IDLE
        self.plan: Optional[Plan] = None

        # 2.初始化Agent预设工具列表
        #   [lab/S-tools] 新增 tools 注入参数（可选，默认 None）：
        #     - 传 None 时行为与改造前【完全一致】→ 向后兼容，不影响现有 UI/线上链路；
        #     - lab 的 fast mode 会传入"仅 file + shell + message"的精简工具集，
        #       把 browser/search/mcp/a2a 从 LLM 的工具清单里去掉。
        #   为什么必须能"去掉"而不只是"让它们失败"：工具 schema 是要进 prompt 的，
        #   留着用不了的工具会诱导模型反复尝试 → 浪费迭代次数、污染评测结果。
        if tools is None:
            tools = [
                FileTool(sandbox=sandbox),
                ShellTool(sandbox=sandbox),
                BrowserTool(browser=browser),
                SearchTool(search_engine=search_engine),
                MessageTool(),
                mcp_tool,
                a2a_tool,
            ]

        # 3.创建规划Agent
        self.planner = PlannerAgent(
            uow_factory=uow_factory,
            session_id=session_id,
            agent_config=agent_config,
            llm=llm,
            json_parser=json_parser,
            tools=tools,
        )
        logger.debug(f"创建规划Agent成功, 会话id: {self._session_id}")

        # 4.创建执行Agent
        self.react = ReActAgent(
            uow_factory=uow_factory,
            session_id=session_id,
            agent_config=agent_config,
            llm=llm,
            json_parser=json_parser,
            tools=tools,
        )
        logger.debug(f"创建执行Agent成功, 会话id: {self._session_id}")

    async def invoke(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """传递消息，运行流，在六中调用planner&react智能体组合完成任务并返回对应事件"""
        # 1.调用会话仓库查询会话是否存在
        async with self._uow:
            session = await self._uow.session.get_by_id(self._session_id)
        if not session:
            raise ValueError(f"会话[{self._session_id}]不存在, 请核实后尝试")

        # 2.判断会话的状态是不是空闲
        #   如果不是则有可能有两种状态
        #    - 任务未结束，还在运行，但是用户又传递一条消息
        #    - Agent在等待人类输入，这时候人类输入了
        #   这时候均需要处理历史消息列表，避免AI(工具调用消息)后直接接上人类消息
        if session.status != SessionStatus.PENDING:
            logger.debug(f"会话[{self._session_id}]未处于空闲状态，回滚数据确保消息列表格式正确")
            await self.planner.roll_back(message)
            await self.react.roll_back(message)

        # 3.如果会话状态等于运行中，则流需要重新规划内容/plan
        if session.status == SessionStatus.RUNNING:
            logger.debug(f"会话[{self._session_id}]处于运行状态并传递了新消息")
            self.status = FlowStatus.PLANNING

        # 4.如果会话状态等于等待人类输入，则需要修改流的状态为执行中
        if session.status == SessionStatus.WAITING:
            logger.debug(f"会话[{self._session_id}]处于等待状态并传递了新消息")
            self.status = FlowStatus.EXECUTING

        # 5.更新会话状态为运行中
        async with self._uow:
            await self._uow.session.update_status(self._session_id, SessionStatus.RUNNING)

        # 6.获取当前会话中最新事件
        self.plan = session.get_latest_plan()
        logger.info(f"Planner&ReAct流接收消息: {message.message[:50]}...")

        # 7.定义当前正在执行的子步骤
        step = None

        # 8.创建死循环执行任务，根据流的不同状态执行不同的操作
        while True:
            # 9.如果流的状态为空闲，则只需要将状态修改为规划中
            if self.status == FlowStatus.IDLE:
                logger.info(f"Planner&ReAct流状态从{FlowStatus.IDLE}变成{FlowStatus.PLANNING}")
                self.status = FlowStatus.PLANNING
            elif self.status == FlowStatus.PLANNING:
                # 10.流状态为规划中，则调用规划Agent
                logger.info(f"Planner&ReAct流开始创建计划/Plan")
                async for event in self.planner.create_plan(message):
                    # 11.判断规划Agent是否返回规划事件
                    if isinstance(event, PlanEvent) and event.status == PlanEventStatus.CREATED:
                        # 12.创建计划成功时需要更新计划
                        self.plan = event.plan
                        logger.info(f"Planner&ReAct流成功创建计划, 共计: {len(event.plan.steps)} 步")

                        # 13.在计划中同步生成了会话标题+初始AI消息
                        yield TitleEvent(title=event.plan.title)
                        yield MessageEvent(role="assistant", message=event.plan.message)

                    # 14.将生成的事件直接输出(一般来说是PlanEvent)
                    yield event

                # 15.计划创建完成，更新流状态为执行中
                logger.info(f"Planner&ReAct流状态从{FlowStatus.PLANNING}变成{FlowStatus.EXECUTING}")
                self.status = FlowStatus.EXECUTING

                # 16.判断计划是否生成，步骤是否正常
                # [lab/D9-fix] 这里原来是 `if not self.plan or len(self.plan.steps) == 0:`
                #   然后直接把状态置成 COMPLETED —— 结果是：**计划为空也被当成"任务完成"**。
                #   实测：某任务 PlannerAgent 产出了 steps=[] 的空计划、一个工具都没调，
                #   流直接跑到 COMPLETED，TaskResult 却是 ok=True / error=None。
                #   这是最危险的一类缺陷：Agent 自称成功、代码也认为成功，只有**外部判定器**
                #   靠"产物不存在"才能发现（Step 4 的 SUT 自述 vs 独立判定交叉校验就是为此）。
                #   空计划意味着任务根本没做，必须作为错误事件上报。
                if not self.plan or len(self.plan.steps) == 0:
                    logger.info(f"Planner&ReAct流创建计划失败或无子步骤")
                    yield ErrorEvent(
                        error="Agent未能生成任何可执行的计划步骤（Plan 为空），任务未执行"
                    )
                    self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.EXECUTING:
                # 17.流的状态为执行中，先将计划状态调整为运行中，同时调用执行Agent完成每个子步骤
                self.plan.status = ExecutionStatus.RUNNING

                # 18.获取当前计划的下一个需要执行的子步骤
                step = self.plan.get_next_step()

                # 19.如果不存在下一个需要执行的自己花，则更新流状态并执行后续步骤
                if not step:
                    logger.info(f"Planner&ReAct流状态从{FlowStatus.EXECUTING}变成{FlowStatus.SUMMARIZING}")
                    self.status = FlowStatus.SUMMARIZING
                    continue

                # 20.调用执行Agent执行对应的步骤
                logger.info(f"Planner&ReAct流开始执行步骤 {step.id}: {step.description[:50]}...")
                async for event in self.react.execute_step(self.plan, step, message):
                    yield event

                # 21.压缩执行Agent记忆，避免上下文腐化+消耗大量token
                logger.info(f"压缩{self.react.name} Agent记忆/上下文")
                await self.react.compact_memory()

                # 22.将状态更新为updating
                self.status = FlowStatus.UPDATING
            elif self.status == FlowStatus.UPDATING:
                # 23.流状态为更新表示需要更新计划
                logger.info(f"Planner&ReAct流开始更新计划")
                async for event in self.planner.update_plan(self.plan, step):
                    yield event

                # 24.计划更新完成，需要执行相应的子步骤
                logger.info(f"Planner&ReAct流状态从{FlowStatus.UPDATING}变成{FlowStatus.EXECUTING}")
                self.status = FlowStatus.EXECUTING
            elif self.status == FlowStatus.SUMMARIZING:
                # 25.流状态为总结中，则意味着所有子步骤都执行完成
                logger.info(f"Planner&ReAct流开始总结")

                # [lab/兜底] 收尾阶段失败**不应把已经做完的工作报成失败**。
                #   实测 4/40 次运行属于"活干完了但收尾失败"：
                #   汇总阶段的 LLM 调用挂了（网络抖动），但产物已经全部生成。
                #   它们被判失败纯粹是因为最后一步没写好总结 —— 这是拿"会不会收尾"
                #   代替了"活干完没干完"，而后者才是任务完成的定义。
                #
                #   兜底策略：如果已经有成功的步骤，就用已产出的结果拼一条总结交付；
                #   如果什么都没做成，则如实上报错误（不能把失败偷偷掩饰成成功）。
                summary_errors: List[str] = []
                delivered = False
                async for event in self.react.summarize():
                    if isinstance(event, ErrorEvent):
                        summary_errors.append(event.error)
                        continue
                    if isinstance(event, MessageEvent) and event.message:
                        delivered = True
                    yield event

                if summary_errors and not delivered:
                    fallback = self._fallback_summary()
                    if fallback:
                        logger.warning(
                            f"收尾阶段失败，已用兜底总结交付（错误：{summary_errors}）"
                        )
                        yield MessageEvent(role="assistant", message=fallback)
                    else:
                        # 没有任何可用结果 → 如实上报，不能假装完成
                        for error in summary_errors:
                            yield ErrorEvent(error=error)

                # 26.总结完毕，意味着流即将结束
                logger.info(f"Planner&ReAct流状态从{FlowStatus.SUMMARIZING}变成{FlowStatus.COMPLETED}")
                self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.COMPLETED:
                # 27.计划状态已完成则更新plan状态，并发送计划事件通知API已完成
                # [lab/D5-bugfix] self.plan 可能为 None：当 PlannerAgent 没能产出有效 Plan 时
                #   （上面第16步）会直接把状态置为 COMPLETED 并走到这里。
                #   原代码无条件执行 self.plan.status 会抛 AttributeError，把"规划失败"
                #   变成"整个进程崩溃"；PlanEvent(plan=None) 也会触发 pydantic 校验错误。
                #   评测场景下弱模型/网络抖动很容易触发这条路径，必须转成结构化错误事件。
                if self.plan is None or len(self.plan.steps) == 0:
                    # 计划为空：上面第16步已经报过 ErrorEvent 了，这里不再重复一个
                    # （重复报错会让错误链出现两条同样的条目，归因时会被重复计数）
                    logger.warning(f"Planner&ReAct流结束但计划为空，跳过完成事件")
                else:
                    self.plan.status = ExecutionStatus.COMPLETED
                    yield PlanEvent(status=PlanEventStatus.COMPLETED, plan=self.plan)
                self.status = FlowStatus.IDLE
                break
        # 28.任务已经结束则返回结束事件
        yield DoneEvent()
        logger.info(f"Planner&ReAct流处理任务消息已完毕")

    @property
    def done(self) -> bool:
        """只读属性，返回流是否运行结束"""
        return self.status == FlowStatus.IDLE

    def _fallback_summary(self) -> str:
        """用已完成的步骤结果拼一条兜底总结。

        只在**至少有一个步骤产出了结果**时才返回非空串 ——
        没有任何结果时应该如实失败，而不是编一句话把任务包装成成功。
        """
        if not self.plan or not self.plan.steps:
            return ""

        parts = [
            "任务已执行完成，但**生成总结报告的最后一步失败了**（通常是模型服务端的临时问题）。",
            "以下是各步骤实际产出的结果，据此交付：",
        ]
        has_result = False
        for index, step in enumerate(self.plan.steps, start=1):
            if not step.result:
                continue
            has_result = True
            parts.append(f"{index}. {step.description}：{step.result}")

        if not has_result:
            return ""

        parts.append("如果希望得到一份完整整理的报告，请回复“重新总结”。")
        return "\n".join(parts)
