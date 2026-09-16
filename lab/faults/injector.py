#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""故障注入器：中间件式，与工具实现完全解耦。

== 为什么做成"中间件"而不是写死在工具里 ==
handoff 决策 3 要求"工具实现与故障策略解耦，可组合任意故障 × 任意工具"。
落地形态就是本模块暴露三个钩子，由中间件在**真实调用前后**依次调用：

    args   = injector.mutate_args(name, args, attempt)   # 改参数（如截断写入内容）
    exc    = injector.maybe_raise(name, attempt)         # 或者直接制造异常
    result = await real_tool(...)
    result = injector.mutate_result(name, args, result, attempt)  # 改结果（静默失败）

好处：
- 不动 `FileTool` / `ShellTool` 一行代码；
- 任意故障可以和任意工具组合（`FaultRule.tool` 支持 glob）；
- 故障注入本身是**可复现的**（固定 seed）—— 否则"加固后成功率 79%"这种数字没法复现。

== 一个容易被忽略的坑：注入要"按次"而不是"按概率" ==
`transient_error` 的语义是"这次失败、下次成功"。
如果实现成"每次调用以 X% 概率失败"，那么重试也会以 X% 概率失败，
你就无法确认"重试机制到底有没有生效"。
所以本实现按**调用序号**判断（`fail_times`），让重试路径可以被确定性地验证。
"""

from __future__ import annotations

import asyncio
import fnmatch
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402
from lab.faults.kinds import FaultKind, FaultRule  # noqa: E402

# 允许被"内容篡改"的工具：必须是**只读**工具。
# 绝不能篡改 write_file 的结果 —— 那是伪造成功，会让 workspace 和 trace 不一致。
_READ_ONLY_TOOLS = {
    "read_file", "shell_read_output", "search_in_file", "find_files", "list_files",
    "browser_view", "browser_console_view", "search_web",
}


@dataclass
class FaultDecision:
    """一次注入决定。"""

    kind: FaultKind
    rule: FaultRule

    @property
    def label(self) -> str:
        return self.kind.value


class FaultInjector:
    """故障注入器（一个实例 = 一个任务）。"""

    def __init__(self, rules: Optional[List[FaultRule]] = None, *, seed: int = 42) -> None:
        self.rules: List[FaultRule] = list(rules or [])
        # 固定 seed：故障注入必须可复现，否则"加固前后对比"无法归因
        self._rng = random.Random(seed)
        self._calls: Dict[str, int] = {}  # 每个工具被调用（或尝试）过几次
        self._injected: Dict[int, int] = {}  # 每条规则注入了几次
        self.log: List[Dict[str, Any]] = []  # 注入流水，最终进 trace

    @property
    def enabled(self) -> bool:
        return bool(self.rules)

    # ==================== 判定 ====================

    def decide(self, function_name: str, attempt: int = 1) -> Optional[FaultDecision]:
        """判断这一次调用是否注入故障。

        注意：`attempt` 只用于记录，真正的判定依据是**该工具的累计调用序号**，
        这样"前 N 次失败"的语义才是确定的。
        """
        if not self.rules:
            return None

        index = self._calls.get(function_name, 0)
        self._calls[function_name] = index + 1

        for rule_index, rule in enumerate(self.rules):
            if not fnmatch.fnmatch(function_name, rule.tool):
                continue
            if index < rule.start_after:
                continue
            if rule.max_injections is not None and self._injected.get(rule_index, 0) >= rule.max_injections:
                continue
            # transient_error 按序号：前 fail_times 次失败，之后恢复正常
            if rule.kind == FaultKind.TRANSIENT_ERROR and rule.fail_times is not None:
                if index >= rule.fail_times:
                    continue
                self._record(rule_index, rule, function_name, index, forced=True)
                return FaultDecision(kind=rule.kind, rule=rule)
            # 其余按概率
            if self._rng.random() >= rule.rate:
                continue
            self._record(rule_index, rule, function_name, index, forced=False)
            return FaultDecision(kind=rule.kind, rule=rule)

        return None

    def _record(self, rule_index: int, rule: FaultRule, function_name: str, index: int, *, forced: bool) -> None:
        self._injected[rule_index] = self._injected.get(rule_index, 0) + 1
        self.log.append({
            "tool": function_name,
            "kind": rule.kind.value,
            "call_index": index,
            "rule": rule.describe(),
            "by": "sequence" if forced else "random",
        })

    # ==================== 三个钩子 ====================

    async def maybe_delay(self, decision: Optional[FaultDecision]) -> None:
        """latency_spike：只加延迟，不改变结果语义。"""
        if decision and decision.kind == FaultKind.LATENCY_SPIKE:
            await asyncio.sleep(decision.rule.latency_s)

    def maybe_raise(self, decision: Optional[FaultDecision], function_name: str) -> Optional[BaseException]:
        """需要"真的失败"的故障：返回异常实例（由调用方 raise）。"""
        if decision is None:
            return None
        if decision.kind == FaultKind.TIMEOUT:
            return asyncio.TimeoutError(f"注入故障: {function_name} 超时")
        if decision.kind == FaultKind.TRANSIENT_ERROR:
            return ConnectionError(f"注入故障: {function_name} 暂时不可用（transient）")
        if decision.kind == FaultKind.PERMANENT_ERROR:
            return RuntimeError(f"注入故障: {function_name} 永久性失败")
        if decision.kind == FaultKind.FLAKY:
            return ConnectionError(f"注入故障: {function_name} 随机失败（flaky）")
        return None

    def mutate_args(self, decision: Optional[FaultDecision], args: Dict[str, Any]) -> Dict[str, Any]:
        """在**真实调用之前**改参数。

        `partial_write` 的正解就在这里：把要写入的内容截掉一半，
        于是文件**真的**只写了一半（而不是伪造一个"只写了一半"的报告）。
        这样后置校验才有意义 —— 它比对的是真实文件，不是自说自话的返回值。
        """
        if decision is None or decision.kind != FaultKind.PARTIAL_WRITE:
            return args
        content = args.get("content")
        if not isinstance(content, str) or not content:
            return args
        mutated = dict(args)
        mutated["content"] = content[: max(1, len(content) // 2)]
        return mutated

    def mutate_result(
            self,
            decision: Optional[FaultDecision],
            function_name: str,
            result: ToolResult,
    ) -> ToolResult:
        """在**真实调用之后**改结果（制造静默失败）。"""
        if decision is None or not result.success:
            return result

        kind = decision.kind

        if kind == FaultKind.EMPTY_RESULT:
            # 声称成功但什么都不返回
            return ToolResult(success=True, message="(注入) 操作成功", data=None)

        if kind == FaultKind.MALFORMED_RESULT:
            # 声称成功且字段名看着像，但该有的字段没有
            return ToolResult(success=True, message="(注入) 操作成功", data={"status": "ok"})

        if kind == FaultKind.TRUNCATED_RESULT:
            return self._truncate_data(result)

        if kind == FaultKind.SILENT_WRONG_RESULT:
            # 只在只读工具上做：结构完整、看起来正常，但内容是错的。
            # 这是最难发现的一类故障 —— 只有内容级校验能抓住。
            if function_name not in _READ_ONLY_TOOLS:
                return result
            return self._corrupt_data(result)

        return result

    # ==================== 结果篡改的具体实现 ====================

    @staticmethod
    def _truncate_data(result: ToolResult) -> ToolResult:
        data = result.data
        if not isinstance(data, dict):
            return result
        mutated = dict(data)
        touched = False

        # 优先截断明显"够长"的字符串字段
        candidates = [(k, v) for k, v in mutated.items() if isinstance(v, str) and len(v) > 4]
        if not candidates:
            # 兜底：即使字段都很短，也把最长的那个截断。
            # 为什么要兜底：如果故障规则命中了却没有任何修改，
            # 实验就变成"以为注入了、其实没注入"，结论会静默地错。
            candidates = [(k, v) for k, v in mutated.items() if isinstance(v, str) and len(v) > 1][:1]
        for key, value in candidates:
            mutated[key] = value[: max(1, len(value) // 3)]
            touched = True

        if not touched:
            for key, value in mutated.items():
                if isinstance(value, list) and len(value) > 1:
                    mutated[key] = value[: max(1, len(value) // 3)]
                    touched = True
                    break

        if not touched:
            return result
        return ToolResult(success=True, message=result.message, data=mutated, attempts=result.attempts)

    @staticmethod
    def _corrupt_data(result: ToolResult) -> ToolResult:
        """内容篡改：把文本里的数字改掉 / 追加一个隐蔽标记。

        选"改数字"是因为它最阴险：如果 Agent 拿这个数字去做计算，
        最终答案会错，但整条链路每一步都显示成功。
        """
        data = result.data
        if not isinstance(data, dict):
            return result
        mutated = dict(data)
        touched = False
        for key, value in mutated.items():
            if not isinstance(value, str) or not value:
                continue
            if any(ch.isdigit() for ch in value):
                import re

                mutated[key] = re.sub(r"\d", lambda m: str((int(m.group()) + 1) % 10), value, count=3)
                touched = True
                break
        if not touched:
            return result
        return ToolResult(success=True, message=result.message, data=mutated, attempts=result.attempts)

    # ==================== 报告 ====================

    def report(self) -> Dict[str, Any]:
        """注入汇总（进 TaskResult / trace）。"""
        by_kind: Dict[str, int] = {}
        for item in self.log:
            by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        return {
            "rules": [rule.describe() for rule in self.rules],
            "injections": len(self.log),
            "by_kind": by_kind,
        }
