#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务模型与加载器。

== 一个任务由四部分组成 ==
    goal       给 Agent 的自然语言目标（**唯一**传给 SUT 的东西）
    fixtures   运行前铺到工作区里的输入文件（CSV、日志、代码……）
    verify     确定性判定规则（只看最终产物，不看 Agent 的自述）
    process    过程规则（效率 / 越权 / 硬编码）
    solution   参考解（**不参与真实评测**，只用于验证"任务本身是否可解、验证器是否正确"）
    wrong      错误解（用于验证"验证器不会接受错答案"）

== 为什么 solution / wrong 要写在任务里 ==
因为**任务集本身必须被验证**。一个"永远通过"的验证器会给出 100% 成功率的
假象；一个"永远失败"的验证器会让所有加固看起来都没用。
所以每个任务都要能通过三步自检（见 validate.py），
而三步自检需要"正确的做法"和"错误的做法"这两个参照物。

== 为什么 goal 里要写清输出路径 ==
判定器依赖固定路径读取产物。如果让 Agent 自己决定文件名，验证就会变成
"在目录里找找看哪个像答案"—— 那既不严谨也无法自动化。
代价是任务比真实场景更"规定化"，这是确定性的必要成本（Step 3 已记录该限制）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


class Fixture(BaseModel):
    """运行前铺到工作区的输入。"""

    path: str  # 沙箱逻辑路径，如 /home/ubuntu/input/data.csv
    content: str = ""


class VerifyCheck(BaseModel):
    """一条确定性判定规则。

    只保留"可以由程序客观判断"的检查。刻意**不支持** LLM judge：
    handoff 决策 2 要求先建立确定性基线，judge 是后续才引入的校准对象。
    """

    kind: str  # file_content / file_exists / json_equals / csv_rows / numeric / line_count / dir_count / no_extra_files
    path: Optional[str] = None
    # --- 内容比较 ---
    equals: Optional[str] = None
    contains: Optional[str] = None
    not_contains: Optional[str] = None
    matches: Optional[str] = None  # 正则
    # --- 归一化：让"格式差异"不成为失败原因（例如结尾换行） ---
    normalize: str = "strip"  # strip / none / lower / collapse_ws / sorted_lines
    # --- 数值比较 ---
    value: Optional[float] = None
    tolerance: float = 1e-6
    # --- 结构比较 ---
    rows: Optional[List[List[str]]] = None
    count: Optional[int] = None
    allow: Optional[List[str]] = None
    # --- 存在性 ---
    exists: Optional[bool] = None
    # --- 说明（失败时给人类看） ---
    description: Optional[str] = None


class ProcessRule(BaseModel):
    """过程规则：判定"答案对但路径错"的情况（handoff 决策 5）。"""

    max_tool_calls: Optional[int] = None
    max_llm_calls: Optional[int] = None
    max_steps: Optional[int] = None
    forbid_path_escape: bool = True  # 不允许尝试越权访问沙箱外的路径
    # 硬编码检测：如果这些字面量直接出现在 write_file 的内容里，说明是抄答案而不是算出来的。
    # 这是个**启发式**（可能误报），所以它只进 process_flags，不影响 ok。
    answer_literals: List[str] = Field(default_factory=list)


class SolutionStep(BaseModel):
    """参考解 / 错误解的一步。"""

    kind: str  # write_file | exec
    path: Optional[str] = None
    content: Optional[str] = None
    command: Optional[str] = None


class BenchTask(BaseModel):
    """一个评测任务。"""

    key: str
    group: str = "synthetic"  # synthetic | semireal
    title: str = ""
    goal: str
    tags: List[str] = Field(default_factory=list)

    fixtures: List[Fixture] = Field(default_factory=list)
    verify: List[VerifyCheck] = Field(default_factory=list)
    process: ProcessRule = Field(default_factory=ProcessRule)

    solution: List[SolutionStep] = Field(default_factory=list)
    wrong: List[SolutionStep] = Field(default_factory=list)

    max_seconds: float = 300.0
    notes: str = ""

    @property
    def uid(self) -> str:
        return f"{self.group}/{self.key}"


def load_task(path: Path) -> BenchTask:
    """从 YAML 载入一个任务。"""
    with open(path, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    data.setdefault("key", path.stem)
    data.setdefault("group", path.parent.name)
    return BenchTask.model_validate(data)


def load_all_tasks(group: Optional[str] = None) -> List[BenchTask]:
    """载入全部任务（按 group/key 排序，保证执行顺序稳定可复现）。"""
    tasks: List[BenchTask] = []
    for file in sorted(TASKS_DIR.glob("*/*.yaml")):
        task = load_task(file)
        if group and task.group != group:
            continue
        tasks.append(task)
    return sorted(tasks, key=lambda t: (t.group, t.key))


def summarize_tasks(tasks: List[BenchTask]) -> Dict[str, Any]:
    """任务集概览（进报告，便于读者判断覆盖面）。"""
    by_group: Dict[str, int] = {}
    tag_counts: Dict[str, int] = {}
    for task in tasks:
        by_group[task.group] = by_group.get(task.group, 0) + 1
        for tag in task.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return {
        "total": len(tasks),
        "by_group": by_group,
        "by_tag": dict(sorted(tag_counts.items(), key=lambda kv: -kv[1])),
    }
