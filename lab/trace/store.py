#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""轨迹落盘：SQLite。

== 为什么用 SQLite 而不是 JSON 文件 ==
Step 4 的 harness 要对几十上百次运行做统计（成功率、P95 耗时、tokens/任务、
按 error_type 分组归因）。这些全是聚合查询，SQLite 一行 SQL 就能算；
用 JSON 文件就得把全部 trace 读进内存再手写聚合，且无法增量扩展。

同时保留"可人工阅读"的能力：`lab trace <id>` 会渲染成树形文本，
不需要装任何 GUI 或数据库客户端。

== 写入时机 ==
在任务**结束时一次性写入**（run_task 的 finally 里），而不是每次 span 结束就写：
- 减少 I/O 对耗时指标的干扰（我们正在测的就是耗时）；
- 崩溃/异常路径由 `close_open_spans()` + finally 兜住，不会丢 trace。

表结构：
    tasks  一行 = 一次运行（用于跨运行统计）
    spans  一行 = 一个执行区间（用于单次运行的因果分析）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from lab.trace.span import Span

if TYPE_CHECKING:  # 仅类型提示，避免运行时耦合
    from lab.sut.base import TaskResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    sut_name           TEXT,
    goal               TEXT,
    ok                 INTEGER,
    error              TEXT,
    error_type         TEXT,
    answer             TEXT,
    plan_title         TEXT,
    steps_total        INTEGER,
    steps_done         INTEGER,
    steps_succeeded    INTEGER,
    steps_failed       INTEGER,
    llm_calls          INTEGER,
    llm_errors         INTEGER,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    total_tokens       INTEGER,
    tool_calls         INTEGER,
    cost_usd           REAL,
    elapsed_ms         INTEGER,
    started_at         TEXT,
    workspace          TEXT,
    tool_sequence      TEXT
);

