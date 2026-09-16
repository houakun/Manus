#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""轨迹的文本渲染（树形 + 汇总）。

== 为什么这里是"文档级"重要的一个文件 ==
handoff 第 1 节写明了一个硬约束：**当前模型不支持读图**，截图无法用于观测。
这意味着"把 trace 画成火焰图/Gantt 图"这种常规做法在这个项目里是**不可用**的。
所以轨迹必须有一个**文本形态**的第一等展示方式：
- 树形结构表达因果（谁包住谁）；
- 毫秒偏移表达时序（谁先谁后、瓶颈在哪）；
- 汇总段表达统计（时间花在哪一类操作上）。

这也是一个通用的好设计：文本 trace 可以直接 grep、可以进 CI 日志、可以在
任何终端里看，而图片不行。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from lab.trace.span import Span, SpanKind

# 汇总时按"耗时占比"排名的类型顺序
_KIND_LABEL = {
    SpanKind.TASK: "task",
    SpanKind.STEP: "step",
    SpanKind.TOOL: "tool",
    SpanKind.LLM: "llm",
}


def build_children(spans: List[Span]) -> Dict[Optional[str], List[Span]]:
    """按 parent_id 分组，构造树的邻接表。"""
    children: Dict[Optional[str], List[Span]] = {}
    for span in spans:
        children.setdefault(span.parent_id, []).append(span)
    for group in children.values():
        group.sort(key=lambda s: (s.start_ms, s.span_id))
    return children


def _fmt_duration(ms: Optional[int]) -> str:
    if ms is None:
        return "  -  "
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _key_attrs(span: Span, kind_attrs: bool = True) -> str:
    """把最能说明问题的属性压成一行。"""
    attrs = span.attrs or {}
    parts: List[str] = []

    if span.kind == SpanKind.LLM:
        if attrs.get("prompt_tokens") is not None:
            parts.append(f"in={attrs.get('prompt_tokens')} out={attrs.get('completion_tokens')}")
        if attrs.get("messages"):
            parts.append(f"msgs={attrs['messages']}")
        if attrs.get("prompt_digest"):
            parts.append(f"prompt#{attrs['prompt_digest']}")
    elif span.kind == SpanKind.TOOL:
        if attrs.get("tool_name"):
            parts.append(f"toolset={attrs['tool_name']}")
        if attrs.get("error_type"):
            parts.append(f"error={attrs['error_type']}")
        if attrs.get("attempts") and attrs["attempts"] > 1:
            parts.append(f"attempts={attrs['attempts']}")
        if attrs.get("result_chars"):
            parts.append(f"out={attrs['result_chars']}c")
        # 加固层最要紧的两个信号必须出现在树里：
        # 否则"6 次 write_file 全绿但全文错"这种事在默认视图里完全看不出来。
        if attrs.get("faults_injected"):
            parts.append(f"⚡fault={attrs['faults_injected']}")
        if attrs.get("postcondition_warning"):
            parts.append(f"⚠post={str(attrs['postcondition_warning'])[:50]}")
    elif span.kind == SpanKind.STEP:
        if attrs.get("success") is not None:
            parts.append(f"success={attrs['success']}")
    elif span.kind == SpanKind.TASK:
        for key in ("error_type", "steps", "plan_revisions"):
            if attrs.get(key) is not None:
                parts.append(f"{key}={attrs[key]}")

    if span.error:
        parts.append(f"error={span.error[:60]}")

    return ("  " + " ".join(parts)) if (kind_attrs and parts) else ""


def render_tree(spans: List[Span], *, max_depth: int = 20) -> str:
    """把 span 列表渲染成树形文本。"""
    if not spans:
        return "(空轨迹)"

    children = build_children(spans)
    roots = children.get(None, [])
    if not roots:
        # 数据异常时的兜底：至少不要抛异常吞掉整条 trace
        roots = [s for s in spans if s.kind == SpanKind.TASK] or spans[:1]

    lines: List[str] = []

    def walk(span: Span, prefix: str, is_last: bool, depth: int) -> None:
        if depth > max_depth:
            lines.append(f"{prefix}└── ...(超过最大深度 {max_depth})")
            return

        connector = "└── " if is_last else "├── "
        status_mark = {"ok": "✓", "error": "✗", "running": "…"}.get(span.status, "?")
        kind = _KIND_LABEL.get(span.kind, str(span.kind))
        name = span.name if len(span.name) <= 60 else span.name[:57] + "..."
        lines.append(
            f"{prefix}{connector}[{kind}] {status_mark} {name} "
            f"@{span.start_ms}ms +{_fmt_duration(span.duration_ms)}{_key_attrs(span)}"
        )

        kids = children.get(span.span_id, [])
        child_prefix = prefix + ("    " if is_last else "│   ")
        for index, kid in enumerate(kids):
            walk(kid, child_prefix, index == len(kids) - 1, depth + 1)

    for index, root in enumerate(roots):
        walk(root, "", index == len(roots) - 1, 0)

    return "\n".join(lines)


