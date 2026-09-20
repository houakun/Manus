#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 录制回放代理。

== 四种模式，每一种解决一个**不同**的问题 ==

| 模式 | 联网？ | 未命中时 | 解决什么问题 |
|---|---|---|---|
| `off` | 是 | — | 不做缓存（默认，行为与改造前逐字节一致） |
| `record` | **总是** | — | 录一次真跑；顺带测出**服务端非确定性** |
| `reuse` | 未命中才联 | 联网并写入 | 改了 prompt 只想重跑受影响的那几次调用 |
| `replay` | **从不** | 直接失败 | 离线复现一次失败；无需 API Key；可进 CI |

**为什么 `record` 要"总是联网"而不是"读穿"**：
如果 `record` 在命中时直接返回缓存，那么同一个请求永远不会被问第二次，
「同一个请求服务端会不会给不同回答」这个问题就**永远测不到** ——
而那正是"温度设成 0 也消不掉的噪声"的直接度量。
所以录制的代价换来一个副产品：**任何一次 record 运行都在免费测量服务端非确定性**。

**为什么 `replay` 未命中必须失败，而不是偷偷联网**：
偷偷联网会让"离线复现"变成"看起来离线、其实条件已经变了"——
回放出来的差异你无法归因（是代码变了，还是这次请求刚好走了一条新路径？）。
未命中就是**信息**：它说明本次运行的请求集合与录制时不同，
而那正是"你的改动确实改变了上下文"的证据。

== 它放在哪一层 ==
    CountingLLM( CachedLLM( OpenAILLM ) )
                              ^ 只把最内层的"网络调用"换成"可能查缓存"
