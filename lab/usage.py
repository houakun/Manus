#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用量与成本计量模型。

为什么单独一层：
- handoff 体检第 4 项（记录 token/成本）和第 8 节的指标模板（tokens/任务、$/任务）
  都要求这个数据，而且要求"口径统一"；
- Step 4 要做 A/B 对比（旧 loop vs 新 loop），两边**必须是同一把尺子**，
  所以计量逻辑只能有一份。

设计取舍：
- 不引入 tiktoken 之类的本地分词器：只统计模型返回的 usage 字段（权威值），
  本地估算值在不同模型上误差很大，反而会污染对比结论；
- 价格表内置常见模型，查不到时成本记 0 并在报告里标注，**不猜**。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from pydantic import BaseModel, Field

# 价格表：(输入 $/百万token, 输出 $/百万token)
# 说明：这是"写死的配置"，Step 4 会挪到独立配置文件；查不到就返回 None。
PRICING: Dict[str, Tuple[float, float]] = {
    "deepseek-chat": (0.28, 0.42),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def price_of(model_name: str) -> Optional[Tuple[float, float]]:
    """按模型名查价格，支持前缀匹配（如 deepseek-chat-0324）。"""
    if not model_name:
        return None
    if model_name in PRICING:
        return PRICING[model_name]
    for key, value in PRICING.items():
        if model_name.startswith(key):
            return value
    return None


class Usage(BaseModel):
    """一次任务的用量汇总。"""

    llm_calls: int = 0  # LLM 调用次数
    llm_errors: int = 0  # LLM 调用失败次数（重试前计数）
    prompt_tokens: int = 0  # 累计输入 token
    completion_tokens: int = 0  # 累计输出 token
    total_tokens: int = 0  # 累计总 token
    tool_calls: int = 0  # 工具调用次数
    llm_latency_ms: int = 0  # LLM 累计耗时（毫秒，用于算"时间都花在哪"）

    def add_llm_call(self, raw_usage: Optional[dict], latency_ms: int = 0) -> None:
        """累加一次 LLM 调用的用量。

        raw_usage 直接来自 OpenAI 兼容接口的 response.usage.model_dump()，
        所以字段名用标准名 prompt_tokens/completion_tokens/total_tokens。
        """
        self.llm_calls += 1
        self.llm_latency_ms += max(0, int(latency_ms or 0))
        if not raw_usage:
            # 有些兼容服务不返回 usage。此时不猜，只记调用次数。
            return
        self.prompt_tokens += int(raw_usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(raw_usage.get("completion_tokens") or 0)
        total = raw_usage.get("total_tokens")
        self.total_tokens += int(total) if total is not None else 0

    def cost_usd(self, model_name: str) -> float:
        """按价格表估算本次任务的美元成本。查不到价格时返回 0.0。"""
        price = price_of(model_name)
        if not price:
            return 0.0
        in_price, out_price = price
        return round(
            self.prompt_tokens / 1_000_000 * in_price
            + self.completion_tokens / 1_000_000 * out_price,
            6,
        )