def summarize(spans: List[Span], *, top_n: int = 5) -> Dict[str, Any]:
    """汇总统计：各类操作的次数、总耗时、占比、最慢的 span、错误 span。"""
    by_kind: Dict[str, Dict[str, Any]] = {}
    for span in spans:
        key = _KIND_LABEL.get(span.kind, str(span.kind))
        bucket = by_kind.setdefault(key, {"count": 0, "total_ms": 0, "errors": 0})
        bucket["count"] += 1
        bucket["total_ms"] += span.duration_ms or 0
        if span.status == "error":
            bucket["errors"] += 1

    # 注意：父 span 的耗时天然包含子 span，所以"总耗时"只能用来横向比较同一类操作，
    # 不能把各类相加当成任务总耗时（否则会重复计数）。文档里说清楚，避免误读。
    slowest = sorted(
        (s for s in spans if s.duration_ms is not None and s.kind != SpanKind.TASK),
        key=lambda s: s.duration_ms or 0,
        reverse=True,
    )[:top_n]

    errors = [s for s in spans if s.status == "error"]

    return {
        "span_count": len(spans),
        "by_kind": by_kind,
        "slowest": [
            {"kind": _KIND_LABEL.get(s.kind, str(s.kind)), "name": s.name[:60],
             "duration_ms": s.duration_ms, "status": s.status}
            for s in slowest
        ],
        "error_spans": [
            {"kind": _KIND_LABEL.get(s.kind, str(s.kind)), "name": s.name[:60],
             "error_type": (s.attrs or {}).get("error_type"), "error": (s.error or "")[:120]}
            for s in errors
        ],
    }


def render_summary(spans: List[Span], *, top_n: int = 5) -> str:
    """渲染汇总段。"""
    data = summarize(spans, top_n=top_n)
    lines = ["", "--- 汇总 ---"]

    lines.append(f"{'类型':<6} {'次数':>5} {'累计耗时':>10} {'错误':>5}")
    for kind, bucket in sorted(data["by_kind"].items(), key=lambda kv: -kv[1]["total_ms"]):
        lines.append(
            f"{kind:<6} {bucket['count']:>5} {_fmt_duration(bucket['total_ms']):>10} {bucket['errors']:>5}"
        )
    lines.append("(注：父 span 耗时包含子 span，各类耗时不可相加为任务总耗时)")

    if data["slowest"]:
        lines.append("")
        lines.append(f"最慢的 {len(data['slowest'])} 个操作：")
        for item in data["slowest"]:
            lines.append(
                f"  {_fmt_duration(item['duration_ms']):>7}  [{item['kind']}] {item['name']}"
            )

    if data["error_spans"]:
        lines.append("")
        lines.append(f"出错的 span（{len(data['error_spans'])} 个）：")
        for item in data["error_spans"]:
            lines.append(
                f"  [{item['kind']}] {item['name']}  error_type={item['error_type']}  {item['error']}"
            )

    return "\n".join(lines)


def render_trace(task: Optional[Dict[str, Any]], spans: List[Span], *, top_n: int = 5) -> str:
    """渲染完整的一份 trace（任务头部 + 树 + 汇总）。"""
    lines: List[str] = []
    if task:
        ok = "OK " if task.get("ok") else "FAIL"
        lines.append("=" * 78)
        lines.append(
            f"[{ok}] {task.get('sut_name')}  task={task.get('task_id')}"
        )
        lines.append(f"目标   : {(task.get('goal') or '')[:70]}")
        lines.append(f"计划   : {(task.get('plan_title') or '')[:70]}")
        lines.append(
            f"结果   : steps={task.get('steps_succeeded')}/{task.get('steps_total')} "
            f"tools={task.get('tool_calls')} llm={task.get('llm_calls')} "
            f"tokens={task.get('total_tokens')} cost=${(task.get('cost_usd') or 0):.4f} "
            f"elapsed={task.get('elapsed_ms')}ms"
        )
        if task.get("error"):
            lines.append(f"失败   : [{task.get('error_type')}] {task.get('error')[:100]}")
        lines.append("=" * 78)

    lines.append(render_tree(spans))
    lines.append(render_summary(spans, top_n=top_n))
    return "\n".join(lines)
