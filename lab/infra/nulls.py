#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""空实现（Null Object）：fast mode 下替代浏览器与搜索引擎。

为什么用"空对象"而不是传 None 或直接抛异常：
- SUT 的 PlannerReActFlow 构造函数要求 browser / search_engine 参数存在。
  传 None 能跑通，但一旦有人把 BrowserTool 装回工具集，就会在运行到一半时
  抛出 `AttributeError: 'NoneType' object has no attribute 'view_page'`，
  而且是在深度调用栈里炸掉整条 flow；
- 返回"结构化失败"则让 Agent 能自己降级（读到 success=false 后换工具），
  这与我们在 SUT 里修的 D4 是同一个设计原则：
  **可预期的失败要走事件/返回值，不要走异常。**

注意：fast mode 下这些对象**根本不会进入 LLM 的工具清单**（见 ManusSUT._build_tools），
它们只是为了让 SUT 的构造函数契约成立，并为未来的"部分能力模式"留好接缝。
"""

from __future__ import annotations

from typing import Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402

_UNAVAILABLE_MSG = "当前运行模式未启用浏览器/搜索能力（fast mode，无 Docker 沙箱）"


def _unavailable(**data) -> ToolResult:
    return ToolResult(
        success=False,
        message=_UNAVAILABLE_MSG,
        data=data or None,
        error_type="unsupported",
        retryable=False,
    )


class NullBrowser:
    """Browser 协议的空实现：所有操作返回结构化失败。"""

    async def view_page(self) -> ToolResult:
        return _unavailable()

    async def navigate(self, url: str) -> ToolResult:
        return _unavailable(url=url)

    async def restart(self, url: str) -> ToolResult:
        return _unavailable(url=url)

    async def click(
            self,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        return _unavailable(index=index, coordinate_x=coordinate_x, coordinate_y=coordinate_y)

    async def input(
            self,
            text: str,
            press_enter: bool,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        return _unavailable(index=index)

    async def move_mouse(self, coordinate_x: float, coordinate_y: float) -> ToolResult:
        return _unavailable(coordinate_x=coordinate_x, coordinate_y=coordinate_y)

    async def press_key(self, key: str) -> ToolResult:
        return _unavailable(key=key)

    async def select_option(self, index: int, option: int) -> ToolResult:
        return _unavailable(index=index, option=option)

    async def scroll_up(self, to_top: Optional[bool] = None) -> ToolResult:
        return _unavailable()

    async def scroll_down(self, to_down: Optional[bool] = None) -> ToolResult:
        return _unavailable()

    async def screenshot(self, full_page: Optional[bool] = None) -> bytes:
        """截图返回空字节：调用方（BrowserTool）会把它转成失败结果。"""
        return b""

    async def console_exec(self, javascript: str) -> ToolResult:
        return _unavailable()

    async def console_view(self, max_lines: Optional[int] = None) -> ToolResult:
        return _unavailable()


class NullSearchEngine:
    """SearchEngine 协议的空实现。

    Step 1 刻意**不接**真实搜索（哪怕 SUT 里已经有 Bing 抓取实现）：
    评测要可复现，而真实搜索结果每天都在变，任务断言会随机失败。
    真正需要联网的任务属于"非确定性任务集"，Step 4 会单独分组处理。
    """

    async def invoke(self, query: str, date_range: Optional[str] = None) -> ToolResult:
        return _unavailable(query=query, date_range=date_range)
