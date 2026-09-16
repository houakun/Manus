#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工具层中间件 —— handoff 决策 4 说的那个"杠杆点"。

== 落在哪里：GuardedTool 代理，不改 SUT ==
handoff 原计划是"改 SUT 的 `_invoke_tool`，让它调用 `call_tool`"。
但 Step 1 已经发现一个更好的接缝：`PlannerReActFlow` 接受**注入的工具列表**。
SUT 对工具只用四个成员：`.name` / `.get_tools()` / `.has_tool()` / `.invoke()`。
所以用一个代理包住每个工具就够了：

    真工具 FileTool  ←  GuardedTool(FileTool, guard)  ←  SUT 的 tools 列表

好处：
- **SUT 零改动**（连"改一行"都不需要）；
- Step 4 的自研 loop 只要也用 `BaseTool`，就自动获得同一套预算/熔断/重试/校验，
  **这才是 A/B 对比两边口径一致的前提**。

== 一次工具调用的完整流水 ==
    1. 记一次调用 → 预算里的 tool_calls +1
    2. 算动作指纹 → 循环检测（观察"原地打转"）
    3. 预算检查（observe：只记录）
    4. 循环内每次尝试：
         a. 故障注入判定（按调用序号，保证 transient_error 可复现）
         b. 延迟注入 → 参数篡改 → 真实调用
         c. 结果篡改（制造静默失败）
         d. 重试判定（**幂等性感知**：非幂等工具不会自动重试 → D2 的正解）
    5. 后置校验（独立确认"声称成功"是否真的成立）
    6. 把上面所有结论写进当前 span

== 一个关键设计：中间件**永不抛异常** ==
任何失败都转成 `ToolResult(success=False, ...)` 返回。
两个原因：
1. SUT 的 `_invoke_tool` 只在异常时才重试 —— 我们返回结果，它的重试循环自然退化，
   不会出现"中间件重试 3 次 × SUT 再重试 3 次 = 9 次"的双重试；
2. 可预期的失败应该走返回值、由 LLM 看到并自行决策，而不是走异常打断控制流
   （这与 Step 1 修的 D4 是同一个原则）。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, List, Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402
from app.domain.services.tools.base import BaseTool  # noqa: E402

from lab.faults.injector import FaultInjector  # noqa: E402
from lab.guard.budget import Budget  # noqa: E402
from lab.guard.loop_guard import LoopGuard  # noqa: E402
from lab.guard.postcondition import check_postcondition  # noqa: E402
from lab.guard.retry import decide_retry, is_idempotent  # noqa: E402


def _classify_exception(error: Optional[BaseException]) -> tuple:
    """把异常分类成 (error_type, retryable)。

    `retryable` 表达的是"这个失败类型是不是暂态"，**不是**"该不该重试"。
    该不该重试还要看工具幂等性（见 decide_retry）。
    """
    if error is None:
        return "tool_error", False
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout", True
    if isinstance(error, ConnectionError):
        return "connection_error", True
    return "tool_error", False


