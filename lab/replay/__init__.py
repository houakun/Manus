#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 录制回放（补上审计 §1① 里「可回放」这个唯一缺口）。

导出：
    ReplayMode     四种模式（off / record / reuse / replay）
    CachedLLM      LLM 协议代理：按模式查/写缓存
    LLMCache       SQLite 响应缓存
    ReplayMiss     严格回放模式下缓存未命中
    cache_key      请求指纹（**按请求内容**，不按调用序号）
"""

from lab.replay.cache import CacheStats, LLMCache, cache_key, strip_volatile
from lab.replay.llm import (
    CachedLLM,
    ReplayMiss,
    ReplayMode,
    resolve_cache_path,
    resolve_ignore_volatile,
    resolve_mode,
)

__all__ = [
    "CacheStats",
    "CachedLLM",
    "LLMCache",
    "ReplayMiss",
    "ReplayMode",
    "cache_key",
    "resolve_cache_path",
    "resolve_ignore_volatile",
    "resolve_mode",
    "strip_volatile",
]
