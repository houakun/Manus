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
"""


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

    # ==================== 写入 ====================

    def save_run(self, result: "TaskResult", spans: List[Span], *, goal: str = "") -> None:
        """写入一次完整运行（任务行 + 全部 span）。同 task_id 重复写入会覆盖，保证幂等。"""
        task_row = (
            result.task_id,
            result.sut_name,
            goal or result.plan_goal,
            1 if result.ok else 0,
            result.error,
            result.error_type,
            result.answer,
            result.plan_title,
            result.steps.total,
            result.steps.done,
            result.steps.succeeded,
            result.steps.failed,
            result.llm_usage.llm_calls,
            result.llm_usage.llm_errors,
            result.llm_usage.prompt_tokens,
            result.llm_usage.completion_tokens,
            result.llm_usage.total_tokens,
            result.llm_usage.tool_calls,
            result.cost_usd,
            result.elapsed_ms,
            datetime.now().isoformat(timespec="seconds"),
            result.workspace,
            json.dumps(result.tool_sequence, ensure_ascii=False),
        )

        with self._connect() as conn:
            conn.execute("DELETE FROM spans WHERE task_id = ?", (result.task_id,))
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (result.task_id,))
            conn.execute(
                """INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                task_row,
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
