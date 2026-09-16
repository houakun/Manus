#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/17 17:21
@Author  : thezehui@gmail.com
@File    : openai_llm.py
"""
import logging
import time
from typing import List, Dict, Any

from openai import AsyncOpenAI

from app.application.errors.exceptions import ServerRequestsError
from app.domain.external.llm import LLM
from app.domain.models.app_config import LLMConfig

logger = logging.getLogger(__name__)


class OpenAILLM(LLM):
    """基于OpenAI SDK/兼容OpenAI格式的LLM调用类"""

    def __init__(self, llm_config: LLMConfig, **kwargs) -> None:
        """构造函数，完成异步OpenAI客户端的创建和参数初始化"""
        # 1.初始化异步客户端
        self._client = AsyncOpenAI(
            base_url=str(llm_config.base_url),
            api_key=llm_config.api_key,
            **kwargs,
        )

        # 2.完成其他参数的存储
        self._model_name = llm_config.model_name
        self._temperature = llm_config.temperature
        self._max_tokens = llm_config.max_tokens
        self._timeout = 3600

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    async def invoke(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        """使用异步OpenAI客户端发起块响应（该步骤可以切换成流式响应）"""
        # [lab/S2] 记录起始时间，用于计算本次调用的真实时延
        started_at = time.monotonic()
        try:
            # 1.检测是否传递了工具列表
            if tools:
                logger.info(f"调用OpenAI客户端向LLM发起请求并携带工具信息: {self._model_name}")
                response = await self._client.chat.completions.create(
                    model=self._model_name,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    messages=messages,
                    response_format=response_format,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=False,  # 关闭并行工具调用(deepseek没有这个参数的)
                    timeout=self._timeout,
                )
            else:
                # 2.为传递工具则删除tools/tool_choice等参数
                logger.info(f"调用OpenAI客户端向LLM发起请求未携带: {self._model_name}")
                response = await self._client.chat.completions.create(
                    model=self._model_name,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    messages=messages,
                    response_format=response_format,
                    timeout=self._timeout,
                )

            # 3.处理响应数据并返回
            logger.info(f"OpenAI客户端返回内容: {response.model_dump()}")
            message = response.choices[0].message.model_dump()

            # [lab/S2] 透出 token 用量与调用时延。
            #   设计要点：
            #   1) 用下划线前缀（_usage/_latency_ms/_model）表示"附着在返回体上的私有遥测数据"，
            #      不新增、不修改任何原有键 → 对 SUT 内部所有调用方完全透明（零行为变更）；
            #   2) lab 侧用 CountingLLM 代理在拿到返回体的这一刻采集数据并把这些键摘掉，
            #      因此 SUT 的记忆/消息里不会残留无关键，不会浪费 token。
            #   3) 改造前 response.usage 是直接丢掉的，导致"无法回答本次任务花了多少 token/多少钱"。
            message["_usage"] = response.usage.model_dump() if getattr(response, "usage", None) else None
            message["_latency_ms"] = int((time.monotonic() - started_at) * 1000)
            message["_model"] = self._model_name
            return message
        except Exception as e:
            logger.error(f"调用OpenAI客户端发生错误: {str(e)}")
            # [lab/D3-bugfix] 补上 `from e`（异常链）。
            #   改造前这里丢掉了原始异常，日志里只剩下"调用OpenAI客户端向LLM发起请求出错"，
            #   无法区分 401/429/超时/网络不通，排障时只能靠猜。
            raise ServerRequestsError("调用OpenAI客户端向LLM发起请求出错") from e


if __name__ == "__main__":
    import asyncio


    async def main():
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="",
            model_name="deepseek-chat",
        ))
        response = await llm.invoke([{"role": "user", "content": "Hi"}])
        print(response)


    asyncio.run(main())
