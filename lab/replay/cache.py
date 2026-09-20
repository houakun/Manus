#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 响应缓存（SQLite）—— 「可回放」的存储层。

== 为什么键必须按**请求内容**算，而不是按调用序号 ==
最省事的做法是"第 1 次调用 → 第 1 条响应"。它能跑，但一旦上游有任何变化
（改了一个 prompt、沙箱路径不同、工具顺序变了），整个序列就静默错位 ——
你会拿到**属于另一个请求的响应**，而回放出来的轨迹看起来完全正常。

按请求内容算键（model+temperature+max_tokens+messages+tools+tool_choice+response_format）
带来三个性质，每一个都是"序号键"给不了的：
1. **错位会变成显式的未命中**，而不是安静的错误答案；
2. **部分命中**可以工作：只改了 system prompt 时，只有受影响的那几次调用未命中，
   其余照旧命中（这就是 `reuse` 模式能省钱的原理）；
3. 同一个键被记录到**不同响应**时，那是**服务端非确定性**的直接证据 ——
   这是温度设为 0 也消不掉的东西，见 `stats()` 的 `nondeterminism`。

== 为什么用完整 sha256 而不是截断的短指纹 ==
`trace/span.py` 的 `_digest` 截断到 12 位十六进制（48 bit），用于"版本标识"足够。
但这里它是**主键**：缓存到十万条时，48 bit 的碰撞概率约 2e-5。
碰撞的后果是**悄悄返回另一个请求的响应** —— 正是本项目最忌讳的那类错误。
多存 52 个字符换掉这个风险，值得。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from lab.usage import price_of

# ==================== 易变字段（回放的死敌）====================
# 实测发现：SUT 在提示词里放了**随机 UUID**（计划步骤的 id）。
# 后果是同一个任务的两次运行，请求集合**在字节层面必然不同** →
# 严格回放永远未命中，"离线复现"变成不可能。
#
# 这类"与语义无关但每次都变"的字段是回放的头号障碍，而且它很隐蔽：
# 未命中的表现只是"缓存里没有"，看起来像是你自己的改动导致的。
# 所以这里做两件事：
#   1. `describe_volatile_diff()` 把"差异仅仅是 UUID"这个结论**说出来**；
#   2. `normalize_volatile=True` 时在**算键之前**把 UUID 抹平（显式、可选）。
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# 其他常见的易变量：ISO 时间戳、长十六进制串（hash / token）
_VOLATILE_PATTERNS = [
    (_UUID_RE, "<uuid>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"), "<time>"),
    (re.compile(r"\b[0-9a-f]{32,}\b"), "<hex>"),
]


def strip_volatile(value: Any) -> Any:
    """递归地把字符串里的易变字段换成占位符（用于"这是不是只有 UUID 不同"的判断）。"""
    if isinstance(value, str):
        out = value
        for pattern, token in _VOLATILE_PATTERNS:
            out = pattern.sub(token, out)
        return out
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_volatile(item) for key, item in value.items()}
    return value

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key         TEXT PRIMARY KEY,
    model             TEXT,
    temperature       REAL,
    response          TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    latency_ms        INTEGER,
    recorded_at       TEXT,
    served            INTEGER DEFAULT 0
);
-- 冲突表：同一个请求键记录到了**不同的响应**。
-- 这不是 bug，而是"服务端在这个请求上不满足确定性"的实测证据。
-- 单独一张表（而不是在 llm_cache 上加一列）是为了保留**每一种**不同的响应，
-- 只留一个"有冲突"的布尔量会把"差多少"这个最有价值的信息丢掉。
CREATE TABLE IF NOT EXISTS llm_conflicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key         TEXT NOT NULL,
    response          TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    seen_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_conflicts_key ON llm_conflicts(cache_key);