这样 token/成本/span 的采集口径**一点没变**（仍然是 CountingLLM 干的），
回放出来的用量是从缓存里带出来的原始 usage，所以"回放一次要花多少钱"也算得出来。
"""

from __future__ import annotations

import copy
import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from lab.replay.cache import CacheStats, LLMCache, cache_key


class ReplayMode(str, Enum):
    """回放模式。"""

    OFF = "off"
    RECORD = "record"
    REUSE = "reuse"
    REPLAY = "replay"

    @property
    def needs_network(self) -> bool:
        """这种模式**可能**联网（用于决定是否强制要求 API Key）。"""
        return self is not ReplayMode.REPLAY

    @property
    def guaranteed_free(self) -> bool:
        """这种模式**保证**不花钱（只有严格回放能保证）。"""
        return self is ReplayMode.REPLAY


class ReplayMiss(RuntimeError):
    """严格回放模式下缓存未命中。

    刻意不继承 `ValueError` / `ConnectionError`：SUT 的 `_invoke_llm` 会重试
    "看起来像暂态"的异常，而这里重试**毫无意义**（缓存里没有就是没有）。
    """


def mode_from_spec(spec: Optional[str]) -> ReplayMode:
    """解析模式字符串（命令行 > 环境变量 LAB_LLM_REPLAY > off）。

    别名是有必要的：`--replay` 这个名字天然会被理解成"我要回放"，
    而它也接受 `on` / `1` 之类的写法。解析失败要**报错而不是静默降级** ——
    静默降级会让一次"以为在离线回放"的运行真的去联网烧钱。
    """
    if spec is None or str(spec).strip() == "":
        return ReplayMode.OFF
    text = str(spec).strip().lower()
    aliases = {
        "off": ReplayMode.OFF,
        "no": ReplayMode.OFF,
        "0": ReplayMode.OFF,
        "record": ReplayMode.RECORD,
        "rec": ReplayMode.RECORD,
        "reuse": ReplayMode.REUSE,
        "on": ReplayMode.REUSE,
        "1": ReplayMode.REUSE,
        "replay": ReplayMode.REPLAY,
        "yes": ReplayMode.REPLAY,
    }
    mode = aliases.get(text)
    if mode is None:
        raise ValueError(
            f"未知的回放模式 {spec!r}；可选：off / record / reuse / replay"
        )
    return mode


class CachedLLM:
    """LLM 协议代理：按模式查/写缓存。

    只转发 LLM 协议（invoke + 三个只读属性），不碰任何 SUT 逻辑。
    """

    def __init__(
            self,
            inner: Any,
            cache: LLMCache,
            *,
            mode: ReplayMode = ReplayMode.RECORD,
            ignore_volatile: bool = False,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._mode = ReplayMode(mode)
        # 算键前是否抹平 UUID / 时间戳。
        # 默认 False：先看到"未命中"和它的诊断，再决定要不要放宽 ——
        # 而不是一开始就放宽，把"你的改动确实改变了上下文"这个信号也一起抹掉。
        self._ignore_volatile = bool(ignore_volatile)
        self.stats = CacheStats(mode=self._mode.value, cache_path=str(cache.path))
        self.stats.volatile_normalized = self._ignore_volatile
        # 最近一次调用的缓存判定（hit / miss / write）。
        # CountingLLM 会把它写进 llm span —— 这是"这次运行确实离线"的取证依据。
        self.last_status: str = ""

    @property
    def mode(self) -> ReplayMode:
        return self._mode

    @property
    def ignore_volatile(self) -> bool:
        return self._ignore_volatile

    def _key(self, messages: Any, tools: Any, tool_choice: Any, response_format: Any,
             *, normalize_volatile: bool) -> str:
        return cache_key(
            model=self._inner.model_name,
            temperature=self._inner.temperature,
            max_tokens=self._inner.max_tokens,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            normalize_volatile=normalize_volatile,
        )

    async def invoke(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        key = self._key(messages, tools, tool_choice, response_format, normalize_volatile=False)
        normalized_key = self._key(messages, tools, tool_choice, response_format,
                                   normalize_volatile=True)

        # ---- 1.先看缓存（reuse / replay 都会读）----
        if self._mode in (ReplayMode.REUSE, ReplayMode.REPLAY):
            cached = self._cache.get(key)
            if cached is not None:
                self.stats.hits += 1
                self._cache.note_served(key)
                self.last_status = "hit"
                return self._replay_response(cached)
            # ---- 1.1 宽松回遯：按副键再找一次 ----
            # 为什么默认不开，但**允许事后开**：
            # 键是录制时算的。你通常在"发现未命中"之后才知道需要这个开关，
            # 而那时不该再让你重新录一遍（重新花钱）。副键在录制时就一并存下了，
            # 所以这里能回遯命中。
            if self._ignore_volatile:
                loose, matches = self._cache.get_loose(normalized_key)
                if loose is not None:
                    self.stats.hits += 1
                    self.stats.loose_hits += 1
                    self.last_status = "hit-loose"
                    return self._replay_response(loose)
            if self._mode is ReplayMode.REPLAY:
                self.stats.misses += 1
                self.last_status = "miss"
                # 诊断必须在这里做（手里还有本次的 messages）：
                # 它把"缓存里没有"变成"第 3 条消息里的 UUID 不一样"，
                # 否则人会去怀疑自己刚改的那行代码。
                self._diagnose(messages, tools)
                raise ReplayMiss(
                    "缓存未命中（严格回放模式不联网）。"
                    "这说明本次运行的请求集合与录制时不同 —— 先确认改动是否真的只影响你以为的那部分；"
                    "要用联网补齐请改用 --replay reuse，要重新录制请用 --replay record。"
                )

        # ---- 2.联网（off / record / reuse 未命中）----
        started = time.monotonic()
        response = await self._inner.invoke(
            messages=messages, tools=tools,
            response_format=response_format, tool_choice=tool_choice,
        )
        self.stats.writes += 1
        self.last_status = "write"

        if self._mode in (ReplayMode.RECORD, ReplayMode.REUSE):
            conflicts = self._cache.put(
                key, response,
                model=self._inner.model_name,
                temperature=self._inner.temperature,
                messages=messages,
                tools=tools,
                volatile_normalized=self._ignore_volatile,
                normalized_key=normalized_key,
            )
            self.stats.conflicts += conflicts

        self._account(response, latency_ms=int((time.monotonic() - started) * 1000))
        return response

    def _diagnose(self, messages: Any, tools: Any) -> None:
        """记录一条"为什么未命中"的人话诊断（去重，同一原因只记一次）。"""
        try:
            report = self._cache.diagnose_miss(messages, tools)
        except Exception as exc:  # noqa: BLE001
            # 诊断本身是**附加信息**，它绝不能成为新的失败源
            report = f"未命中诊断自身出错（不影响回放判定）：{type(exc).__name__}: {exc}"
        if report and report not in self.stats.miss_diagnosis:
            self.stats.miss_diagnosis.append(report)

    def _replay_response(self, cached: Dict[str, Any]) -> Dict[str, Any]:
        """回放一条缓存响应。

        **刻意把 `_latency_ms` 归零**：如果原样带回录制时的耗时，
        报告里的"耗时"会看起来像一次真实的快速运行，而实际上这次运行
        **根本没有发生任何模型推理**。耗时是最容易被误读成
        "我们把它优化快了"的指标，所以宁可把它清零并单独记等价量。
        token 与成本则原样带上 —— 那是"这次回放等价于花了多少钱"，
        是"先回放看看要花多少"这个用法的基础。
        """
        payload = copy.deepcopy(cached)
        recorded_latency = int(payload.get("_latency_ms") or 0)
        payload["_latency_ms"] = 0
        self._account(payload, latency_ms=recorded_latency)
        return payload

    def _account(self, response: Dict[str, Any], *, latency_ms: int) -> None:
        """累计等价成本/耗时（用于回答"这次回放等价于花了多少"）。"""
        self.stats.equivalent_latency_ms += max(0, int(latency_ms or 0))
        usage = response.get("_usage") or {}
        if not usage:
            return
        from lab.usage import Usage

        probe = Usage()
        probe.add_llm_call(usage, 0)
        self.stats.equivalent_cost_usd += probe.cost_usd(self._inner.model_name)
        self.stats.equivalent_cost_usd = round(self.stats.equivalent_cost_usd, 6)

    def finalize(self) -> CacheStats:
        """收口：按模式判定"实际消费"。

        `off` / `record` / `reuse` 是真的付了钱（只要有 writes）；
        `replay` 一次网络调用都没发生 → 实际消费恒为 0。
        """
        if self._mode is ReplayMode.REPLAY:
            self.stats.actual_spend_usd = 0.0
        else:
            self.stats.actual_spend_usd = 0.0  # 由 run_task 用 Usage 的权威值覆盖
        return self.stats

    # --- LLM 协议的只读属性，原样转发 ---

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def temperature(self) -> float:
        return self._inner.temperature

    @property
    def max_tokens(self) -> int:
        return self._inner.max_tokens


def resolve_mode(
        explicit: Optional[str] = None,
        *,
        env: Optional[Dict[str, str]] = None,
) -> ReplayMode:
    """模式解析：显式参数 > `LAB_LLM_REPLAY` > off。"""
    source = env if env is not None else dict(os.environ)
    return mode_from_spec(explicit if explicit is not None else source.get("LAB_LLM_REPLAY"))


def resolve_cache_path(
        explicit: Optional[Path] = None,
        *,
        env: Optional[Dict[str, str]] = None,
) -> Path:
    """缓存路径解析：显式参数 > `LAB_LLM_CACHE` > `lab/runs/llm_cache.db`。"""
    source = env if env is not None else dict(os.environ)
    if explicit is not None:
        return Path(explicit)
    raw = source.get("LAB_LLM_CACHE")
    if raw:
        return Path(raw)
    from lab.replay.cache import default_cache_path

    return default_cache_path()


def resolve_ignore_volatile(
        explicit: Optional[bool] = None,
        *,
        env: Optional[Dict[str, str]] = None,
) -> bool:
    """是否在算键前抹平易变字段（UUID/时间戳）。 `LAB_LLM_REPLAY_IGNORE_VOLATILE=1`。"""
    if explicit is not None:
        return bool(explicit)
    source = env if env is not None else dict(os.environ)
    return str(source.get("LAB_LLM_REPLAY_IGNORE_VOLATILE", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def summarize_cache_stats(stats: CacheStats) -> str:
    """一行摘要（CLI 输出用）。"""
    return json.dumps(stats.model_dump(mode="json"), ensure_ascii=False)
