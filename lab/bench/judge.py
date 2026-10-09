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

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# rubric 版本：**改提示词就必须改它**（否则不同版本的人工金标会被混在一起算 κ）。
#
# **v2 起成为默认**：把 `instruction_following` / `output_quality` 的**边界**写进提示词。
# 依据是**对照实验**（同一批 111 条、同一份金标、**同一份代码**，只切 rubric 版本）：
#
#   instruction_following κ  0.208 → **0.556**（“人✓judge✗” 32 → 11）
#   output_quality       κ  0.978 → **0.842**（1+0 → 4+3）
#   task_completion      κ  0.977 → 0.977（不变）
#
# → 这是**转移、不是白拿**：总分歧 33 → 15，但两维都有代价；**且 v2 仍不达标**
#   （0.556 < 0.6）。采纳的理由是“消除了真实存在的规格歧义 + 总分歧更少”，
#   而不是“它把指标顶上去了”。完整记录：`docs/step14-judge-rubric-v2-experiment.md`。
JUDGE_RUBRIC_VERSION = "v2"

# 三个维度：各自独立调用，互不牵连。
JUDGE_DIMENSIONS = ("task_completion", "instruction_following", "output_quality")

# ==================== 维度边界：实测定下来的，不是猜的 ====================
#
# `instruction_following` 与 `output_quality` 的边界最容易糊 —— 两轮校准里**绝大多数分歧
# 都集中在这里**：111 条样本中有 **28 条**是“人工判 instruction_following=true 而 judge 判 false”，
# 而原因都是同一件：**judge 把“内容错”读成了“约束没遵守”**
# （例如诊断说明里写错了数字、配置里的端口号写错 —— 那些属于 output_quality）。
#
# 本项目采用的边界（供人工标注与未来的 rubric 版本对齐）：
#   - `instruction_following` = **显式约束的“形状”**：要求的文件在不在、路径/文件名对不对、
#     结构/字段名/表头顺序对不对、有没有碰不该碰的冻结文件。**不看内容对不对**。
#   - `output_quality` = **内容正确性**：数值对不对、文字是不是事实、有没有残缺或占位。
#   - 归因规则：**内容错但形状对 → `output_quality=false`、`instruction_following=true`**。
#
# ⚠️ v1 下这段只是**注释级约定**（喂给模型的文本里没写边界）。
# `LAB_JUDGE_RUBRIC=v2` 会启用把边界**写进提示词**的版本 —— 用来做对照实验：
# 它到底**判得更对**，还是只是**更合人意**？
#
# 为什么用**环境变量**而不是改代码：两个臂必须跑在**同一份代码**上，否则
# “差异来自提示词”这句话就不成立。（对照组用 `LAB_JUDGE_RUBRIC=v1` 重跑即可。）
RUBRIC_VERSION_ENV = "LAB_JUDGE_RUBRIC"

_RUBRIC_V1: Dict[str, str] = {
    "task_completion": "目标的**核心要求**是否真的完成了（产物能不能证明它完成了）",
    "instruction_following": "格式/路径/命名等**显式约束**是否遵守（例如要求的输出文件、字段名）",
    "output_quality": "产物本身的质量（内容是否正确、是否明显残缺或占位）",
}

# v2：把"形状 vs 内容"的边界写死在**给模型看的文本**里。
_RUBRIC_V2: Dict[str, str] = {
    "task_completion": "目标的**核心要求**是否真的完成了（产物能不能证明它完成了）",
    "instruction_following": (
        "**显式约束的“形状”**是否遵守 —— 只看这些：要求的文件在不在、路径/文件名对不对、"
        "结构/字段名/表头顺序对不对、有没有碰不该碰的文件（如冻结的测试文件）。"
        "**不要判断内容对不对**：数值/文字/事实错了属于 output_quality，**不算这一维的违反**。"
    ),
    "output_quality": (
        "**内容正确性**：数值对不对、文字是不是事实、有没有残缺或占位。"
        "只判内容，不判路径/命名（那些属于 instruction_following）。"
    ),
}

RUBRIC_BY_VERSION: Dict[str, Dict[str, str]] = {"v1": _RUBRIC_V1, "v2": _RUBRIC_V2}
JUDGE_RUBRIC_VERSIONS = tuple(RUBRIC_BY_VERSION)


def rubric_for(version: str) -> Dict[str, str]:
    """取某个版本的 rubric（未知版本→**响亮地失败**，而不是悄悄回退到 v1）。"""
    if version not in RUBRIC_BY_VERSION:
        raise ValueError(f"未知 rubric 版本 {version!r}（可选：{JUDGE_RUBRIC_VERSIONS}）")
    return RUBRIC_BY_VERSION[version]


def active_rubric_version() -> str:
    """当前生效的版本（`LAB_JUDGE_RUBRIC` 可覆盖，默认 v1）。"""
    import os

    version = (os.environ.get(RUBRIC_VERSION_ENV) or JUDGE_RUBRIC_VERSION).strip()
    rubric_for(version)  # 值非法就当场报错
    return version


