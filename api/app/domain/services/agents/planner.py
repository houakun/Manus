#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/20 15:27
@Author  : thezehui@gmail.com
@File    : planner.py
"""
from typing import Optional, AsyncGenerator

from app.domain.models.event import BaseEvent, MessageEvent, PlanEvent, PlanEventStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.services.prompts.planner import (
    PLANNER_SYSTEM_PROMPT,
    CREATE_PLAN_PROMPT,
    UPDATE_PLAN_PROMPT,
)
from app.domain.services.prompts.system import SYSTEM_PROMPT
from .base import BaseAgent
from .base import StructuredResult

"""
多Agent系统/flow=PlannerAgent+ReActAgent

顺序:
1. PlannerAgent生成规划;
2. 循环取出规划中的子步骤，让ReActAgent执行，依次迭代;
3. ReActAgent执行完每一个子步骤之后，需要将子步骤结果+Plan传递给PlannerAgent让其更新计划/Plan；
4. 循环取出规划中的子步骤，让ReActAgent执行，依次迭代;
5. ...
6. 直到所有子任务/步骤都完成，这时候将子步骤的所有结果汇总进行总结(ReActAgent);

PlannerAgent:
- 功能: 将用户的需求拆解成多个子任务+根据已完成的子任务更新规划
- 提示词: 创建规划的prompt、更新规划的prompt

ReActAgent:
- 功能: 迭代执行完每一个子任务、汇总所有的子任务进行总结
- 提示词: 执行任务的prompt、汇总总结prompt
"""

class PlannerAgent(BaseAgent):
    """规划Agent，用于将用户的任务/需求拆解成多个子步骤"""
    name: str = "planner"
    _system_prompt: str = SYSTEM_PROMPT + PLANNER_SYSTEM_PROMPT
    _format: Optional[str] = "json_object"
    _tool_choice: Optional[str] = "none"

    async def create_plan(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """根据用户传递的消息创建计划/规划，迭代返回对应的事件"""
        # 1.根据用户传递的消息生成创建plan的提示词
        query = CREATE_PLAN_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
        )

        # 2.调用invoke函数返回迭代事件
        #   [lab/D10] 改用 invoke_structured：解析/校验失败时会把"格式错误 + 期望结构"
        #   回灌给模型重试一次，而不是直接崩溃。
        #   实测案例：模型在结构化输出位置返回了一个数组 → 原代码
        #   Plan.model_validate(数组) 抛 ValidationError → 整个任务挂掉。
        holder = StructuredResult()
        async for event in self.invoke_structured(query, Plan, holder):
            if isinstance(event, MessageEvent):
                # 已由 invoke_structured 内部解析完成，这里不会拿到 MessageEvent
                yield event
            else:
                yield event

        if holder.ok:
            plan: Plan = holder.value
            yield PlanEvent(plan=plan, status=PlanEventStatus.CREATED)

    async def update_plan(self, plan: Plan, step: Step) -> AsyncGenerator[BaseEvent, None]:
        """根据传递的原始规划+子步骤更新事件"""
        # 1.使用plan+step创建更新Plan提示词
        query = UPDATE_PLAN_PROMPT.format(
            plan=plan.model_dump_json(),
            step=step.model_dump_json(),
        )

        # 2.调用invoke获取对应的事件
        #   [lab/D10] 同 create_plan：解析失败 → 纠错重试，不直接崩。
        holder = StructuredResult()
        async for event in self.invoke_structured(query, Plan, holder):
            yield event

        if not holder.ok:
            return

        updated_plan: Plan = holder.value

        # 6.拷贝更新计划中的steps，避免造成数据污染
        new_steps = [Step.model_validate(step) for step in updated_plan.steps]

        # 7.查询旧计划中第一个未完成的计划
        first_pending_index = None
        for idx, step in enumerate(plan.steps):
            if not step.done:
                first_pending_index = idx
                break

        # 8.判断是否有未完成的步骤，如果有则执行更新
        if first_pending_index is not None:
            # 9.获取历史已完成的子步骤并更新
            updated_steps = plan.steps[:first_pending_index]
            updated_steps.extend(new_steps)

            # 10.更新plan规划
            plan.steps = updated_steps

        # 11.返回规划更新事件
        yield PlanEvent(plan=plan, status=PlanEventStatus.UPDATED)
