#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""轨迹（trace）子系统：把 SUT 的事件流重建成可查询、可统计的 span 树。

设计取舍见 span.py 顶部注释（为什么不用 contextvars、为什么 lab 侧重建）。
"""

from lab.trace.span import Span, SpanHandle, SpanKind, SpanSink, TraceRecorder
from lab.trace.store import SpanStore

__all__ = ["Span", "SpanHandle", "SpanKind", "SpanSink", "TraceRecorder", "SpanStore"]