CREATE INDEX IF NOT EXISTS idx_llm_cache_model ON llm_cache(model);
"""

#   `request` 列：存**录制时的请求体**（有截断），只为了一个目的 ——
#   回放未命中时，能告诉你"到底哪一条消息不一样"。
#   没有它，未命中只有一个干巴巴的"缓存里没有"，而你会去怀疑自己刚改的那行代码，
#   真实原因可能是 SUT 在提示词里放了随机 UUID（实测就是这个）。
_CACHE_COLUMNS = {
    "request": "TEXT",
    "system_digest": "TEXT",
    "messages": "INTEGER",
    "volatile_normalized": "INTEGER",
    # 抹平易变字段后的**副键**。
    #
    # 为什么要存一个副键（而不是只在查询时抹平）：
    # 键是**录制时**算出来的。如果录制时没抹平、回放时才抹平，两者算出的键不同，
    # 一样命中不了 —— 于是"发现 UUID 问题"之后必须**重新录制**（重新花钱）。
    # 存下副键之后，回放可以按副键回遯查找：
    # 实测发现问题的那一刻就是想要这个开关的那一刻，而那一刻不该再让你掏一次钱。
    "normalized_key": "TEXT",
}

_MAX_REQUEST_CHARS = 40000
_MAX_MESSAGE_CHARS = 4000


def _canonical(value: Any) -> str:
    """把任意结构序列化成**稳定**字符串（键排序、中文不转义）。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _system_digest(messages: Any) -> str:
    """system prompt 的指纹（用于"找同类请求"做未命中诊断）。"""
    for message in (messages or []):
        if isinstance(message, dict) and message.get("role") == "system":
            return hashlib.sha256(str(message.get("content", "")).encode("utf-8")).hexdigest()[:16]
    return ""


def _message_text(message: Any) -> str:
    return _canonical(message)