CREATE TABLE IF NOT EXISTS spans (
    span_id      TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    parent_id    TEXT,
    name         TEXT,
    kind         TEXT,
    status       TEXT,
    start_ms     INTEGER,
    end_ms       INTEGER,
    duration_ms  INTEGER,
    attrs        TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_spans_task ON spans(task_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent ON spans(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_ok ON tasks(sut_name, ok);

-- ==================== 评测 harness（Step 4）====================
-- 这两张表与 tasks/spans 的分工：
--   tasks/spans  记录"SUT 干了什么"
--   bench_*      记录"判定器认为它做对了吗"
-- 分开存的原因：同一条 trace 可能被多套任务/多种判定规则复用，
-- 把判定结果写进 tasks 会让"一次运行"和"一次评测"的概念混在一起。
CREATE TABLE IF NOT EXISTS bench_suites (
    suite_id       TEXT PRIMARY KEY,
    label          TEXT,
    model_name     TEXT,
    temperature    REAL,
    runs_per_task  INTEGER,
    concurrency    INTEGER,
    budget_mode    TEXT,
    started_at     TEXT,
    elapsed_s      REAL,
    total_runs     INTEGER,
    successes      INTEGER,
    task_count     INTEGER,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS bench_runs (
    run_id                 TEXT PRIMARY KEY,
    suite_id               TEXT NOT NULL,
    task_uid               TEXT,
    task_key               TEXT,
    group_name             TEXT,
    run_index              INTEGER,
    ok                     INTEGER,
    self_reported_ok       INTEGER,
    failed_checks          TEXT,
    process_flags          TEXT,
    task_id                TEXT,
    sut_name               TEXT,
    tokens                 INTEGER,
    cost_usd               REAL,
    elapsed_ms             INTEGER,
    llm_calls              INTEGER,
    tool_calls             REAL,
    steps_done             INTEGER,
    error_type             TEXT,
    error                  TEXT,
    guard_config           TEXT,
    budget_violated        TEXT,
    budget_would_stop      INTEGER,
    action_diversity       REAL,
    postcondition_warnings INTEGER,
    faults_injected        INTEGER,
    created_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_bench_runs_suite ON bench_runs(suite_id);
CREATE INDEX IF NOT EXISTS idx_bench_runs_task ON bench_runs(task_uid);
"""

# 后加的列：老库需要 ALTER TABLE 才能补齐。
# 为什么要这个：Step 3 之后 tasks 表多了加固层字段，而 CREATE TABLE IF NOT EXISTS
# 对已存在的表**不会**新增列 —— 不写迁移的话，老库会静默地缺字段，
# 写入时抛 OperationalError（或更糟：字段永远为空）。
_TASKS_GUARD_COLUMNS = {
    "budget_violated_metrics": "TEXT",
    "budget_would_stop": "INTEGER",
    "action_diversity": "REAL",
    "postcondition_warnings": "INTEGER",
    "faults_injected": "INTEGER",
    # LLM 录制回放（Step 6）：模式单独成列（可 GROUP BY），统计整包存 JSON。
    # 为什么模式不复用 `replay_stats` 里的字段：报告要能按模式过滤（"只看真跑的数据"），
    # 而过滤一个 JSON 字段要么用 LIKE 猜、要么全表读进内存。
    "replay_mode": "TEXT",
    "replay_stats": "TEXT",
}

# bench_runs 的后加列（同样的迁移理由）
_BENCH_RUN_COLUMNS = {
    "self_reported_ok": "INTEGER",
    # 加固配置标签：没有它，"无加固"与"加固"两组数据落库后无法区分
    "guard_config": "TEXT",
    # 故障规格的**文本化**形式（如 `partial_write@write_file(rate=0.5)`）。
    # 原来只有 `faults_injected` 这个**计数**：
    # 它能让 `avg_cost_for_group(faulted=True)` 工作，却完全答不了
    # "这次运行注入的是哪一类故障"—— 做到 10 类故障 × 2 种加固的矩阵时，
    # 分组只能靠人工写在 label 字符串里，事后无法查询、无法自动分组。
    "fault_spec": "TEXT",
    # 交错 A/B 的臂名（如 `guard=none` / `guard=all`）。
    # 一个 suite 里可以同时含多个臂（这正是交错的意义：时间漂移对两边同权），
    # 所以"哪个臂"必须落在**运行**这一级，而不是 suite 标题里。
    "arm": "TEXT",
    "replay_mode": "TEXT",
}

# bench_suites 的后加列：suite 级的实验条件。
# 为什么 suite 级也要存（运行级已经存了）：跑完 10 个 suite 后想回答
# "哪几次是同配置的"，需要一条 SQL 就能筛出来，而不是把几万行 runs 全读一遍。
_BENCH_SUITE_COLUMNS = {
    "guard_config": "TEXT",
    "fault_spec": "TEXT",
    "replay_mode": "TEXT",
    "arms": "TEXT",
    "interleaved": "INTEGER",
    # 环境指纹（Step 8）：Agent 当时能看到哪些工具。
    # 两个列而不是一个：`env_digest` 用来 GROUP BY（"这几组是不是同一个环境"），
    # `env_fingerprint` 存完整 JSON（要解释 digest 时看它）。
    "env_digest": "TEXT",
    "env_fingerprint": "TEXT",
}


def _column(row: Any, name: str, default: Any = None) -> Any:
    """安全读列：老库可能还没跑完迁移（或行来自 LEFT JOIN）。

    为什么不用 `row[name]`：`sqlite3.Row` 对不存在的列会抛 IndexError。
    而 `load_suite` 里的语句在版本升级前后可能列数不同 ——
    一句 `SELECT *` 写下去，读旧库就炸，读新库才对，这种错很难在测试里覆盖到。
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _json_list(raw: Any) -> List[str]:
    """把 TEXT 列里的 JSON 数组读回来（坏数据返回空列表，不抛）。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _json_items(raw: Any) -> List[Any]:
    """同上，但**不**把元素转成字符串（用于 arms 这种 dict 列表）。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return list(value) if isinstance(value, list) else []


def _json_dict(raw: Any) -> Dict[str, Any]:
    """把 TEXT 列里的 JSON 对象读回来（坏数据返回空字典，不抛）。

    为什么需要它：这些列是**历史数据**（旧版本写过什么不由现在决定），
    而一条坏行不该让成本归因/报告整个挂掉 —— 那正是最需要它的时候。
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def as_int(value: Any, default: int = 0) -> int:
    """安全取整（库里的历史行 / 上游 report 的字段可能是 None / 字符串 / 浮点）。

    为什么不用裸 `int()`：这些值来自**不由本函数决定**的来源（历史数据、
    `injector.report()` 这类 dict），一条坏行不该让读取/报告整个挂掉。

    公开名（不帯下划线）：`lab/bench/runner.py` 的统计聚合要用**同一套**约定 ——
    否则同一个 `guard` dict 在两个模块里会被两种语义处理。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    """安全转浮点（同上：坏行不该让读取崩掉）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _replay_misses_from_flags(flags: List[str]) -> int:
    """从过程 flag 里读回回放未命中次数。

    为什么不用单独一列：这个信息已经写在 `process_flags` 里了（`replay_misses:6`）。
    多一列就多一份可能与 flag 不一致的状态；直接从 flag 反解，
    历史数据（加 flag 之前跑的）自然退化成 0 —— 而那时确实没记录过。
    """
    for flag in flags:
        if flag.startswith("replay_misses:"):
            try:
                return int(flag.split(":", 1)[1])
            except (TypeError, ValueError):
                return 0
    return 0


def _tool_calls_from_row(row: Any) -> float:
    """从事件流（`tasks.tool_sequence`）算工具调用次数，拿不到才回退到存储列。

    为什么必须这样：`bench_runs.tool_calls` 曾经是从 `loop_guard` 取的，
    而 `loop_guard` 可以被 `--guard none` 关掉 → 对照组的计数全为 0，
    于是"无加固 vs 加固"会显示出完全虚假的差异（实测：假的 +31 次调用）。
    **指标不能依赖可以被开关关掉的能力。**
    """
    sequence = row["task_tool_sequence"] if "task_tool_sequence" in row else None
    if sequence:
        try:
            return float(len(json.loads(sequence)))
        except (TypeError, ValueError):
            pass
    # 兜底列也可能是坏数据 → 走 as_float（带 try/except）
    return as_float(row["tool_calls"])


# tasks 表的写入列。
# 为什么单独列成常量：`save_run` 的 INSERT 现在是**静态 SQL**（不留动态拼接），
# 而 sqlite3 对多余的字典键是**宽容**的 —— “加了字段忘了改 SQL”会安静地丢数据。
# 所以用这个元组做一次集合对账，不一致就抛异常（把静默丢数据变成响亮失败）。
_TASK_INSERT_COLUMNS = (
    "task_id",
    "sut_name",
    "goal",
    "ok",
    "error",
    "error_type",
    "answer",
    "plan_title",
    "steps_total",
    "steps_done",
    "steps_succeeded",
    "steps_failed",
    "llm_calls",
    "llm_errors",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "tool_calls",
    "cost_usd",
    "elapsed_ms",
    "started_at",
    "workspace",
    "tool_sequence",
    "budget_violated_metrics",
    "budget_would_stop",
    "action_diversity",
    "postcondition_warnings",
    "faults_injected",
    "replay_mode",
    "replay_stats",
)


class SpanStore:
    """轨迹存储（SQLite）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # ==================== 连接与建表 ====================

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """给已存在的表补新列（幂等）。"""
        for table, columns in (
            ("tasks", _TASKS_GUARD_COLUMNS),
            ("bench_runs", _BENCH_RUN_COLUMNS),
            ("bench_suites", _BENCH_SUITE_COLUMNS),
        ):
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ==================== 写入 ====================

    def save_run(self, result: "TaskResult", spans: List[Span], *, goal: str = "") -> None:
        """写入一次完整运行（任务行 + 全部 span）。同 task_id 重复写入会覆盖，保证幂等。"""
        guard = result.guard or {}
        budget = guard.get("budget") or {}
        loop = guard.get("loop") or {}

        # 用显式列名而不是 VALUES(?,?,...) 位置参数：
        # 以后再加字段时，不会因为占位符数量对不上而把钱已经花掉的评测结果写坏。
        columns = {
            "task_id": result.task_id,
            "sut_name": result.sut_name,
            "goal": goal or result.plan_goal,
            "ok": 1 if result.ok else 0,
            "error": result.error,
            "error_type": result.error_type,
            "answer": result.answer,
            "plan_title": result.plan_title,
            "steps_total": result.steps.total,
            "steps_done": result.steps.done,
            "steps_succeeded": result.steps.succeeded,
            "steps_failed": result.steps.failed,
            "llm_calls": result.llm_usage.llm_calls,
            "llm_errors": result.llm_usage.llm_errors,
            "prompt_tokens": result.llm_usage.prompt_tokens,
            "completion_tokens": result.llm_usage.completion_tokens,
            "total_tokens": result.llm_usage.total_tokens,
            "tool_calls": as_int(loop.get("tool_calls") or result.llm_usage.tool_calls),
            "cost_usd": result.cost_usd,
            "elapsed_ms": result.elapsed_ms,
            # 时间戳刻意用**本地 naive**：库里已有数据都是这个语义，改成 UTC 会让新旧数据不同源。
            "started_at": datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005
            "workspace": result.workspace,
            "tool_sequence": json.dumps(result.tool_sequence, ensure_ascii=False),
            # ---- Step 3 加固层字段 ----
            "budget_violated_metrics": json.dumps(budget.get("violated_metrics") or [], ensure_ascii=False),
            "budget_would_stop": 1 if budget.get("would_stop") else 0,
            "action_diversity": loop.get("action_diversity"),
            "postcondition_warnings": len((guard.get("postconditions") or {}).get("warnings") or []),
            "faults_injected": as_int((guard.get("faults") or {}).get("injections")),
            # ---- Step 6 回放 ----
            # 写入时用 `result.replay`；旧调用点没有这个字段 → 退化成空 dict，
            # 于是模式落成 "off"（而不是 NULL）—— NULL 会被读成"未知"，
            # 而那时确实就是没开缓存。
            "replay_mode": (result.replay or {}).get("mode") or "off",
            "replay_stats": json.dumps(result.replay or {}, ensure_ascii=False),
        }
        with self._connect() as conn:
            # 列集对账：静态 SQL 无法自动适配新增字段，所以这里显式守住一致性。
            mismatched = set(columns) ^ set(_TASK_INSERT_COLUMNS)
            if mismatched:
                raise ValueError(
                    f"save_run 的列与 INSERT 语句不一致（多了或漏了）：{sorted(mismatched)}。"
                    "新增字段时必须同时改 _TASK_INSERT_COLUMNS 与下面这条静态 SQL。"
                )
            conn.execute("DELETE FROM spans WHERE task_id = ?", (result.task_id,))
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (result.task_id,))
            conn.execute(
                # 静态 SQL + 命名参数（不再拼列名）
                """INSERT INTO tasks (
                       task_id, sut_name, goal, ok, error, error_type, answer, plan_title,
                       steps_total, steps_done, steps_succeeded, steps_failed,
                       llm_calls, llm_errors, prompt_tokens, completion_tokens, total_tokens,
                       tool_calls, cost_usd, elapsed_ms, started_at, workspace, tool_sequence,
                       budget_violated_metrics, budget_would_stop, action_diversity,
                       postcondition_warnings, faults_injected, replay_mode, replay_stats)
                   VALUES (
                       :task_id, :sut_name, :goal, :ok, :error, :error_type, :answer, :plan_title,
                       :steps_total, :steps_done, :steps_succeeded, :steps_failed,
                       :llm_calls, :llm_errors, :prompt_tokens, :completion_tokens, :total_tokens,
                       :tool_calls, :cost_usd, :elapsed_ms, :started_at, :workspace, :tool_sequence,
                       :budget_violated_metrics, :budget_would_stop, :action_diversity,
                       :postcondition_warnings, :faults_injected, :replay_mode, :replay_stats)""",
                columns,
            )
            conn.executemany(
                """INSERT INTO spans
                   (span_id, task_id, parent_id, name, kind, status,
                    start_ms, end_ms, duration_ms, attrs, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        s.span_id,
                        s.task_id,
                        s.parent_id,
                        s.name,
                        s.kind.value if hasattr(s.kind, "value") else str(s.kind),
                        s.status,
                        s.start_ms,
                        s.end_ms,
                        s.duration_ms,
                        json.dumps(s.attrs, ensure_ascii=False, default=str),
                        s.error,
                    )
                    for s in spans
                ],
            )

    # ==================== 读取 ====================

    def load_spans(self, task_id: str) -> List[Span]:
        """读回 span 列表（按开始时间排序，保证树形渲染的稳定性）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM spans WHERE task_id = ? ORDER BY start_ms ASC, rowid ASC",
                (task_id,),
            ).fetchall()

        return [
            Span(
                span_id=r["span_id"],
                task_id=r["task_id"],
                parent_id=r["parent_id"],
                name=r["name"] or "",
                kind=r["kind"],
                status=r["status"] or "ok",
                start_ms=r["start_ms"] or 0,
                end_ms=r["end_ms"],
                duration_ms=r["duration_ms"],
                attrs=_json_dict(r["attrs"]),
                error=r["error"],
            )
            for r in rows
        ]

    def load_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def latest_task_id(self) -> Optional[str]:
        """取最近一次运行（CLI 便利功能：不用手敲 uuid）。"""
        with self._connect() as conn:
            row = conn.execute("SELECT task_id FROM tasks ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        return row["task_id"] if row else None

    def resolve_task_id(self, prefix: str) -> Optional[str]:
        """把短 id 解析成完整 task_id。

        为什么要这个：完整 uuid 是 36 个字符，从日志里拷一半是常态，
        手敲整串几乎必错。规则：**只有唯一匹配才返回**，
        0 个或多个都返回 None（让调用方报错），绝不猜。
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT task_id FROM tasks WHERE task_id LIKE ? LIMIT 2", (f"{prefix}%",)).fetchall()
        return rows[0]["task_id"] if len(rows) == 1 else None

    def list_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """列出最近的运行（Step 4 的 baseline 报告会基于这个做聚合）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT task_id, sut_name, ok, error_type, total_tokens, cost_usd,
                          elapsed_ms, tool_calls, started_at
                   FROM tasks ORDER BY started_at DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ==================== 评测 harness（Step 4）====================

    def save_suite(self, suite: Any) -> None:
        """写入一次评测（suite 元信息 + 全部 run）。同 suite_id 重复写入会覆盖。"""
        self.save_suite_header(suite)
        for outcome in suite.outcomes:
            self.save_outcome(outcome)
        self.finalize_suite(suite)

    def save_suite_header(self, suite: Any) -> None:
        """先写 suite 元信息（**在跑之前**）。

        为什么要拆出来：长时间评测（40 次运行 = 半小时）中途被中断是常态。
        先把元信息写进去 + 每完成一次运行就落库，崩溃最多丢一次运行，
        而不是丢掉整份数据（否则半小时的 API 花费直接归零）。
        这就是 handoff 体检第 7 项"断点续跑"在评测层的落地。
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM bench_suites WHERE suite_id = ?", (suite.suite_id,))
            conn.execute(
                """INSERT INTO bench_suites
                   (suite_id, label, model_name, temperature, runs_per_task, concurrency,
                    budget_mode, started_at, elapsed_s, total_runs, successes, task_count, notes,
                    guard_config, fault_spec, replay_mode, arms, interleaved,
                    env_digest, env_fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    suite.suite_id,
                    suite.label,
                    suite.model_name,
                    suite.temperature,
                    suite.runs_per_task,
                    suite.concurrency,
                    suite.budget_mode,
                    suite.started_at,
                    suite.elapsed_s,
                    suite.total_runs,
                    suite.successes,
                    len(suite.task_summaries),
                    json.dumps(suite.notes, ensure_ascii=False),
                    getattr(suite, "guard_config", "") or "",
                    getattr(suite, "fault_spec", "") or "",
                    getattr(suite, "replay_mode", "off") or "off",
                    json.dumps(getattr(suite, "arms", []) or [], ensure_ascii=False),
                    1 if getattr(suite, "interleaved", False) else 0,
                    getattr(suite, "env_digest", "") or "",
                    getattr(suite, "env_fingerprint", "") or "",
                ),
            )

    def finalize_suite(self, suite: Any) -> None:
        """运行结束后回写汇总字段（总数/成功数/耗时 + 实验条件）。

        实验条件（guard / fault / replay / arms）在这里**再写一次**：
        header 是在跑之前写的，那时如果调用方还没把臂信息填进 suite，
        落库的就是空值；事后没人能区分"没记录"与"确实没有"。
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE bench_suites
                   SET elapsed_s = ?, total_runs = ?, successes = ?, task_count = ?, notes = ?,
                       guard_config = ?, fault_spec = ?, replay_mode = ?, arms = ?, interleaved = ?,
                       env_digest = ?, env_fingerprint = ?
                   WHERE suite_id = ?""",
                (
                    suite.elapsed_s,
                    suite.total_runs,
                    suite.successes,
                    len(suite.task_summaries),
                    json.dumps(suite.notes, ensure_ascii=False),
                    getattr(suite, "guard_config", "") or "",
                    getattr(suite, "fault_spec", "") or "",
                    getattr(suite, "replay_mode", "off") or "off",
                    json.dumps(getattr(suite, "arms", []) or [], ensure_ascii=False),
                    1 if getattr(suite, "interleaved", False) else 0,
                    getattr(suite, "env_digest", "") or "",
                    getattr(suite, "env_fingerprint", "") or "",
                    suite.suite_id,
                ),
            )

    def save_outcome(self, outcome: Any) -> None:
        """写入/覆盖**一次运行**的结果（幂等）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM bench_runs WHERE run_id = ?", (outcome.run_id,))
            conn.execute(
                """INSERT INTO bench_runs
                   (run_id, suite_id, task_uid, task_key, group_name, run_index, ok, self_reported_ok,
                    failed_checks, process_flags, task_id, sut_name, tokens, cost_usd, elapsed_ms,
                    llm_calls, tool_calls, steps_done, error_type, error, guard_config, budget_violated,
                    budget_would_stop, action_diversity, postcondition_warnings, faults_injected,
                    fault_spec, arm, replay_mode, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outcome.run_id,
                    outcome.suite_id,
                    outcome.task_uid,
                    outcome.task_key,
                    outcome.group,
                    outcome.run_index,
                    1 if outcome.ok else 0,
                    1 if outcome.self_reported_ok else 0,
                    json.dumps(outcome.failed_checks, ensure_ascii=False),
                    json.dumps(outcome.process_flags, ensure_ascii=False),
                    outcome.task_id,
                    outcome.sut_name,
                    outcome.tokens,
                    outcome.cost_usd,
                    outcome.elapsed_ms,
                    outcome.llm_calls,
                    outcome.tool_calls,
                    outcome.steps_done,
                    outcome.error_type,
                    outcome.error,
                    outcome.guard_config,
                    json.dumps(outcome.budget_violated, ensure_ascii=False),
                    1 if outcome.budget_would_stop else 0,
                    outcome.action_diversity,
                    outcome.postcondition_warnings,
                    outcome.faults_injected,
                    getattr(outcome, "fault_spec", "") or "",
                    getattr(outcome, "arm", "") or "",
                    getattr(outcome, "replay_mode", "off") or "off",
                    # 同上：本地 naive，与既有数据保持一致
                    datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005
                ),
            )

    def load_task_by_workspace(self, workspace: str) -> Optional[Dict[str, Any]]:
        """根据工作区路径反查运行记录。

        用途：
        1. 崩溃后重建评测（把工作区里的产物与已落库的指标重新对应上）；
        2. 人工排查"这个目录到底是哪次运行"。
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT task_id, sut_name, goal, ok, error, error_type, llm_calls, llm_errors,
                          total_tokens, tool_calls, cost_usd, elapsed_ms, plan_title,
                          steps_total, steps_done, budget_violated_metrics, budget_would_stop,
                          action_diversity, postcondition_warnings, faults_injected, started_at
                   FROM tasks WHERE workspace = ? ORDER BY rowid DESC LIMIT 1""",
                (workspace,),
            ).fetchone()
        return dict(row) if row else None

    def list_suites(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT suite_id, label, model_name, runs_per_task, started_at,
                          total_runs, successes, task_count, elapsed_s, budget_mode
                   FROM bench_suites ORDER BY started_at DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_suite(self, suite_id: str) -> Optional[Any]:
        """重建一次评测的汇总结果（用于"不重跑也能重新出报告"）。

        注意：这里只重建汇总所需的信息（failed_checks / process_flags），
        不重建逐条 CheckResult —— 报告只需要知道"哪几条没过"，
        完整判定明细已经在跑的时候打印过了。
        """
        # 延迟导入：bench.runner 会间接 import lab.api，而 lab.api 又依赖本模块，
        # 模块级导入会形成循环。函数内导入可以避免这个问题。
        from lab.bench.runner import RunOutcome, SuiteResult, summarize_task
        from lab.bench.task import load_all_tasks

        with self._connect() as conn:
            suite_row = conn.execute("SELECT * FROM bench_suites WHERE suite_id = ?", (suite_id,)).fetchone()
            if not suite_row:
                return None
            run_rows = conn.execute(
                # 联表取 tool_sequence：工具调用次数必须从**事件流**算，
                # 而不是从 bench_runs.tool_calls 列读 —— 那一列的历史数据里，
                # guard=none 的运行是 0（因为当时的计数靠 loop_guard，而它被关掉了）。
                # 不这样修的话，"无加固 vs 加固"的对比会出现完全虚假的"+31 次工具调用"。
                """SELECT b.*, t.tool_sequence AS task_tool_sequence
                   FROM bench_runs b LEFT JOIN tasks t ON b.task_id = t.task_id
                   WHERE b.suite_id = ? ORDER BY b.task_uid, b.run_index""",
                (suite_id,),
            ).fetchall()

        outcomes = [
            RunOutcome(
                run_id=row["run_id"],
                suite_id=row["suite_id"],
                task_uid=row["task_uid"],
                task_key=row["task_key"] or "",
                group=row["group_name"] or "",
                run_index=row["run_index"] or 0,
                ok=bool(row["ok"]),
                self_reported_ok=bool(row["self_reported_ok"]),
                failed_checks=_json_list(row["failed_checks"]),
                process_flags=_json_list(row["process_flags"]),
                task_id=row["task_id"] or "",
                sut_name=row["sut_name"] or "",
                tokens=row["tokens"] or 0,
                cost_usd=row["cost_usd"] or 0.0,
                elapsed_ms=row["elapsed_ms"] or 0,
                llm_calls=row["llm_calls"] or 0,
                tool_calls=_tool_calls_from_row(row),
                steps_done=row["steps_done"] or 0,
                error_type=row["error_type"],
                error=row["error"],
                guard_config=row["guard_config"] or "all",
                budget_violated=_json_list(row["budget_violated"]),
                budget_would_stop=bool(row["budget_would_stop"]),
                action_diversity=row["action_diversity"],
                postcondition_warnings=row["postcondition_warnings"] or 0,
                faults_injected=row["faults_injected"] or 0,
                fault_spec=_column(row, "fault_spec") or "",
                arm=_column(row, "arm") or "",
                replay_mode=_column(row, "replay_mode") or "off",
                replay_misses=_replay_misses_from_flags(_json_list(row["process_flags"])),
            )
            for row in run_rows
        ]

        suites = {task.uid: task for task in load_all_tasks()}
        return SuiteResult(
            suite_id=suite_id,
            label=suite_row["label"] or "",
            model_name=suite_row["model_name"] or "",
            temperature=suite_row["temperature"] or 0.0,
            runs_per_task=suite_row["runs_per_task"] or 1,
            concurrency=suite_row["concurrency"] or 1,
            budget_mode=suite_row["budget_mode"] or "observe",
            started_at=suite_row["started_at"] or "",
            elapsed_s=suite_row["elapsed_s"] or 0.0,
            notes=_json_list(suite_row["notes"]),
            self_report_available=all(row["self_reported_ok"] is not None for row in run_rows),
            guard_config=_column(suite_row, "guard_config") or "",
            fault_spec=_column(suite_row, "fault_spec") or "",
            replay_mode=_column(suite_row, "replay_mode") or "off",
            arms=_json_items(_column(suite_row, "arms")),
            interleaved=bool(_column(suite_row, "interleaved")),
            env_digest=_column(suite_row, "env_digest") or "",
            env_fingerprint=_column(suite_row, "env_fingerprint") or "",
            outcomes=outcomes,
            task_summaries=[
                summarize_task(suites[uid], [o for o in outcomes if o.task_uid == uid])
                for uid in sorted({o.task_uid for o in outcomes})
                if uid in suites
            ],
        )

    def cost_by(self, *, suite_id: Optional[str] = None, kind: str = "tool", limit: int = 12) -> Dict[str, Any]:
        """按 span 归属聚合成本：**钱花在哪一步 / 哪个工具**（T3 / 审计 §4.12）。

        为什么必须靠 span 树：成本只在 **LLM 调用**那一层可观测
        （span 的 attrs 里有 prompt/completion tokens，但**没有 cost 列**），
        而人想知道的是"钱花在哪一步"。所以把每次 LLM 调用的成本
        向上归给**最近的 `kind` 祖先** —— 这正是 span 父子关系的用途。

        实测价值：最贵的那次运行是"20 步里 10 步在探环境/看字节"，
        而这种结论只有归因才看得出来（task 级成本只能说"它贵"，
        说不出"贵在哪个动作"）。

        :param suite_id: 只统计这个 suite 的运行；None = 全库
        :param kind: `tool`（按工具名，**按时间归给紧随其后的工具调用**）或
                     `step`（按计划步骤名，按 span 树归给最近的 step 祖先）
        :return: {"rows", "total_cost", "attributed_cost", "unattributed_cost", "kind"}
        """
        from lab.usage import price_of  # 函数内导入：与 usage 保持低耦合

        if kind not in ("tool", "step"):
            raise ValueError("kind 只能是 tool 或 step")

        pattern = f"{suite_id}%" if suite_id else None
        with self._connect() as conn:
            rows = conn.execute(
                # 静态 SQL + 命名参数（不拼接）
                """
                SELECT span_id, task_id, parent_id, name, kind, start_ms, end_ms, attrs
                FROM spans
                WHERE (:pattern IS NULL
                       OR task_id IN (SELECT task_id FROM bench_runs WHERE suite_id LIKE :pattern))
                """,
                {"pattern": pattern},
            ).fetchall()

        by_id = {row["span_id"]: row for row in rows}
        # ⚠️ 工具归因**不能用 span 树**：实测 LLM span 与 tool span 是**同一个 step 下的兄弟**
        # （recorder 里 llm 不 push，所以它不会成为 tool 的子节点）。
        # 按树找"工具祖先"会得到 100% 任务级 —— 实测就是这样的（报告里
        # "可归因成本 $0.0000" 把这个问题直接暴露了出来）。
        # 改用**时间顺序**：把一次 LLM 调用的成本归给**紧随其后的那次工具调用**
        # （即这次调用产出的动作）；最后一次调用（通常是收尾）落在任务级。
        tools_by_task: Dict[str, List[tuple]] = {}
        for row in rows:
            if row["kind"] == "tool":
                tools_by_task.setdefault(row["task_id"], []).append(
                    (row["start_ms"] or 0, row["name"] or "（未命名 tool）")
                )
        for items in tools_by_task.values():
            items.sort()

        buckets: Dict[str, Dict[str, float]] = {}
        total_cost = 0.0
        attributed_cost = 0.0
        root_label = "（任务级：没有归属的工具/步骤）"

        for row in rows:
            if row["kind"] != "llm":
                continue
            attrs = _json_dict(row["attrs"])
            prompt = attrs.get("prompt_tokens") or 0
            completion = attrs.get("completion_tokens") or 0
            if not prompt and not completion:
                continue
            price = price_of(attrs.get("model") or "")
            cost = prompt / 1_000_000 * price[0] + completion / 1_000_000 * price[1] if price else 0.0
            total_cost += cost

            # 归属：tool → 按时间找紧随其后的工具调用；step → 按 span 树找最近祖先
            label = root_label
            if kind == "tool":
                moment = row["end_ms"] or row["start_ms"] or 0
                following = [name for start, name in tools_by_task.get(row["task_id"], []) if start >= moment]
                if following:
                    label = following[0]
                    attributed_cost += cost
            else:
                cursor = row["parent_id"]
                for _ in range(64):  # 防御环（脏数据不应该让命令挂死）
                    if not cursor:
                        break
                    parent = by_id.get(cursor)
                    if parent is None:
                        break
                    if parent["kind"] == kind:
                        label = parent["name"] or f"（未命名 {kind}）"
                        attributed_cost += cost
                        break
                    cursor = parent["parent_id"]

            bucket = buckets.setdefault(label, {"cost_usd": 0.0, "tokens": 0.0, "llm_calls": 0})
            bucket["cost_usd"] += cost
            bucket["tokens"] += prompt + completion
            bucket["llm_calls"] += 1

        ordered = sorted(buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)
        out_rows = [
            {
                "name": name,
                "cost_usd": round(value["cost_usd"], 6),
                # 用 round() 而不是 int()：它本身就是取整（无 ndigits 时返回 int），
                # 少一次可能抛异常的转换调用。
                "tokens": round(value["tokens"]),
                "llm_calls": round(value["llm_calls"]),
                "share": (value["cost_usd"] / total_cost) if total_cost else 0.0,
            }
            for name, value in ordered
        ]
        if len(out_rows) > limit:
            rest = out_rows[limit:]
            out_rows = out_rows[:limit] + [
                {
                    "name": f"（其他 {len(rest)} 项）",
                    "cost_usd": round(sum(r["cost_usd"] for r in rest), 6),
                    "tokens": sum(r["tokens"] for r in rest),
                    "llm_calls": sum(r["llm_calls"] for r in rest),
                    "share": sum(r["share"] for r in rest),
                }
            ]
        return {
            "rows": out_rows,
            "total_cost": round(total_cost, 6),
            "attributed_cost": round(attributed_cost, 6),
            "unattributed_cost": round(total_cost - attributed_cost, 6),
            "kind": kind,
        }

    def task_success_audit(self) -> List[Dict[str, Any]]:
        """每个任务的**干净运行**成功率（供 `bench purpose-audit`，T5）。

        为什么只算干净运行：故障注入会**人为压低**成功率（实测故障格成本 +109%、
        Agent 在额外重试），把它算进"任务稳不稳定"会把装置噪声当成能力边界。

        另带最近一次干净失败的 suite/run：告警要能直接接一条 `bench show` 命令，
        否则人拿到告警还得自己去库里翻（诊断信息本身是产品）。
        """
        with self._connect() as conn:
            rows = conn.execute(
                # 静态 SQL（不拼接、也不需要参数）
                """
                SELECT task_key,
                       SUM(CASE WHEN COALESCE(faults_injected, 0) = 0 THEN 1 ELSE 0 END) AS clean_n,
                       SUM(CASE WHEN COALESCE(faults_injected, 0) = 0 AND ok = 1
                                THEN 1 ELSE 0 END) AS clean_ok,
                       SUM(CASE WHEN COALESCE(faults_injected, 0) > 0 THEN 1 ELSE 0 END) AS fault_n,
                       SUM(CASE WHEN COALESCE(faults_injected, 0) > 0 AND ok = 1
                                THEN 1 ELSE 0 END) AS fault_ok,
                       (SELECT b2.suite_id FROM bench_runs b2
                         WHERE b2.task_key = bench_runs.task_key
                           AND COALESCE(b2.faults_injected, 0) = 0 AND b2.ok = 0
                         ORDER BY b2.created_at DESC LIMIT 1) AS last_clean_fail_suite,
                       (SELECT b2.run_index FROM bench_runs b2
                         WHERE b2.task_key = bench_runs.task_key
                           AND COALESCE(b2.faults_injected, 0) = 0 AND b2.ok = 0
                         ORDER BY b2.created_at DESC LIMIT 1) AS last_clean_fail_run
                FROM bench_runs
                GROUP BY task_key
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def sample_runs_for_judging(self, limit: int = 100) -> List[Dict[str, Any]]:
        """挑用于人工标注的运行：**失败优先，再补成功**（T14）。

        为什么要混正负样本：只拿失败样本时金标分布会偏斜（同一维度几乎全是 false），
        κ 的期望一致率随之失真（极端情况直接退化成"无法计算"）。
        抽样就是为了让分布可读 —— 这是校准能不能得出可信 κ 的前提。
        """
        sql = """
            SELECT b.suite_id, b.task_key, b.group_name AS group_name, b.run_index, b.arm,
                   b.self_reported_ok, b.ok, t.workspace AS workspace, t.goal AS goal
            FROM bench_runs b LEFT JOIN tasks t ON b.task_id = t.task_id
            WHERE b.ok = :ok
            ORDER BY b.created_at DESC
            LIMIT :limit
        """
        out: List[Dict[str, Any]] = []
        with self._connect() as conn:
            for ok in (0, 1):  # 0 = 失败优先
                if len(out) >= limit:
                    break
                rows = conn.execute(sql, {"ok": ok, "limit": limit - len(out)}).fetchall()
                out.extend(dict(row) for row in rows)
        return out[:limit]

    def list_failed_runs(self, *, limit: int = 10, suite_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """挑出**失败的**运行（供 `bench sample-failures` 读轨迹）。

        为什么用 SQL 而不是遍历 suite：`bench_runs.ok` 本来就是一列，
        一条 SQL 就能筛；评测跑多以后把几万行读进内存再过滤会变成瓶颈。

        返回**行的字典**而不是 RunOutcome：这里要的是"去哪读轨迹"
        （task_id / workspace / 失败检查 / 工具序列），不是重建整个 suite 的统计口径。
        workspace 与 tool_sequence 来自 `tasks` 表（`bench_runs` 里没有它们）。
        """
        with self._connect() as conn:
            rows = conn.execute(
                # 静态 SQL + 命名参数（`:pattern IS NULL` 让"全库"与"单个 suite"共用一条）
                """
                SELECT b.suite_id, b.task_key, b.task_uid, b.run_index, b.arm, b.task_id,
                       b.error_type, b.error, b.failed_checks, b.process_flags, b.cost_usd,
                       b.created_at, b.self_reported_ok, b.tool_calls,
                       t.workspace AS workspace, t.tool_sequence AS tool_sequence
                FROM bench_runs b LEFT JOIN tasks t ON b.task_id = t.task_id
                WHERE b.ok = 0
                  AND (:pattern IS NULL OR b.suite_id LIKE :pattern)
                ORDER BY b.created_at DESC
                LIMIT :limit
                """,
                {"pattern": f"{suite_id}%" if suite_id else None, "limit": limit},
            ).fetchall()
        return [dict(row) for row in rows]

    def load_runs_grouped_by_task(self, limit_per_task: int = 500) -> Dict[str, List[Dict[str, Any]]]:
        """把历史运行按 task_key 分组（用于按分位数推导预算阈值）。

        为什么要限制 limit_per_task：评测跑多了以后这张表会很大，
        而阈值推导只需要近期的分布（旧版本 SUT 的数据反而会污染阈值）。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT task_key, group_name, tokens, cost_usd, elapsed_ms, tool_calls,
                          steps_done, ok, created_at
                   FROM bench_runs ORDER BY created_at DESC"""
            ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            key = row["task_key"] or ""
            bucket = grouped.setdefault(key, [])
            if len(bucket) < limit_per_task:
                bucket.append(dict(row))
        return grouped

    def avg_cost_for_group(self, group: str, *, faulted: Optional[bool] = None) -> Optional[float]:
        """某个分组的历史平均单次成本（用于**预估预算**）。

        why 需要它：CLI 原来用一个写死的系数（$0.045/次）估成本，
        那系数来自冒烟测试的简单任务；实测 semireal 任务的平均成本是它的 3 倍，
        导致"预估 $1.8、实际花 $5" —— 花钱的事上估错 3 倍是不能接受的。
        改成读实测历史：没有历史时由调用方给保守默认值。

        :param faulted: True = 只看**带故障注入**的运行；
            False = 只看无故障的。这一区分很关键：实测故障格的单次成本是基线的
            2~3 倍（Agent 会因为产物被截断而反复重试），
            用无故障均值去估故障实验会低估一半以上。
        """
        with self._connect() as conn:
            row = conn.execute(
                # 静态 SQL + 命名参数（用 NULL/1/0 表达"不筛/只看故障/只看无故障"）
                """
                SELECT AVG(cost_usd) AS avg_cost, COUNT(*) AS n
                FROM bench_runs
                WHERE group_name = :group
                  AND (:faulted IS NULL
                       OR (:faulted = 1 AND COALESCE(faults_injected, 0) > 0)
                       OR (:faulted = 0 AND COALESCE(faults_injected, 0) = 0))
                """,
                {"group": group, "faulted": None if faulted is None else (1 if faulted else 0)},
            ).fetchone()
        if not row or not row["n"]:
            return None
        return as_float(row["avg_cost"])

    def stats(self, sut_name: Optional[str] = None) -> Dict[str, Any]:
        """跨运行聚合：成功率、tokens/任务、成本/任务、P95 耗时。

        这是 handoff 第 8 节指标模板的最小实现 —— Step 4 会在此基础上
        加置信区间（当前先用 min/max 给出波动范围，避免假装精确）。
        """
        where, params = "", []
        if sut_name:
            where, params = "WHERE sut_name = ?", [sut_name]

        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*)              AS runs,
                           SUM(ok)               AS ok_runs,
                           AVG(total_tokens)     AS avg_tokens,
                           MIN(total_tokens)     AS min_tokens,
                           MAX(total_tokens)     AS max_tokens,
                           AVG(cost_usd)         AS avg_cost,
                           AVG(elapsed_ms)       AS avg_elapsed,
                           AVG(tool_calls)       AS avg_tools
                    FROM tasks {where}""",
                params,
            ).fetchone()

            # P95 耗时：SQLite 没有百分位函数，用"排序后取第 95% 位"的等价写法
            p95_row = conn.execute(
                f"""SELECT elapsed_ms FROM tasks {where}
                    ORDER BY elapsed_ms ASC
                    LIMIT 1 OFFSET (
                        SELECT CAST(COUNT(*) * 0.95 AS INTEGER) FROM tasks {where}
                    )""",
                params + params,
            ).fetchone()

            # 按失败类型归因（handoff 第 8 节的"归因：定位/规划/验证/工具"雏形）
            by_error = conn.execute(
                f"""SELECT COALESCE(error_type, '(success)') AS error_type, COUNT(*) AS n
                    FROM tasks {where} GROUP BY error_type ORDER BY n DESC""",
                params,
            ).fetchall()

        return {
            "runs": row["runs"] or 0,
            "ok_runs": row["ok_runs"] or 0,
            "success_rate": (row["ok_runs"] / row["runs"]) if row["runs"] else 0.0,
            "avg_tokens": row["avg_tokens"] or 0,
            "min_tokens": row["min_tokens"] or 0,
            "max_tokens": row["max_tokens"] or 0,
            "avg_cost_usd": row["avg_cost"] or 0.0,
            "avg_elapsed_ms": row["avg_elapsed"] or 0,
            "p95_elapsed_ms": p95_row["elapsed_ms"] if p95_row else 0,
            "avg_tool_calls": row["avg_tools"] or 0,
            "by_error_type": [{"error_type": r["error_type"], "n": r["n"]} for r in by_error],
        }