# 默认版本的 rubric（兼容既有引用；实际取用走 `rubric_for(active_rubric_version())`）
RUBRIC: Dict[str, str] = _RUBRIC_V1

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
                f"评审维度：{dimension} —— {rubric_for(active_rubric_version()).get(dimension, '')}\n\n"
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
    # 下面这行被 ast-grep 的 `no-boolean-in-except` 报为 error，但它**是误报**：
    # 该规则想抓的是 `except (A or B)`（`or` 会先求值 → 抓不到异常），而它实现里的
    # `stopBy: end` 会连 except **体**一起扫，于是命中体里的 `text or ""`。
    # 这里 `(TypeError, ValueError)` 是合法的异常元组，所以按项目惯例（见 ruff.toml 对 S608
    # 的处理）**记录误报**而不是为迁就工具去改正确的代码。
    except (TypeError, ValueError) as exc:  # pi-lens-ignore: no-boolean-in-except
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
    return JudgeReport(verdicts=verdicts, rubric_version=active_rubric_version())


# ==================== 跑 judge：从工作区收集材料（T14 收尾） ====================

# 为什么不能复用 `bench show` 的产物渲染：两者**目的**不同。
# `bench show` 是给人看"Agent 交了什么"，所以排除 `input/`（那是题目素材）；
# 而 judge 要判断"目标有没有达成"，需要工作区**全貌** —— 例如
# `sem_refactor_config` 要求改写题目给的 `input/service.py`，排除它 judge 就无从判断。
# 代价是 prompt 变长，所以下面三个上限必须存在（judge 的成本要可预估）。
ARTIFACT_MAX_FILES = 40
ARTIFACT_MAX_CHARS_PER_FILE = 1500
ARTIFACT_MAX_TOTAL_CHARS = 12000


def collect_artifacts(
    workspace: str,
    *,
    max_files: int = ARTIFACT_MAX_FILES,
    max_chars_per_file: int = ARTIFACT_MAX_CHARS_PER_FILE,
    max_total_chars: int = ARTIFACT_MAX_TOTAL_CHARS,
) -> str:
    """把工作区里的文本材料拼成 judge 的输入（**材料不足时要如实说**）。

    - 排除 `__pycache__`；二进制按 NUL 字节识别（否则会把 `.pyc` 打成乱码进 prompt）；
    - `input/` **保留**并标注来源（有的任务要求改写题目给的源文件）；
    - 逐文件 + 总量双重截断：没有上界的 prompt 会让成本不可预估。
    """
    root = Path(workspace) / "home" / "ubuntu"
    if not root.is_dir():
        return "（工作区不存在：没有可判断的产物）"
    files = sorted(f for f in root.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    if not files:
        return "（工作区里没有任何文件）"

    parts: List[str] = []
    used = 0
    for path in files[:max_files]:
        rel = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        tag = "（题目输入，可能被 Agent 改写）" if rel.startswith("input/") else ""
        if b"\x00" in raw[:2048]:
            parts.append(f"--- {rel} {tag}({len(raw)}B，二进制，未展开)")
            continue
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        body = text[:max_chars_per_file]
        suffix = "…（截断）" if len(text) > len(body) else ""
        block = f"--- {rel} {tag}\n{body}{suffix}"
        if used + len(block) > max_total_chars:
            parts.append("…（其余文件因总量上限未展开）")
            break
        parts.append(block)
        used += len(block)
    if len(files) > max_files:
        parts.append(f"…（另有 {len(files) - max_files} 个文件未列出）")
    return "\n\n".join(parts)


async def judge_entries(
    llm: Any,
    entries: List[Dict[str, str]],
    *,
    dimensions: Optional[List[str]] = None,
    concurrency: int = 4,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_sample: Optional[Callable[[str, Dict[str, Optional[bool]]], None]] = None,
) -> Dict[str, Dict[str, Optional[bool]]]:
    """对多条样本跑 judge，返回 `{sample_id: {dimension: bool | None}}`。

    - 每个维度**独立调用**（复用 `judge_run`，不合并 prompt）；
    - 返回 `None` 而不是省略键：`calibrate` 的契约是"None = Unknown"；
    - 单条样本失败不会毁掉整批（`judge_dimension` 已把异常转成 Unknown）；
    - `on_sample` 每完成一条就回调一次 —— 294 次调用要跑十几分钟，
      被中断的途径很多（Ctrl-C / 休眠 / 断网），**每条就落盘**才能做到
      "最坏情况只丢一条"而不是"丢掉整批"（与评测的增量落库同一个理由）。
    """
    dims = list(dimensions or JUDGE_DIMENSIONS)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    out: Dict[str, Dict[str, Optional[bool]]] = {}
    done = 0

    async def _one(entry: Dict[str, str]) -> None:
        nonlocal done
        async with semaphore:
            report = await judge_run(
                llm,
                goal=entry.get("goal", ""),
                artifacts=entry.get("artifacts", ""),
                dimensions=dims,
            )
            verdicts = {v.dimension: v.verdict for v in report.verdicts}
            out[entry["sample_id"]] = verdicts
            done += 1
            if on_sample is not None:
                on_sample(entry["sample_id"], verdicts)
            if on_progress is not None:
                on_progress(done, len(entries))

    await asyncio.gather(*(_one(entry) for entry in entries))
    return out
