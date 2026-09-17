#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重试策略：指数退避 + 抖动 + **幂等性感知**。

== 这里解决的是 SUT 的 D2 问题（重试导致重复副作用）==
SUT 原来的逻辑是：`for _ in range(max_retries): try: tool.invoke() except: 重试`。
它对**所有**工具一视同仁地重试，于是：
    沙箱超时 → 重试 write_file(append=True) → 文件里出现两遍内容
    网络抖动 → 重试 shell_execute("... >> log") → 副作用执行两次
这类 bug 极难发现，因为**最后都显示成功**。

== 正解：把两件事分开 ==
1. `ToolResult.retryable` 表达"**这个失败类型是不是暂态**"（由工具自己判断，
   例如超时是暂态、路径越界不是）；
2. `is_idempotent(tool)` 表达"**重复执行安不安全**"（由这里的声明表判断）。
只有当「暂态」且「安全」时才自动重试。

== 未知工具默认**非幂等** ==
MCP 动态注册的工具、A2A 远程 Agent，我们不知道它们有没有副作用。
"不知道"就必须按"危险"处理 —— 少重试一次只是慢一点，
多执行一次副作用可能已经写坏了数据。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402

# 重复执行**安全**的工具（幂等或"重试最坏只是失败，不会产生第二次副作用"）
IDEMPOTENT_TOOLS = {
    # 纯读
    "read_file", "search_in_file", "find_files", "list_files", "check_file_exists",
    "shell_read_output", "shell_wait_process",
    "browser_view", "browser_console_view", "browser_scroll_up", "browser_scroll_down",
    "search_web", "get_remote_agent_cards",
    # 写入类但重试安全：第二次执行会因为"目标已不存在/已生效"而失败或等价成功，
    # 不会产生叠加副作用。注意 write_file 不在此列 —— append=True 时会叠加！
    "replace_in_file", "delete_file", "shell_kill_process",
}

# 重复执行**会产生副作用**的工具
NON_IDEMPOTENT_TOOLS = {
    "write_file",  # append=True 时重复执行 = 内容翻倍
    "shell_execute",  # 命令可能带重定向/建库/装包等副作用
    "shell_write_input",  # 向交互式进程重复输入 = 语义改变
    "message_notify_user",  # 重复播报 = 打扰用户
    "message_ask_user",  # 重复提问 = 对话错乱
    "browser_navigate", "browser_restart", "browser_click", "browser_input",
    "browser_press_key", "browser_select_option", "browser_console_exec",
    "call_remote_agent",  # 消耗远程资源，且可能非幂等
}

# 无论是否幂等，这些失败类型都不该重试（确定性错误，重试必然同样失败）
FATAL_ERROR_TYPES = {
    "tool_not_found",
    "invalid_arguments",  # 工具参数不是合法 JSON：重试同一串坏参数没有意义
    "path_escape",
    "invalid_argument",
    "unsupported",
    "file_not_found",
    "not_found",
    "session_not_found",
}


def is_idempotent(function_name: str) -> bool:
    """判断工具是否幂等。**未知工具返回 False**（fail-safe）。"""
    if function_name in NON_IDEMPOTENT_TOOLS:
        return False
    if function_name in IDEMPOTENT_TOOLS:
        return True
    # 未知（例如 MCP 动态工具）→ 按危险处理
    return False


def compute_backoff(
        attempt: int,
        *,
        base: float = 0.5,
        factor: float = 2.0,
        cap: float = 8.0,
        jitter_ratio: float = 0.25,
        rng: Optional[random.Random] = None,
) -> float:
    """指数退避 + 抖动。

    抖动（jitter）不是装饰：Step 4 会并行跑任务集，如果所有任务都在同一时刻
    因为同一个故障而退避、又同时重试，就会形成**重试风暴**（thundering herd），
    把上游打得更死。抖动把重试时间打散。
    传入固定 seed 的 rng 可让评测可复现。
    """
    exponent = max(0, attempt - 1)
    delay = min(cap, base * (factor ** exponent))
    if jitter_ratio <= 0:
        return delay
    generator = rng or random
    jittered = delay * (1.0 + generator.uniform(-jitter_ratio, jitter_ratio))
    # 上限是**硬**上限：抖动不得把它顶穿（否则靠近上限时延迟会超过配置值，
    # 让"最多等 8 秒"这个承诺失效）。代价是接近上限时抖动只能缩短延迟，不再双向。
    return max(0.0, min(cap, jittered))


@dataclass
class RetryDecision:
    """一次重试判定。"""

    should_retry: bool
    delay: float
    reason: str


def decide_retry(
        *,
        function_name: str,
        result: Optional[ToolResult],
        error: Optional[BaseException],
        attempt: int,
        max_attempts: int,
        rng: Optional[random.Random] = None,
) -> RetryDecision:
    """决定要不要重试，并给出**可读的原因**。

    为什么要返回原因字符串而不是裸布尔：
    归因报告需要区分"没重试是因为不该重试"还是"因为重试次数用完了"，
    这两个结论指向完全不同的改法。原因字符串会直接进 trace 的 span 属性。
    """
    if attempt >= max_attempts:
        return RetryDecision(False, 0.0, "max_attempts_reached")

    # 1.异常路径：视为暂态（工具抛异常通常是网络/沙箱层问题）
    if error is not None:
        if not is_idempotent(function_name):
            return RetryDecision(False, 0.0, "skipped_non_idempotent")
        return RetryDecision(True, compute_backoff(attempt, rng=rng), "exception_retry")

    # 2.结构化失败路径
    if result is None or result.success:
        return RetryDecision(False, 0.0, "no_failure")

    error_type = result.error_type or "unknown"
    if error_type in FATAL_ERROR_TYPES:
        return RetryDecision(False, 0.0, f"fatal_error_type:{error_type}")
    if not result.retryable:
        return RetryDecision(False, 0.0, f"not_marked_retryable:{error_type}")
    if not is_idempotent(function_name):
        # 这就是 D2 的正解：暂态失败 + 非幂等工具 → **不自动重试**，
        # 把"要不要重试"交回给 LLM（它能看到失败原因，自己决定换个做法）。
        return RetryDecision(False, 0.0, "skipped_non_idempotent")

    return RetryDecision(True, compute_backoff(attempt, rng=rng), f"retryable:{error_type}")
