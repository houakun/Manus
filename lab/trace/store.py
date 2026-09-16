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
}

# bench_runs 的后加列（同样的迁移理由）
_BENCH_RUN_COLUMNS = {
    "self_reported_ok": "INTEGER",
}


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
            "tool_calls": int(loop.get("tool_calls") or result.llm_usage.tool_calls),
            "cost_usd": result.cost_usd,
            "elapsed_ms": result.elapsed_ms,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "workspace": result.workspace,
            "tool_sequence": json.dumps(result.tool_sequence, ensure_ascii=False),
            # ---- Step 3 加固层字段 ----
            "budget_violated_metrics": json.dumps(budget.get("violated_metrics") or [], ensure_ascii=False),
            "budget_would_stop": 1 if budget.get("would_stop") else 0,
            "action_diversity": loop.get("action_diversity"),
            "postcondition_warnings": len((guard.get("postconditions") or {}).get("warnings") or []),
            "faults_injected": int((guard.get("faults") or {}).get("injections") or 0),
        }
        placeholders = ",".join("?" for _ in columns)
        column_names = ",".join(columns)

        with self._connect() as conn:
            conn.execute("DELETE FROM spans WHERE task_id = ?", (result.task_id,))
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (result.task_id,))
            conn.execute(
                f"INSERT INTO tasks ({column_names}) VALUES ({placeholders})",
                tuple(columns.values()),
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
                attrs=json.loads(r["attrs"]) if r["attrs"] else {},
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
            row = conn.execute(
                "SELECT task_id FROM tasks ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return row["task_id"] if row else None

    def resolve_task_id(self, prefix: str) -> Optional[str]:
        """把短 id 解析成完整 task_id。

        为什么要这个：完整 uuid 是 36 个字符，从日志里拷一半是常态，
        手敲整串几乎必错。规则：**只有唯一匹配才返回**，
        0 个或多个都返回 None（让调用方报错），绝不猜。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE task_id LIKE ? LIMIT 2", (f"{prefix}%",)
            ).fetchall()
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
        with self._connect() as conn:
            conn.execute("DELETE FROM bench_runs WHERE suite_id = ?", (suite.suite_id,))
            conn.execute("DELETE FROM bench_suites WHERE suite_id = ?", (suite.suite_id,))
            conn.execute(
                """INSERT INTO bench_suites
                   (suite_id, label, model_name, temperature, runs_per_task, concurrency,
                    budget_mode, started_at, elapsed_s, total_runs, successes, task_count, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    suite.suite_id, suite.label, suite.model_name, suite.temperature,
                    suite.runs_per_task, suite.concurrency, suite.budget_mode, suite.started_at,
                    suite.elapsed_s, suite.total_runs, suite.successes, len(suite.task_summaries),
                    json.dumps(suite.notes, ensure_ascii=False),
                ),
            )
            conn.executemany(
                """INSERT INTO bench_runs
                   (run_id, suite_id, task_uid, task_key, group_name, run_index, ok, self_reported_ok,
                    failed_checks, process_flags, task_id, sut_name, tokens, cost_usd, elapsed_ms,
                    llm_calls, tool_calls, steps_done, error_type, error, budget_violated,
                    budget_would_stop, action_diversity, postcondition_warnings, faults_injected,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        o.run_id, o.suite_id, o.task_uid, o.task_key, o.group, o.run_index,
                        1 if o.ok else 0,
                        1 if o.self_reported_ok else 0,
                        json.dumps(o.failed_checks, ensure_ascii=False),
                        json.dumps(o.process_flags, ensure_ascii=False),
                        o.task_id, o.sut_name, o.tokens, o.cost_usd, o.elapsed_ms,
                        o.llm_calls, o.tool_calls, o.steps_done, o.error_type, o.error,
                        json.dumps(o.budget_violated, ensure_ascii=False),
                        1 if o.budget_would_stop else 0, o.action_diversity,
                        o.postcondition_warnings, o.faults_injected,
                        datetime.now().isoformat(timespec="seconds"),
                    )
                    for o in suite.outcomes
                ],
            )

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
            suite_row = conn.execute(
                "SELECT * FROM bench_suites WHERE suite_id = ?", (suite_id,)
            ).fetchone()
            if not suite_row:
                return None
            run_rows = conn.execute(
                "SELECT * FROM bench_runs WHERE suite_id = ? ORDER BY task_uid, run_index",
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
                failed_checks=json.loads(row["failed_checks"] or "[]"),
                process_flags=json.loads(row["process_flags"] or "[]"),
                task_id=row["task_id"] or "",
                sut_name=row["sut_name"] or "",
                tokens=row["tokens"] or 0,
                cost_usd=row["cost_usd"] or 0.0,
                elapsed_ms=row["elapsed_ms"] or 0,
                llm_calls=row["llm_calls"] or 0,
                tool_calls=row["tool_calls"] or 0.0,
                steps_done=row["steps_done"] or 0,
                error_type=row["error_type"],
                error=row["error"],
                budget_violated=json.loads(row["budget_violated"] or "[]"),
                budget_would_stop=bool(row["budget_would_stop"]),
                action_diversity=row["action_diversity"],
                postcondition_warnings=row["postcondition_warnings"] or 0,
                faults_injected=row["faults_injected"] or 0,
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
            notes=json.loads(suite_row["notes"] or "[]"),
            self_report_available=all(row["self_reported_ok"] is not None for row in run_rows),
            outcomes=outcomes,
            task_summaries=[
                summarize_task(suites[uid], [o for o in outcomes if o.task_uid == uid])
                for uid in sorted({o.task_uid for o in outcomes})
                if uid in suites
            ],
        )

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
