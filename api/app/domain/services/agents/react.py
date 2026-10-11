#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/21 10:26
@Author  : thezehui@gmail.com
@File    : react.py
"""
from typing import AsyncGenerator

from app.domain.models.event import (
    StepEventStatus,
    StepEvent,
    ToolEvent,
    MessageEvent,
    ErrorEvent,
    ToolEventStatus,
    WaitEvent,
    BaseEvent
)
from app.domain.models.file import File
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.prompts.react import REACT_SYSTEM_PROMPT, EXECUTION_PROMPT, SUMMARIZE_PROMPT
from app.domain.services.prompts.system import SYSTEM_PROMPT
from .base import BaseAgent, StructuredResult

class ReActAgent(BaseAgent):
    """基于ReAct架构的执行Agent"""
    name: str = "react"
    _system_prompt: str = SYSTEM_PROMPT + REACT_SYSTEM_PROMPT
    # pyright: ignore[reportIncompatibleVariableOverride] —— 父类声明是 `str | None`，
    # 这里赋的是具体值（**既有**注解不一致，运行时无影响）
    _format: str = "json_object"  # pyright: ignore[reportIncompatibleVariableOverride]  # format控制的是content、工具调用控制的是tool_calls两者不冲突

    async def execute_step(self, plan: Plan, step: Step, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """根据传递的消息+规划+子步骤，执行相应的子步骤"""
        # 1.根据传递的内容生成执行消息
        query = EXECUTION_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
            language=plan.language,
            step=step.description,
        )

        # 2.更新步骤的执行状态为运行中并返回Step事件
        step.status = ExecutionStatus.RUNNING
        yield StepEvent(step=step, status=StepEventStatus.STARTED)

        # 3.调用invoke获取agent返回的事件内容
        #   [lab/D10] 改用 invoke_structured：模型返回的最终 JSON 不符合 Step 结构时
        #   （实测：它把转换后的数据数组当答案交了），会把错误回灌让它重试，
        #   而不是 Step.model_validate() 抛 ValidationError 把整条流炸掉。
        holder = StructuredResult()
        async for event in self.invoke_structured(query, Step, holder):
            # 4.判断事件类型执行不同操作
            if isinstance(event, ToolEvent):
                # 5.工具事件需要判断工具的名称是否为message_ask_user
                if event.function_name == "message_ask_user":
                    # 6.工具如果在调用中，我们需要返回一条消息告知用户需要让用户处理什么
                    if event.status == ToolEventStatus.CALLING:
                        yield MessageEvent(
                            role="assistant",
                            message=event.function_args.get("text", "")
                        )
                    elif event.status == ToolEventStatus.CALLED:
                        # 7.如果工具事件为已调用，则需要返回等待事件并中断程序
                        yield WaitEvent()
                        return
                    continue
            elif isinstance(event, ErrorEvent):
                # 13.错误事件更新步骤的状态
                step.status = ExecutionStatus.FAILED
                step.error = event.error

                # 14.返回子步骤对应事件
                yield StepEvent(step=step, status=StepEventStatus.FAILED)

            # 15.其他场景将事件直接返回
            yield event

        # 8.拿到结构化结果 → 更新子步骤的数据
        if holder.ok:
            # holder.ok 保证 value 非 None（**既有**注解问题，运行时无影响）
            new_step: Step = holder.value  # pyright: ignore[reportAssignmentType]
            step.success = new_step.success
            step.result = new_step.result
            step.attachments = new_step.attachments

            # [lab/D11] 模型**可以在 Step 结构里诚实地写 `success=false`**（承认这一步没做成）。
            #   原代码无条件置 COMPLETED（而且下面还有一处重复赋值）→
            #   “失败的步骤”被当成“完成的步骤” → 流程以“任务成功”结束
            #   （`self_reported_ok=True`），而产物根本不存在。
            #   实测（2026-10-10，216 次运行）：**16 次失败全部是这种形态** ——
            #   `tool_calls=0`、answer 是计划文本、自述成功（假阳性率 94%）；
            #   只有外部判定器（不看自述）靠“产物不存在”才能发现。
            #   注意 `Step.done` 把 FAILED 也算“已结束”，所以这里改成 FAILED
            #   **不会**让流程卡在该步骤上（只是不再谎报完成）。
            step.status = ExecutionStatus.COMPLETED if new_step.success else ExecutionStatus.FAILED
            if not new_step.success:
                step.error = step.error or "模型在步骤结果里报告 success=false：这一步没有做成"

            # 11.返回步骤事件（成功 / 失败要如实反映）
            yield StepEvent(
                step=step,
                status=StepEventStatus.COMPLETED if new_step.success else StepEventStatus.FAILED,
            )

            # 12.如果子步骤拿到了结果，还需要返回一段消息给用户(将结果返回给用户)
            if step.result:
                yield MessageEvent(role="assistant", message=step.result)

    async def summarize(self) -> AsyncGenerator[BaseEvent, None]:
        """调用Agent汇总历史的消息并生成最终回复+附件"""
        # 1.构建请求query
        query = SUMMARIZE_PROMPT

        # 2.调用invoke方法获取Agent生成的事件
        #   [lab/D10] 同其他调用点：汇总输出格式不符 → 纠错重试。
        holder = StructuredResult()
        async for event in self.invoke_structured(query, Message, holder):
            yield event

        if not holder.ok:
            return

        # 5.拿到解析好的 Message（holder.ok 保证非 None）
        message: Message = holder.value  # pyright: ignore[reportAssignmentType]

        # 6.提取消息中的附件信息
        attachments = [File(filepath=filepath) for filepath in message.attachments]

        # 7.返回消息事件并将消息+附件进行相应
        yield MessageEvent(
            role="assistant",
            message=message.message,
            attachments=attachments,
        )
