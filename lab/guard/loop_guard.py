#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""循环检测：识别"原地打转"并（在启用后）熔断。

== 为什么需要它 ==
SUT 唯一的循环边界是 `max_iterations=100`（单次 invoke 内），而且外层 flow 是
`while True`。实测一次"7 乘 6"的任务出现了 **4 次 shell_execute**，
Step 1 的"1 到 100 求和"出现过 **10 次连续 shell_execute** —— 都是同一个动作反复试。
没有熔断时，这类"原地打转"会一直烧 token 直到把预算耗光。

== 检测什么、不检测什么（关键区分）==
- 检测：**同一个工具的同一组参数**连续重复 N 次 → 典型的"卡住了"。
- 不检测：同一个工具用**不同参数**调用多次（例如依次写 5 个文件）→ 这是正常行为。
只按"工具名"去重会把这第二种情况也误判成循环，那就会把正常任务掐死。
所以指纹必须是 `(function_name, args_digest)`。

== 又一个细节：报告"连续"还是"累计" ==
    A B A B A B  → 累计 3 次但从不连续 → 更像是探索，不是卡住
    A A A A      → 连续 4 次 → 基本可以确定卡住了
两者都记录，但熔断只看**连续**次数。单看累计会误杀前一种。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


def signature(function_name: str, args: Optional[dict]) -> str:
    """动作指纹：工具名 + 参数（排序后序列化，保证同一组参数指纹稳定）。"""
    try:
        payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(args)
    return f"{function_name}({payload})"


@dataclass
class RepeatHit:
    """一次重复检测结果。"""

    signature: str
    total: int  # 该指纹在窗口内累计出现次数
    consecutive: int  # 连续出现次数
    would_trip: bool  # 若启用熔断，"本会触发"
    distinct: int  # 窗口内不同指纹的数量（多样性）
    total_calls: int

    @property
    def diversity(self) -> float:
        """动作多样性 = 不同指纹数 / 总调用数。越低说明越在原地打转。"""
        return (self.distinct / self.total_calls) if self.total_calls else 1.0


class LoopGuard:
    """重复动作检测器（Step 3 只观察，不熔断）。"""

    def __init__(self, *, repeat_threshold: int = 3, window: int = 12) -> None:
        """
        :param repeat_threshold: 同一个指纹连续出现多少次算"卡住"
        :param window: 多样性统计的滑动窗口大小
        """
        self.repeat_threshold = repeat_threshold
        self.window = window

        self._counts: Dict[str, int] = {}  # 窗口内累计
        self._order: List[str] = []  # 窗口内的指纹序列
        self._consecutive_sig: Optional[str] = None
        self._consecutive: int = 0
        self.total_calls = 0
        self.trips: List[RepeatHit] = []  # 所有"本会熔断"的记录
        self.max_consecutive: int = 0

    def observe(self, function_name: str, args: Optional[dict]) -> RepeatHit:
        """记录一次工具调用并返回重复检测结果。"""
        sig = signature(function_name, args)
        self.total_calls += 1

        # 1.滑动窗口
        self._order.append(sig)
        self._counts[sig] = self._counts.get(sig, 0) + 1
        if len(self._order) > self.window:
            dropped = self._order.pop(0)
            self._counts[dropped] -= 1
            if self._counts[dropped] <= 0:
                self._counts.pop(dropped, None)

        # 2.连续计数
        if sig == self._consecutive_sig:
            self._consecutive += 1
        else:
            self._consecutive_sig = sig
            self._consecutive = 1
        self.max_consecutive = max(self.max_consecutive, self._consecutive)

        would_trip = self._consecutive >= self.repeat_threshold
        hit = RepeatHit(
            signature=sig,
            total=self._counts.get(sig, 1),
            consecutive=self._consecutive,
            would_trip=would_trip,
            distinct=len(self._counts),
            total_calls=self.total_calls,
        )
        # 只在"首次达到阈值"时记录，避免同一次卡死被记很多遍
        if would_trip and self._consecutive == self.repeat_threshold:
            self.trips.append(hit)
        return hit

    def reset_after_step(self) -> None:
        """进入新的计划步骤时重置连续计数。

        为什么需要：跨步骤的"连续"没有意义 —— 第 1 步和第 3 步都调了
        `read_file(同一个文件)` 是正常行为，不该算作连续重复。
        """
        self._consecutive_sig = None
        self._consecutive = 0

    def report(self) -> Dict[str, float]:
        """汇总指标（进 TaskResult，供 Step 4 聚合）。"""
        diversity = (len(self._counts) / self.total_calls) if self.total_calls else 1.0
        return {
            "tool_calls": float(self.total_calls),
            "max_consecutive_repeat": float(self.max_consecutive),
            "repeat_trips": float(len(self.trips)),
            "action_diversity": round(diversity, 4),
        }
