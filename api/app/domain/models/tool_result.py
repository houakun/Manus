#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/18 15:14
@Author  : thezehui@gmail.com
@File    : tool_result.py
"""
from typing import Optional, TypeVar, Generic

from pydantic import BaseModel

T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    """工具结果Domain模型"""
    success: bool = True  # 是否成功调用
    message: Optional[str] = ""  # 额外的信息提示
    data: Optional[T] = None  # 工具的执行结果/数据

    # [lab/S6] 结构化错误字段。
    # 为什么加：重试策略、失败归因、评测指标都需要区分"失败类型"，
    # 而不是从 message 字符串里做正则匹配（脆弱且不可枚举）。
    # 三个字段都有默认值 → 对既有工具实现完全向后兼容。
    error_type: Optional[str] = None  # 失败类型：timeout/tool_not_found/sandbox_error/llm_error...
    retryable: bool = False  # 该失败是否值得重试（非幂等工具必须为 False）
    attempts: int = 1  # 实际尝试次数，用于归因"重试是否引发重复副作用"

    @classmethod
    def from_sandbox(cls, code: int, msg: str, data: Optional[T], **kwargs) -> "ToolResult":
        """将从沙箱中返回的API数据转换成工具结果"""
        return cls(
            success=True if code < 300 else False,
            message=msg,
            data=data,
        )
