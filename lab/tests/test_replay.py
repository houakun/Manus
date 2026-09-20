#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 录制回放的测试。

== 这个文件要守住的三件事（按重要性）==
1. **回放绝不联网**：严格回放未命中时必须失败，而不是偷偷联网。
   如果这条破了，一个"以为在离线复现"的运行会真的花钱，而且结论不可归因。
2. **键按请求内容算**：少放一个入参 → 两段不同请求算成同一个键 →
   回放时**静默返回错误的响应**，而轨迹看起来毫无异常。
   这类错误不会崩，只会让所有结论都错，所以必须有测试钉住。
3. **成本口径不混**：回放运行里的 `cost_usd` 是**等价量**，实际消费必须是 0。
   两者混在一起会把"回放"读成"花了这么多钱"。
"""

from __future__ import annotations

import pytest

from lab.replay.cache import LLMCache, cache_key
from lab.replay.llm import CachedLLM, ReplayMiss, ReplayMode, mode_from_spec
from lab.tests.fakes import ScriptedLLM, content, json_content


def _cache(tmp_path) -> LLMCache:
    return LLMCache(tmp_path / "cache.db")


# ==================== 1. 键的稳定性 ====================

def test_cache_key_is_stable_and_order_insensitive():
    """同样的请求 → 同样的键（dict 键序不影响）。"""
    key_a = cache_key(model="m", temperature=0.0, max_tokens=100,
                      messages=[{"role": "user", "content": "hi"}],
                      tools=[{"name": "t", "x": 1, "y": 2}])
    key_b = cache_key(model="m", temperature=0.0, max_tokens=100,
                      messages=[{"role": "user", "content": "hi"}],
                      tools=[{"y": 2, "x": 1, "name": "t"}])
    assert key_a == key_b


@pytest.mark.parametrize("changed", [
    {"model": "m2"},
    {"temperature": 0.7},
    {"max_tokens": 200},
    {"messages": [{"role": "user", "content": "HI"}]},
    {"tools": [{"name": "other"}]},
    {"tool_choice": "required"},
    {"response_format": {"type": "json_object"}},
])
def test_cache_key_changes_with_every_input(changed):
    """**每一个**会影响输出的入参都必须进键。

    这条是参数化的：以后加新入参（比如 top_p）时，如果只加进 invoke 却没加进键，
    这里会立刻挂 —— 而不是等到某次回放给出一个属于另一个请求的答案。
    """
    base = dict(model="m", temperature=0.0, max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
                tools=[{"name": "t"}], tool_choice=None, response_format=None)
    assert cache_key(**base) != cache_key(**{**base, **changed})


# ==================== 2. 四种模式的语义 ====================

async def test_record_mode_always_hits_network_and_writes(tmp_path):
    """record 模式**总是**联网（这是它能顺带测非确定性的原因）。"""
    cache = _cache(tmp_path)
    inner = ScriptedLLM([content("a"), content("b")])
    llm = CachedLLM(inner, cache, mode=ReplayMode.RECORD)

    first = await llm.invoke([{"role": "user", "content": "q"}])
    second = await llm.invoke([{"role": "user", "content": "q"}])

    assert len(inner.calls) == 2, "record 模式必须每次都真的问模型"
    assert first["content"] == "a" and second["content"] == "b"
    assert llm.stats.writes == 2
    assert cache.stats()["entries"] == 1, "同一个请求键只应有一个主条目"


async def test_record_mode_detects_server_nondeterminism(tmp_path):
    """同一个请求拿到不同回答 → 记为冲突（这就是"服务端非确定性"的度量）。

    这个测量的价值：温度设成 0 也消不掉这部分噪声，它决定了 A/B 的噪声地板。
    而且它是 record 模式的**副产品**，不需要额外的钱。
    """
    cache = _cache(tmp_path)
    llm = CachedLLM(ScriptedLLM([content("a"), content("b")]), cache, mode=ReplayMode.RECORD)

    await llm.invoke([{"role": "user", "content": "q"}])
    await llm.invoke([{"role": "user", "content": "q"}])

    assert llm.stats.conflicts == 1
    stats = cache.stats()
    assert stats["conflict_keys"] == 1
    assert stats["conflict_responses"] == 1
    assert stats["nondeterminism_rate"] == 1.0


async def test_replay_mode_never_touches_network_and_zeroes_latency(tmp_path):
    """严格回放：命中即返回，一次都不调内层；耗时归零、usage 保留。"""
    cache = _cache(tmp_path)
    recorded = ScriptedLLM([content("cached answer")], model_name="deepseek-chat",
                           prompt_tokens=42, completion_tokens=7, latency_ms=999)
    await CachedLLM(recorded, cache, mode=ReplayMode.RECORD).invoke(
        [{"role": "user", "content": "q"}]
    )

    # 内层 LLM 的 model_name / temperature / max_tokens 都会进键，
    # 所以回放用的替身必须与录制时**完全一致** —— 否则键不同、必然未命中。
    offline = ScriptedLLM([], model_name="deepseek-chat")  # 空脚本：一旦被调就 AssertionError
    llm = CachedLLM(offline, cache, mode=ReplayMode.REPLAY)
    result = await llm.invoke([{"role": "user", "content": "q"}])

    assert offline.calls == [], "严格回放绝不允许联网"
    assert result["content"] == "cached answer"
    # 耗时归零：这次运行根本没有发生推理，带着录制时的耗时会让人误以为"变快了"
    assert result["_latency_ms"] == 0
    assert llm.stats.equivalent_latency_ms == 999
    # usage 必须带出来，否则"回放一次等价于花多少钱"永远算不出来
    assert result["_usage"]["prompt_tokens"] == 42
    # 价格表命中时等价成本必须 > 0；否则"先回放看看要花多少"这个用法就不成立
    assert llm.stats.equivalent_cost_usd > 0
    assert llm.stats.hits == 1


async def test_equivalent_cost_is_zero_when_price_table_misses(tmp_path):
    """价格表查不到 → 成本记 0 并**不猜**（与 usage.py 同一原则）。

    这条单独立一个测试，是为了把它与"回放成本算错了"区分开：
    0 在这里是**有意的**，而不是 bug。
    """
    cache = _cache(tmp_path)
    llm = CachedLLM(ScriptedLLM([content("x")], model_name="unknown-model-xyz"),
                    cache, mode=ReplayMode.RECORD)
    await llm.invoke([{"role": "user", "content": "q"}])
    assert llm.stats.equivalent_cost_usd == 0.0


async def test_replay_mode_miss_raises_instead_of_silently_going_online(tmp_path):
    """未命中必须**失败**，不能偷偷联网 —— 否则"离线复现"是假的。"""
    cache = _cache(tmp_path)
    inner = ScriptedLLM([content("should not be used")])
    llm = CachedLLM(inner, cache, mode=ReplayMode.REPLAY)

    with pytest.raises(ReplayMiss):
        await llm.invoke([{"role": "user", "content": "never recorded"}])

    assert inner.calls == []
    assert llm.stats.misses == 1
    assert llm.last_status == "miss"


async def test_reuse_mode_is_read_through(tmp_path):
    """reuse：命中就不联网（省钱的原理）；未命中才联网并写入。"""
    cache = _cache(tmp_path)
    first_inner = ScriptedLLM([content("v1")])
    await CachedLLM(first_inner, cache, mode=ReplayMode.RECORD).invoke(
        [{"role": "user", "content": "q"}]
    )

    hit_inner = ScriptedLLM([])
    llm = CachedLLM(hit_inner, cache, mode=ReplayMode.REUSE)
    assert (await llm.invoke([{"role": "user", "content": "q"}]))["content"] == "v1"
    assert hit_inner.calls == []

    # 换了一个请求 → 未命中 → 联网并写入
    miss_inner = ScriptedLLM([content("v2")])
    llm2 = CachedLLM(miss_inner, cache, mode=ReplayMode.REUSE)
    assert (await llm2.invoke([{"role": "user", "content": "q2"}]))["content"] == "v2"
    assert len(miss_inner.calls) == 1
    assert llm2.stats.writes == 1


async def test_partial_hit_when_only_one_prompt_changed(tmp_path):
    """只改了一个 prompt → 只有受影响的那几次调用未命中。

    这正是 reuse 模式存在的理由：改提示词时不必为没变的调用重复付费。
    """
    cache = _cache(tmp_path)
    await CachedLLM(ScriptedLLM([content("s1"), content("s2")]), cache,
                    mode=ReplayMode.RECORD).invoke([{"role": "system", "content": "sys-OLD"}])
    await CachedLLM(ScriptedLLM([content("u")]), cache,
                    mode=ReplayMode.RECORD).invoke([{"role": "user", "content": "same"}])

    inner = ScriptedLLM([content("s1-new")])
    llm = CachedLLM(inner, cache, mode=ReplayMode.REUSE)
    await llm.invoke([{"role": "system", "content": "sys-NEW"}])   # 未命中
    await llm.invoke([{"role": "user", "content": "same"}])        # 命中

    assert len(inner.calls) == 1, "只有变了的那次才该联网"
    assert (llm.stats.hits, llm.stats.writes) == (1, 1)


# ==================== 3. 模式解析 ====================

@pytest.mark.parametrize("spec,expected", [
    (None, ReplayMode.OFF),
    ("", ReplayMode.OFF),
    ("off", ReplayMode.OFF),
    ("record", ReplayMode.RECORD),
    ("reuse", ReplayMode.REUSE),
    ("on", ReplayMode.REUSE),
    ("replay", ReplayMode.REPLAY),
])
def test_mode_from_spec(spec, expected):
    assert mode_from_spec(spec) is expected


def test_mode_from_spec_rejects_unknown_instead_of_silently_disabling():
    """拼错的模式必须报错。

    静默降级成 off 的后果：一次"以为在离线回放"的运行真的联网烧钱，
    而且你事后才知道。
    """
    with pytest.raises(ValueError):
        mode_from_spec("replaay")


def test_only_strict_replay_is_guaranteed_free():
    assert ReplayMode.REPLAY.guaranteed_free and not ReplayMode.REPLAY.needs_network
    for mode in (ReplayMode.OFF, ReplayMode.RECORD, ReplayMode.REUSE):
        assert mode.needs_network and not mode.guaranteed_free


# ==================== 4. 端到端：连 SUT 一起跑 ====================

def _script():
    return [
        json_content({
            "title": "回放任务", "goal": "写文件", "language": "中文",
            "steps": [{"description": "写文件"}], "message": "开始",
        }),
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "write_file", "arguments":
                         '{"filepath": "/home/ubuntu/a.txt", "content": "hello"}'},
        }]},
        json_content({"success": True, "attachments": [], "result": "已写入"}),
        json_content({"steps": []}),
        json_content({"message": "任务完成", "attachments": []}),
    ]


def test_end_to_end_record_then_replay_without_api_key(tmp_path, monkeypatch):
    """端到端：录一次 → 看诊断 → 放宽易变字段 → 离线回放成功。

    三条断言对应三个价值：
    1. 严格回放**不联网、不需要 Key**（未命中就失败）；
    2. 未命中时给的是**人话诊断**，不是干巴巴的"缓存里没有"；
    3. 显式放宽后能真正离线复现出同一条轨迹。
    """
    import lab.api as api_module
    from lab.tests.fakes import ScriptedLLM

    cache_path = tmp_path / "llm_cache.db"
    calls = {"n": 0}
    # 录制阶段给一个假 Key（record 模式需要联网权限），回放阶段**清空**它，
    # 以此证明"回放不需要任何凭据"。
    credentials = {"api_key": "fake-key-for-record"}

    def _fake_llm_config():
        from app.domain.models.app_config import LLMConfig

        return LLMConfig(base_url="http://localhost", api_key=credentials["api_key"],
                         model_name="fake-model", temperature=0.0, max_tokens=1024)

    class _Factory:
        """每次 new 一个 OpenAILLM 就换成脚本化 LLM。

        注意统计的是 **invoke 次数**（而不是构造次数）：
        `CachedLLM` 总是会构造内层 LLM（构造不花钱、不联网），
        真正要证明的是"没有发出请求"这件事。
        """

        def __init__(self):
            self.instances = 0
            self.invokes = 0

        def __call__(self, llm_config):
            self.instances += 1
            outer = self

            class _Counting(ScriptedLLM):
                async def invoke(self, *args, **kwargs):
                    outer.invokes += 1
                    calls["n"] += 1
                    return await super().invoke(*args, **kwargs)

            return _Counting(_script(), model_name="fake-model")

    factory = _Factory()
    monkeypatch.setattr(api_module, "OpenAILLM", factory)
    monkeypatch.setattr(api_module, "load_llm_config", lambda **kwargs: _fake_llm_config())

    # ---- 1. 录制 ----
    recorded = api_module.run_task_sync(
        "把 hello 写入 /home/ubuntu/a.txt",
        workspace=tmp_path / "r1",
        replay="record", replay_cache=cache_path,
    )
    assert recorded.ok, recorded.error
    assert recorded.replay["mode"] == "record"
    assert recorded.replay["writes"] > 0
    assert recorded.replay["actual_spend_usd"] == recorded.cost_usd
    sequence = list(recorded.tool_sequence)

    # ---- 2. 严格回放（精确匹配）：**必须失败**，并给出可操作的诊断 ----
    # 实测发现：本项目的 SUT 把随机计划 id（UUID）放进了提示词，
    # 所以同一个任务的两次运行，请求在字节层面**必然不同** → 精确回放必然未命中。
    # 这不是缺点，而是这个功能最有价值的产出：它把"你改坏了"与"上游有隐藏随机性"
    # 区分开了。
    factory.instances = 0
    factory.invokes = 0
    credentials["api_key"] = ""
    strict = api_module.run_task_sync(
        "把 hello 写入 /home/ubuntu/a.txt",
        workspace=tmp_path / "r2",
        replay="replay", replay_cache=cache_path,
    )
    assert not strict.ok
    assert strict.error_type == "replay_miss"
    diagnosis = "\n".join(strict.replay["miss_diagnosis"])
    assert "第 4 条" in diagnosis, diagnosis
    assert "抹掉 UUID / 时间戳之后两边完全一致" in diagnosis, diagnosis
    assert "不是你的改动造成的" in diagnosis, diagnosis
    assert factory.invokes == 0, "严格回放绝不允许联网（哪怕未命中）"

    # ---- 3. 看了诊断之后再显式放宽：抹平易变字段 → 回放成功且全程离线 ----
    factory.instances = 0
    factory.invokes = 0
    replayed = api_module.run_task_sync(
        "把 hello 写入 /home/ubuntu/a.txt",
        workspace=tmp_path / "r3",
        replay="replay", replay_cache=cache_path,
        replay_ignore_volatile=True,
    )

    assert replayed.ok, replayed.error
    assert replayed.replay["mode"] == "replay"
    assert replayed.replay["misses"] == 0, "放宽易变字段后不该再有未命中"
    assert replayed.replay["hits"] > 0
    assert replayed.replay["volatile_normalized"] is True
    assert factory.invokes == 0, "命中缓存的回放同样不该联网"
    # 最关键的断言：回放运行**实际消费为 0**
    assert replayed.replay["actual_spend_usd"] == 0.0
    # 轨迹必须一致（这正是"可回放"的意义）
    assert replayed.tool_sequence == sequence


def test_end_to_end_replay_miss_marks_run_as_replay_miss(tmp_path, monkeypatch):
    """未命中 → 运行失败，且 `error_type` 必须指向**根因**（replay_miss）。

    如果只留 SUT 报出来的 `llm_error`，人会去查模型服务，而真正的原因是"缓存没录全"。
    """
    import lab.api as api_module
    from app.domain.models.app_config import LLMConfig

    monkeypatch.setattr(api_module, "OpenAILLM", lambda cfg: ScriptedLLM([]))
    monkeypatch.setattr(api_module, "load_llm_config", lambda **kwargs: LLMConfig(
        base_url="http://localhost", api_key="", model_name="fake-model",
        temperature=0.0, max_tokens=1024,
    ))

    result = api_module.run_task_sync(
        "任何任务", workspace=tmp_path / "w",
        replay="replay", replay_cache=tmp_path / "empty.db",
    )

    assert not result.ok
    assert result.error_type == "replay_miss"
    assert result.replay["misses"] > 0
    assert any("replay_miss" in line for line in result.error_chain)


def test_llm_span_records_cache_status(tmp_path):
    """llm span 上要能看出"这次响应是缓存来的"。

    这是"本次运行确实离线"的取证依据 —— 事后看轨迹就能确认，
    而不是只能相信命令行参数。
    """
    import asyncio

    from lab.infra.counting_llm import CountingLLM
    from lab.trace.span import TraceRecorder
    from lab.usage import Usage

    cache = _cache(tmp_path)
    asyncio.run(CachedLLM(ScriptedLLM([content("x")]), cache, mode=ReplayMode.RECORD).invoke(
        [{"role": "user", "content": "q"}]
    ))

    recorder = TraceRecorder("t-cache-status")
    inner = CachedLLM(ScriptedLLM([]), cache, mode=ReplayMode.REPLAY)
    llm = CountingLLM(inner, Usage(), sink=recorder)
    asyncio.run(llm.invoke([{"role": "user", "content": "q"}]))

    llm_spans = [s for s in recorder.spans if s.kind == "llm"]
    assert llm_spans and llm_spans[0].attrs.get("cache_status") == "hit"