def _common_prefix(left: List[str], right: List[str]) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def cache_key(
        *,
        model: str,
        temperature: Any,
        max_tokens: Any,
        messages: Any,
        tools: Any = None,
        tool_choice: Any = None,
        response_format: Any = None,
        normalize_volatile: bool = False,
) -> str:
    """请求指纹：**凡是会影响模型输出的入参都必须进键**。

    少放一个入参会怎样：两段实际不同的请求算成同一个键 →
    回放时返回错误的响应，而轨迹看起来毫无异常。
    所以这里刻意把 `max_tokens` 也放进去（它截断输出、会改变响应内容），
    尽管它在多数实验里是常量。

    :param normalize_volatile: 是否在算键前抹平 UUID / 时间戳 / 长十六进制串。
        **默认 False（精确匹配）**：先看到"未命中"，再决定要不要放宽 ——
        而不是一开始就放宽、把"你的改动确实改变了上下文"这个信号也一起抹掉。
    """
    if normalize_volatile:
        messages = strip_volatile(messages)
        tools = strip_volatile(tools)
    payload = _canonical({
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "response_format": response_format,
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bounded_request(messages: Any, tools: Any = None) -> str:
    """把请求压成可存储的诊断副本（逐条截断 + 总量封顶）。

    为什么要截断而不是原样存：缓存里可能有上万条请求，
    而每条上下文动辄几十 KB —— 不封顶会把磁盘吃光，
    而被吃光的缓存库会以"磁盘写满"的形式让整套评测挂掉。
    诊断只需要看到**差异点附近**的文字，截断完全够用。
    """
    trimmed: List[Any] = []
    for message in (messages or []):
        if isinstance(message, dict):
            item = dict(message)
            content = item.get("content")
            if isinstance(content, str) and len(content) > _MAX_MESSAGE_CHARS:
                item["content"] = content[:_MAX_MESSAGE_CHARS] + "…(truncated)"
            trimmed.append(item)
        else:
            trimmed.append(str(message)[:_MAX_MESSAGE_CHARS])
    payload = {"messages": trimmed, "tools": tools}
    text = _canonical(payload)
    return text[:_MAX_REQUEST_CHARS]


class CacheStats(BaseModel):
    """缓存层面的统计（落库 + 进报告）。"""

    mode: str = "off"
    hits: int = 0
    misses: int = 0
    writes: int = 0
    conflicts: int = 0
    equivalent_cost_usd: float = 0.0
    equivalent_latency_ms: int = 0
    cache_path: str = ""
    # 实际为这次运行付出去的钱。回放模式下恒为 0 ——
    # 没有这个字段，报告里的"成本"会被读成"这次花了这么多"。
    actual_spend_usd: float = 0.0
    # 是否在算键时抹平了易变字段（UUID/时间戳）。开启后回放更稳，
    # 但也更宽 —— 必须让人看得到这件事发生过。
    volatile_normalized: bool = False
    # 按副键（抹平易变字段后）命中的次数。
    # 单独立一个数：它说明这些命中是"宽松"的，报告里要能区分。
    loose_hits: int = 0
    # 未命中的**诊断**（人话，直接说明是哪一条消息不一样）
    miss_diagnosis: List[str] = Field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return self.hits + self.misses + self.writes

    @property
    def offline(self) -> bool:
        """本次运行是否**完全**没有联网（严格回放的验收条件）。"""
        return self.mode == "replay" and self.misses == 0

    def describe(self) -> str:
        if self.mode == "off":
            return "LLM 缓存：关闭"
        return (
            f"LLM 缓存[{self.mode}]：命中 {self.hits} / 未命中 {self.misses} / 写入 {self.writes}"
            f"（等价成本 ${self.equivalent_cost_usd:.4f}，实际消费 ${self.actual_spend_usd:.4f}）"
        )


class LLMCache:
    """SQLite 响应缓存。一个实例 = 一个进程（跨任务复用，因为缓存本来就是跨任务的）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 后加列：老缓存库（改造前建的）得补上，否则写入会报 OperationalError。
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(llm_cache)")}
            for name, ddl in _CACHE_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE llm_cache ADD COLUMN {name} {ddl}")
            # 索引必须在列补好之后建（否则首次迁移会直接报"no such column"）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_cache_norm ON llm_cache(normalized_key)")

    # ==================== 读写 ====================

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """取一条缓存的响应（原始 dict，含 `_usage` 等私有遥测键）。

        为什么连私有键一起存：`CountingLLM` 依赖它们来累计 token 与成本。
        如果回放时没有 `_usage`，回放出来的成本会是 0 ——
        于是"回放一次看看要花多少钱"这件事就永远算不出来。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response FROM llm_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["response"])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def note_served(self, key: str) -> None:
        """记一次命中（用于回答"这条缓存被复用了多少次"）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE llm_cache SET served = COALESCE(served, 0) + 1 WHERE cache_key = ?",
                (key,),
            )

    def get_loose(self, normalized_key: str) -> tuple:
        """按**副键**回遯查找，返回 (响应, 命中的条目数)。

        == 为什么返回条目数而不是直接返回一条 ==
        同一个副键可能对应多条记录（每次运行的 UUID 不同 → 主键不同 → 副键相同）。
        它们**应该**是同一次请求的不同实例。但"应该"不等于"一定"：
        如果这些记录里的响应真的不同，那说明除了 UUID 之外还有别的差异。
        把条目数告诉调用方（并计入 loose_hits），才不至于把异常情况静默吃掉。
        """
        if not normalized_key:
            return None, 0
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT cache_key, response FROM llm_cache
                   WHERE normalized_key = ? ORDER BY recorded_at DESC""",
                (normalized_key,),
            ).fetchall()
        if not rows:
            return None, 0
        try:
            value = json.loads(rows[0]["response"])
        except (TypeError, ValueError):
            return None, len(rows)
        return (value if isinstance(value, dict) else None), len(rows)

    def put(
            self,
            key: str,
            response: Dict[str, Any],
            *,
            model: str = "",
            temperature: Any = None,
            messages: Any = None,
            tools: Any = None,
            volatile_normalized: bool = False,
            normalized_key: str = "",
    ) -> int:
        """写入一条响应，返回**本次是否发现了非确定性**（0/1）。

        `record` 模式的附带收益：因为它是"每次都真的联网"，所以同一个键会被反复写入。
        只要两次的响应不同，就说明**这个请求在服务端不是确定性的** ——
        这是温度 0 也消不掉的那部分噪声，而且我们**一分钱没多花**就测到了它。
        """
        usage = response.get("_usage") or {}
        payload = _canonical(response)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        latency_ms = int(response.get("_latency_ms") or 0)
        now = datetime.now().isoformat(timespec="seconds")
        request = bounded_request(messages, tools) if messages is not None else None
        system_digest = _system_digest(messages)
        message_count = len(messages or [])

        with self._connect() as conn:
            row = conn.execute(
                "SELECT response FROM llm_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO llm_cache
                       (cache_key, model, temperature, response, prompt_tokens,
                        completion_tokens, latency_ms, recorded_at, served,
                        request, system_digest, messages, volatile_normalized, normalized_key)
                       VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                    (key, model, temperature, payload, prompt_tokens,
                     completion_tokens, latency_ms, now,
                     request, system_digest, message_count, 1 if volatile_normalized else 0,
                     normalized_key or ""),
                )
                return 0

            if row["response"] == payload:
                # 同一个响应被记了第二次：这是**确定性的证据**，值得计数，不是冲突
                conn.execute(
                    "UPDATE llm_cache SET served = COALESCE(served, 0) + 1 WHERE cache_key = ?",
                    (key,),
                )
                return 0

            # 同请求不同响应 → 非确定性。先查重，避免同一替代响应被记很多遍
            exists = conn.execute(
                "SELECT 1 FROM llm_conflicts WHERE cache_key = ? AND response = ?",
                (key, payload),
            ).fetchone()
            if exists is not None:
                return 0
            conn.execute(
                """INSERT INTO llm_conflicts
                   (cache_key, response, prompt_tokens, completion_tokens, seen_at)
                   VALUES (?,?,?,?,?)""",
                (key, payload, prompt_tokens, completion_tokens, now),
            )
        return 1

    # ==================== 未命中诊断 ====================

    def diagnose_miss(self, messages: Any, tools: Any = None, *, limit: int = 3) -> str:
        """回放未命中时，找出"最接近的那条录制请求"并指出差异在哪。

        == 为什么这个功能很重要 ==
        未命中本身只有一个信号："缓存里没有"。而人会本能地怀疑**自己刚改的那行代码**。
        实测就踩过：真实原因是 SUT 在提示词里放了随机 UUID（计划步骤 id），
        跟你的改动毫无关系。没有诊断，你会花半天去改一个本来就对的地方。

        做法：在缓存里找**消息条数相同、system prompt 相同**的候选，
        逐条比对，报出第一处不同的消息和两边的开头；
        如果抹掉 UUID/时间戳后就一致，就直接把这个结论说出来。
        """
        if not messages:
            return "未命中：本次请求为空，无法诊断。"
        digest = _system_digest(messages)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT cache_key, request FROM llm_cache
                   WHERE COALESCE(system_digest, '') = ? AND COALESCE(messages, 0) = ?
                   ORDER BY recorded_at DESC LIMIT ?""",
                (digest, len(messages), limit),
            ).fetchall()
        if not rows:
            return (
                f"未命中：缓存里**没有**同类请求（连 system prompt 相同、消息条数 "
                f"{len(messages)} 的都没有）→ 说明 system prompt / 工具集 / 消息条数已变，"
                "不是「某条消息内容不同」这么小的事。"
            )

        new_texts = [_message_text(item) for item in messages]
        best = None
        for row in rows:
            try:
                recorded = json.loads(row["request"] or "{}").get("messages") or []
            except (TypeError, ValueError):
                continue
            common = _common_prefix(new_texts, [_message_text(item) for item in recorded])
            if best is None or common > best[0]:
                best = (common, recorded)
        if best is None:
            return "未命中：缓存里有同类请求，但存的是旧格式，无法比对内容。"

        common, recorded = best
        if common >= len(new_texts) and len(recorded) == len(new_texts):
            # 每条消息的**原文**都不同但只有易变字段不同？不可能到这里；
            # 说明差异在 tools / tool_choice / response_format 上。
            return (
                f"未命中：前 {common} 条消息完全一致 → 差异在 **tools / tool_choice / "
                "response_format**（工具描述变了）。改提示词或工具集后这是预期行为。"
            )

        index = min(common, len(new_texts) - 1, len(recorded) - 1)
        old_text = _message_text(recorded[index])
        new_message = new_texts[index]
        if old_text == new_message:
            return f"未命中：前 {common} 条一致，差异在第 {index + 1} 条之后的长度。"

        prefix = f"未命中：前 {common} 条消息一致，**第 {index + 1} 条**不同。"
        if strip_volatile(old_text) == strip_volatile(new_message):
            return (
                prefix
                + "\n  ✅ **抹掉 UUID / 时间戳之后两边完全一致** —— "
                "这是 SUT 在提示词里放了随机 ID，**不是你的改动造成的**。\n"
                "  处置：加 `--replay-ignore-volatile`（在算键前抹平易变字段，**不需要重新录制**），"
                "或接受这一步不再命中。"
            )
        return (
            prefix
            + f"\n  录制时：{old_text[:160]!r}\n  本次  ：{new_message[:160]!r}"
        )

    # ==================== 统计 ====================

    def stats(self) -> Dict[str, Any]:
        """缓存全局统计。

        `nondeterminism` 是这里最重要的一个数：**同一个请求、同一个温度，
        服务端给过几种不同的回答**。它决定了"重跑一次"到底能带来多少不可控变化，
        也决定了 A/B 实验里"两次跑出的差异"有多大成分只是采样噪声。
        """
        with self._connect() as conn:
            entries = conn.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()["n"] or 0
            served_total = conn.execute(
                "SELECT COALESCE(SUM(served), 0) AS n FROM llm_cache"
            ).fetchone()["n"] or 0
            conflict_keys = conn.execute(
                "SELECT COUNT(DISTINCT cache_key) AS n FROM llm_conflicts"
            ).fetchone()["n"] or 0
            conflict_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM llm_conflicts"
            ).fetchone()["n"] or 0
            by_model = conn.execute(
                """SELECT COALESCE(model, '(未记录)') AS model, COUNT(*) AS n,
                          COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                   FROM llm_cache GROUP BY model ORDER BY n DESC"""
            ).fetchall()
            span_row = conn.execute(
                "SELECT MIN(recorded_at) AS first, MAX(recorded_at) AS last FROM llm_cache"
            ).fetchone()

        equivalent_cost = 0.0
        models: List[Dict[str, Any]] = []
        for row in by_model:
            price = price_of(row["model"])
            cost = 0.0
            if price:
                cost = (
                    row["prompt_tokens"] / 1_000_000 * price[0]
                    + row["completion_tokens"] / 1_000_000 * price[1]
                )
            equivalent_cost += cost
            models.append({
                "model": row["model"],
                "entries": row["n"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "equivalent_cost_usd": round(cost, 6),
                # 价格表没命中时成本记 0 并标注，**不猜**（与 usage.py 同一原则）
                "priced": price is not None,
            })

        return {
            "path": str(self.path),
            "entries": entries,
            "served_total": served_total,
            "conflict_keys": conflict_keys,
            "conflict_responses": conflict_rows,
            "nondeterminism_rate": (conflict_keys / entries) if entries else 0.0,
            "equivalent_cost_usd": round(equivalent_cost, 6),
            "by_model": models,
            "first_recorded_at": span_row["first"] if span_row else None,
            "last_recorded_at": span_row["last"] if span_row else None,
        }


def default_cache_path() -> Path:
    """默认缓存路径：`lab/runs/llm_cache.db`（与 traces.db 同目录，但**分库**）。

    为什么分库：`traces.db` 是"运行记录"，会被 `bench recover`、报告、统计读取；
    缓存是"可安全删除的加速器"，删掉只会变慢，不会丢结论。
    把两者的生命周期分开，人才敢删。
    """
    from lab.bootstrap import ensure_runs_dir

    return ensure_runs_dir() / "llm_cache.db"
