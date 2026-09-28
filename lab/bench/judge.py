#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM-as-judge：**按维度独立调用**，并允许回答"不知道"（T14 / 审计 §1④）。

== 为什么这个模块存在，以及为什么它必须是最后才引入的东西 ==
它是审计里**唯一完全为零**的高分项；但项目从 Step 1 就定了一条决策（handoff 决策 2）：
**先建立确定性基线，judge 是后续才引入的校准对象**。
原因是 LLM judge 本身不可信 —— 它自己就是个需要被测量的东西。所以：

1. 它**不参与**任何既有指标（`verify` 仍然只跑确定性检查，`bench task` 里明确写了"不支持 judge"）；
2. 它必须能被校准：`lab/bench/calibrate.py` 用人工金标算 Cohen's κ，
   **κ 低于阈值就拒绝启用**（而不是"先用了再说"）；
3. 它按**维度**独立调用：一个 prompt 让模型同时判三件事，会把不同维度的错误混在一起，
   校准也就分不清"哪一维不可信"。

== 三条不可动摇的纪律 ==
- **允许"Unknown"**：模型说"看不出来"是**有效答案**，而且**不计入 κ 的分母**
  （把"不确定"算成错会让一致性被无谓地拉低；算成对则更糟）。
- **不看 Agent 自述**：judge 的输入是**产物与目标**，不是 SUT 的 answer 字段
  （实测过 SUT "0 个步骤却报成功"，自述只配用来交叉校验）。
- **rubric 带版本号**：改了提示词就必须改版本号，否则跨版本的金标不能混用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# rubric 版本：**改提示词就必须改它**（否则不同版本的人工金标会被混在一起算 κ）。
JUDGE_RUBRIC_VERSION = "v1"

# 三个维度：各自独立调用，互不牵连。
JUDGE_DIMENSIONS = ("task_completion", "instruction_following", "output_quality")

RUBRIC: Dict[str, str] = {
    "task_completion": "目标的**核心要求**是否真的完成了（产物能不能证明它完成了）",
    "instruction_following": "格式/路径/命名等**显式约束**是否遵守（例如要求的输出文件、字段名）",
    "output_quality": "产物本身的质量（内容是否正确、是否明显残缺或占位）",
}

_SYSTEM = """你是一个严格但保守的评审。只根据**给出的材料**判断，不要猜测没看到的东西。

你必须只输出一个 JSON 对象，形如：
{"verdict": true | false | "unknown", "reason": "一句话理由"}

三种取值的含义：
- true  = 材料足以证明这一维度**做到了**；
- false = 材料足以证明这一维度**没做到**；
- "unknown" = 材料不足以判断（**这是合法答案**，不要为了给出结论而猜）。"""


@dataclass
class JudgeVerdict:
    """一维的评审结论。`verdict is None` 表示 Unknown。"""

    dimension: str
    verdict: Optional[bool]
    reason: str = ""
    raw: str = ""
    error: Optional[str] = None

    @property
    def unknown(self) -> bool:
        return self.verdict is None


@dataclass
class JudgeReport:
    """一次运行的全部维度评审（rubric 版本随包带出）。"""

    verdicts: List[JudgeVerdict] = field(default_factory=list)
    rubric_version: str = JUDGE_RUBRIC_VERSION

    @property
    def decided(self) -> List[JudgeVerdict]:
        return [v for v in self.verdicts if not v.unknown]

    @property
    def unknown_ratio(self) -> float:
        if not self.verdicts:
            return 0.0
        return sum(1 for v in self.verdicts if v.unknown) / len(self.verdicts)


def build_prompt(goal: str, artifacts: str, dimension: str) -> List[Dict[str, str]]:
    """构造**单维度**的评审提示（维度独立调用是校准可解释的前提）。"""
    return [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                f"评审维度：{dimension} —— {RUBRIC.get(dimension, '')}\n\n"
                f"【任务目标】\n{goal.strip()}\n\n"
                f"【实际产物（截断）】\n{artifacts.strip()[:6000]}\n\n"
                "请只输出那个 JSON 对象。"
            ),
        },
    ]


_JSON_BLOB = re.compile(r"\{.*\}", re.S)


def parse_verdict(dimension: str, text: str) -> JudgeVerdict:
    """解析模型输出。**解析不出来一律算 Unknown**，绝不当成 true/false。

    为什么这么保守：把解析失败当成"判定为真"会伪造一致性 ——
    而这一节的整个目的就是**测量 judge 有多不可信**。
    """
    match = _JSON_BLOB.search(text or "")
    if not match:
        return JudgeVerdict(dimension=dimension, verdict=None, raw=text or "",
                            error="输出里找不到 JSON 对象")
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError) as exc:
        return JudgeVerdict(dimension=dimension, verdict=None, raw=text or "",
                            error=f"JSON 解析失败: {exc}")
    value = payload.get("verdict")
    reason = str(payload.get("reason") or "")
    if isinstance(value, bool):
        return JudgeVerdict(dimension=dimension, verdict=value, reason=reason, raw=text or "")
    if isinstance(value, str) and value.strip().lower() == "unknown":
        return JudgeVerdict(dimension=dimension, verdict=None, reason=reason, raw=text or "")
    return JudgeVerdict(dimension=dimension, verdict=None, reason=reason, raw=text or "",
                        error=f"verdict 字段非法: {value!r}")


async def judge_dimension(llm: Any, *, goal: str, artifacts: str, dimension: str) -> JudgeVerdict:
    """对一个维度做一次评审。LLM 抛异常 → 该维度记为 Unknown（不让整条链崩掉）。"""
    try:
        message = await llm.invoke(build_prompt(goal, artifacts, dimension))
    except Exception as exc:  # noqa: BLE001
        return JudgeVerdict(dimension=dimension, verdict=None,
                            error=f"LLM 调用失败: {type(exc).__name__}: {exc}")
    content = message.get("content") if isinstance(message, dict) else str(message)
    return parse_verdict(dimension, content or "")


async def judge_run(
    llm: Any, *, goal: str, artifacts: str, dimensions: Optional[List[str]] = None
) -> JudgeReport:
    """按维度**逐个独立调用**（不合并成一个 prompt）。"""
    verdicts = []
    for dimension in dimensions or list(JUDGE_DIMENSIONS):
        verdicts.append(await judge_dimension(llm, goal=goal, artifacts=artifacts,
                                              dimension=dimension))
    return JudgeReport(verdicts=verdicts)