class ToolGuard:
    """工具层中间件集合（一个实例 = 一个任务）。"""

    def __init__(
            self,
            *,
            recorder: Any = None,
            budget: Optional[Budget] = None,
            loop_guard: Optional[LoopGuard] = None,
            injector: Optional[FaultInjector] = None,
            sandbox: Any = None,
            max_attempts: int = 3,
            verify_postconditions: bool = True,
            seed: int = 42,
            watch_literals: Optional[List[str]] = None,
    ) -> None:
        self._recorder = recorder
        self.budget = budget
        self.loop_guard = loop_guard or LoopGuard()
        self.injector = injector
        self._sandbox = sandbox
        self._max_attempts = max(1, max_attempts)
        self._verify = verify_postconditions
        self._rng = random.Random(seed)
        # 硬编码检测：这些字面量如果直接出现在写入内容/命令里，说明是"抄答案"而不是算出来的。
        # 由 bench 层传入（它才知道答案是什么），guard 只负责盯。
        self._watch_literals = [item for item in (watch_literals or []) if item]

        # 观察结果（进 TaskResult）
        self.postcondition_warnings: List[str] = []
        self.retry_stats: Dict[str, int] = {}
        self.fault_counts: Dict[str, int] = {}
        # 失败类型计数：bench 的"越权访问"等过程规则靠它
        self.error_types: Dict[str, int] = {}
        # 硬编码可疑记录：{文件或命令: [命中的字面量]}
        self.hardcode_suspects: Dict[str, List[str]] = {}

    def _note_error_type(self, error_type: Optional[str]) -> None:
        """累计失败类型（供过程规则与归因统计使用）。"""
        if not error_type:
            return
        self.error_types[error_type] = self.error_types.get(error_type, 0) + 1

    def _check_hardcode(self, function_name: str, args: Dict[str, Any]) -> None:
        """盯住"直接写入答案"这个作弊模式。

        两种作弊路径都要盖住：
          - `write_file` 直接把答案写进文件（content 命中字面量）
          - `shell_execute` 用 echo/printf 把答案重定向进文件（command 命中字面量）
        这是个**启发式**：可能误报（例如任务本身就是要求写入某个固定字符串），
        所以它只进 process_flags，不影响结果分 `ok`。
        """
        if not self._watch_literals:
            return
        if function_name == "write_file":
            haystack = args.get("content") or ""
            where = args.get("filepath") or "write_file"
        elif function_name == "shell_execute":
            haystack = args.get("command") or ""
            where = args.get("command", "shell_execute")[:80]
        else:
            return
        if not isinstance(haystack, str):
            return
        hits = [literal for literal in self._watch_literals if literal in haystack]
        if hits:
            self.hardcode_suspects.setdefault(where, []).extend(hits)

    # ==================== 工具包装 ====================

    def wrap_tools(self, tools: List[BaseTool]) -> List[BaseTool]:
        """把工具列表整体包一层代理。"""
        return [GuardedTool(inner=tool, guard=self) for tool in tools]

    # ==================== 由适配器调用 ====================

    def note_step(self) -> None:
        """进入新的计划步骤。

        循环检测要在这里重置连续计数：跨步骤的"连续"没有意义 ——
        第 1 步和第 3 步都调 `read_file(同一文件)` 是正常行为，不算原地打转。
        """
        if self.budget is not None:
            self.budget.note_step()
        self.loop_guard.reset_after_step()

    def check_budget(self) -> None:
        """预算检查（可被"每次 LLM 调用后"回调触发）。

        为什么 LLM 调用后也要查：token 和成本是**在 LLM 调用时**涨的，
        只在工具调用前检查的话，一个不停思考、不调工具的 Agent 会漏检。
        """
        if self.budget is None:
            return
        violations = self.budget.check()
        if violations and self._recorder is not None:
            self._recorder.annotate(
                budget_violations=",".join(v.metric for v in violations),
                **{"budget_hard_exceeded": any(v.exceeded_hard for v in violations)},
            )

    # ==================== 核心：一次工具调用 ====================

    async def call_tool(self, inner: BaseTool, function_name: str, args: Dict[str, Any]) -> ToolResult:
        """执行一次工具调用，串联预算 / 循环检测 / 故障注入 / 重试 / 后置校验。"""
        # 1.预算与循环检测
        if self.budget is not None:
            self.budget.note_tool_call()
        hit = self.loop_guard.observe(function_name, args)
        self._check_hardcode(function_name, args)
        self.check_budget()

        attempt = 0
        result: Optional[ToolResult] = None
        error: Optional[BaseException] = None
        retry_reasons: List[str] = []
        faults: List[str] = []

        # 2.尝试循环
        while attempt < self._max_attempts:
            attempt += 1

            decision = self.injector.decide(function_name, attempt) if self.injector else None
            if decision is not None:
                faults.append(decision.label)
                self.fault_counts[decision.label] = self.fault_counts.get(decision.label, 0) + 1
                await self.injector.maybe_delay(decision)

            # 2.1 故障注入：需要"真的失败"的类型
            injected = self.injector.maybe_raise(decision, function_name) if decision else None
            if injected is not None:
                error, result = injected, None
            else:
                # 2.2 参数篡改（partial_write 在这里真正截断内容）
                call_args = self.injector.mutate_args(decision, args) if decision else args
                try:
                    result = await inner.invoke(function_name, **call_args)
                    error = None
                except Exception as exc:  # noqa: BLE001
                    error, result = exc, None
                # 2.3 结果篡改（制造静默失败）
                if result is not None and decision is not None:
                    result = self.injector.mutate_result(decision, function_name, result)

            # 2.4 重试判定
            decision_retry = decide_retry(
                function_name=function_name,
                result=result,
                error=error,
                attempt=attempt,
                max_attempts=self._max_attempts,
                rng=self._rng,
            )
            retry_reasons.append(decision_retry.reason)
            if not decision_retry.should_retry:
                break
            self.retry_stats[decision_retry.reason] = self.retry_stats.get(decision_retry.reason, 0) + 1
            await asyncio.sleep(decision_retry.delay)

        # 3.任何失败都转成结构化结果返回（永不抛异常，理由见模块注释）
        if result is None:
            error_type, retryable = _classify_exception(error)
            self._note_error_type(error_type)
            result = ToolResult(
                success=False,
                message=f"{type(error).__name__}: {error}" if error else "工具调用失败",
                error_type=error_type,
                retryable=retryable,
                attempts=attempt,
            )
        else:
            result.attempts = attempt
            if not result.success:
                self._note_error_type(result.error_type)

        # 4.后置校验：独立确认"声称成功"是否真的成立
        warning: Optional[str] = None
        if self._verify and result.success:
            try:
                warning = await check_postcondition(
                    function_name=function_name,
                    args=args,  # 注意用**原始**参数：调用方的意图才是判断标准
                    result=result,
                    sandbox=self._sandbox,
                )
            except Exception as exc:  # noqa: BLE001
                # 校验器自己的异常不能影响主流程（校验是"附加信息"，不是关键路径）
                warning = f"后置校验器异常: {type(exc).__name__}: {exc}"
            if warning:
                self.postcondition_warnings.append(f"{function_name}: {warning}")

        # 5.把结论写进当前 span（由适配器在收到 ToolEvent(CALLED) 时一并收口）
        if self._recorder is not None:
            attrs: Dict[str, Any] = {
                "repeat_consecutive": hit.consecutive,
                "action_diversity": round(hit.diversity, 3),
                "idempotent": is_idempotent(function_name),
            }
            if attempt > 1:
                attrs["retry_reasons"] = ",".join(dict.fromkeys(retry_reasons))
            if faults:
                attrs["faults_injected"] = ",".join(sorted(set(faults)))
            if warning:
                attrs["postcondition_warning"] = warning
            self._recorder.annotate(**attrs)

        return result

    # ==================== 报告 ====================

    def report(self) -> Dict[str, Any]:
        """汇总本任务的加固层观察结果（进 TaskResult.guard）。"""
        data: Dict[str, Any] = {
            "postconditions": {"warnings": self.postcondition_warnings},
            "retries": {"total": sum(self.retry_stats.values()), "by_reason": dict(self.retry_stats)},
            "loop": self.loop_guard.report(),
            "error_types": dict(self.error_types),
        }
        if self.hardcode_suspects:
            data["hardcode_suspects"] = {key: sorted(set(value)) for key, value in self.hardcode_suspects.items()}
        if self.budget is not None:
            data["budget"] = self.budget.report().model_dump(mode="json")
        if self.injector is not None:
            data["faults"] = self.injector.report()
        if self.fault_counts:
            data["faults_observed"] = dict(self.fault_counts)
        return data


class GuardedTool(BaseTool):
    """工具代理：把 `invoke` 换成经过中间件的版本。

    只转发 SUT 真正会用到的四个成员，其余一概不代理 ——
    代理范围越小，越不容易在 SUT 升级时悄悄失效。
    """

    def __init__(self, inner: BaseTool, guard: ToolGuard) -> None:
        super().__init__()
        self._inner = inner
        self._guard = guard

    @property
    def name(self) -> str:
        """转发工具集名字（SUT 会把它填进 ToolEvent.tool_name）。"""
        return self._inner.name

    def get_tools(self) -> List[Dict[str, Any]]:
        """转发工具 schema（LLM 的工具清单）。"""
        return self._inner.get_tools()

    def has_tool(self, tool_name: str) -> bool:
        return self._inner.has_tool(tool_name)

    async def invoke(self, tool_name: str, **kwargs) -> ToolResult:
        """经过中间件调用真实工具。"""
        return await self._guard.call_tool(self._inner, tool_name, kwargs)
