#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""lab 的运行配置：以 SUT 的 api/config.yaml 为唯一真源，环境变量可覆盖。

为什么这样设计：
1. **同一把尺子**：Step 4 要做 A/B 对比，两边必须用同一个模型、同一份参数。
   如果 lab 自己再写一套配置，很快就会和 SUT 跑偏，对比结论就不可信了。
2. **可复现优先**（handoff 体检第 3 项）：temperature 默认强制 0.0。
   SUT 的 config.yaml 里是 0.7 —— 那是给人用的产品默认值；
   评测必须固定温度，否则同一个任务每次跑出的步数/工具序列都不同，
   "成功率 78%" 这种结论根本无法复现。
3. 所有字段都允许用 LAB_* 环境变量覆盖，方便在 CI 里换模型/换 key，
   不需要改任何文件（也避免把 key 写进仓库）。

支持的覆盖项：
    LAB_CONFIG_FILE     指定 config.yaml 路径（默认 api/config.yaml）
    LAB_LLM_BASE_URL    LLM 服务地址
    LAB_LLM_API_KEY     LLM 密钥
    LAB_LLM_MODEL       模型名
    LAB_TEMPERATURE     温度（默认 0.0）
    LAB_MAX_TOKENS      单次最大输出 token
    LAB_MAX_ITERATIONS  Agent 单步最大迭代次数
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from lab.bootstrap import SUT_API_DIR, ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.app_config import AgentConfig, LLMConfig  # noqa: E402

DEFAULT_CONFIG_FILE = SUT_API_DIR / "config.yaml"
# 评测默认温度：0.0 = 尽量确定性，保证同一任务可复现
DEFAULT_TEMPERATURE = 0.0


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def load_raw_config(config_file: Optional[Path] = None) -> dict:
    """读取 SUT 的 config.yaml，返回原始 dict（读不到就返回空 dict）。"""
    path = Path(_env_str("LAB_CONFIG_FILE") or config_file or DEFAULT_CONFIG_FILE)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def load_llm_config(
        *,
        temperature: Optional[float] = None,
        config_file: Optional[Path] = None,
) -> LLMConfig:
    """构造 LLM 配置：SUT config.yaml 为基础，环境变量与显式参数优先。"""
    raw = (load_raw_config(config_file) or {}).get("llm_config") or {}

    # 1.优先级：函数参数 > 环境变量 > config.yaml > 代码默认值
    base_url = _env_str("LAB_LLM_BASE_URL") or raw.get("base_url") or "https://api.deepseek.com"
    api_key = _env_str("LAB_LLM_API_KEY") or raw.get("api_key") or ""
    model_name = _env_str("LAB_LLM_MODEL") or raw.get("model_name") or "deepseek-chat"

    # 2.温度：显式参数 > 环境变量 > 评测默认 0.0（注意这里【不】回落到 config.yaml 的 0.7）
    if temperature is not None:
        real_temperature = float(temperature)
    elif _env_str("LAB_TEMPERATURE") is not None:
        real_temperature = float(_env_str("LAB_TEMPERATURE"))
    else:
        real_temperature = DEFAULT_TEMPERATURE

    max_tokens = int(_env_str("LAB_MAX_TOKENS") or raw.get("max_tokens") or 8192)

    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        temperature=real_temperature,
        max_tokens=max_tokens,
    )


def load_agent_config(
        *,
        max_iterations: Optional[int] = None,
        config_file: Optional[Path] = None,
) -> AgentConfig:
    """构造 Agent 通用配置。

    注意 max_iterations 的语义限制（体检第 6 项的缺口）：
    它是 SUT 里 **单次 invoke 内部** 的迭代上限，不是"整个任务"的步数上限。
    任务级预算由 lab 侧控制（Step 1 用 max_seconds，Step 3 会用 budget 中间件）。
    """
    raw = (load_raw_config(config_file) or {}).get("agent_config") or {}

    if max_iterations is not None:
        real_iterations = int(max_iterations)
    else:
        real_iterations = int(_env_str("LAB_MAX_ITERATIONS") or raw.get("max_iterations") or 100)

    return AgentConfig(
        max_iterations=real_iterations,
        max_retries=int(raw.get("max_retries") or 3),
        max_search_results=int(raw.get("max_search_results") or 10),
    )
