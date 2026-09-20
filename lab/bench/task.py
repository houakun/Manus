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
    # --- 执行型检查（kind=exec_script）---
    # 一段**来自任务定义**的 Python 脚本；判定器把它写到沙箱里执行，检查退出码。
    #
    # 为什么需要这个原语（真实踩到的漏洞）：
    # 文件读取型检查有一个绕不过去的弱点 —— 只要期望值是可推导的，
    # 一个"直接写产物、不改代码"的解就能过关。实测确认过：
    # 两个 ci 任务都能被"手写 out.json + 手写 diagnosis.json"绕过。
    # 而"CI 失败归因"这类任务的核心恰恰是"你有没有真的把根因修好"，
    # 光看产物根本区分不了"跑出来的"和"编出来的"。
    #
    # 关键区别：脚本内容存在**任务定义**里，不在工作区里 ——
    # 所以 Agent 无法篡改它（对比：执行工作区的测试文件，Agent 可以先把它改弱）。
    script: Optional[str] = None
    script_cwd: Optional[str] = None  # 执行时的工作目录（逻辑路径），默认 /home/ubuntu
    expect_exit: int = 0
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


class SelfCheck(BaseModel):
    """"fixture 必须是真实的"这一条的自检规则。

    == 为什么必须验证"CI 日志"本身（真实踩到的缺陷）==
    `ci` 类任务把一份 CI 日志当作 Agent 的**主要输入**。
    而"日志"是一个人写出来的文本 —— 它可能与代码的**实际行为不一致**。

    实测踩到：`ci_window_boundary` 的日志写了
        `test_page_zero_is_unaffected ... PASSED`
    但那条 case 实际跑出来是 FAILED（off-by-one 同样会让第 0 页少一条）。
    后果很隐蔽也很严重：**Agent 拿到的"现象"是假的**，
    它要么被误导去改一个本来就没问题的东西，要么花大量步骤去调和一个矛盾。

    为什么三次自检抓不到：`fixtures_only` / `reference_solution` / `wrong_solution`
    跑的都是 `verify`，而日志只是一份 fixture —— **没有人执行过它**。
    这和历史上那次事故是同一个形状：参考解"把期望值写出来"，
    就不可能发现期望值本身写错（见 `_tautology_flags`）。

    == 规则 ==
    在"只铺 fixtures"的工作区里真跑一遍 `command`，然后：
      1. 退出码必须等于 `expect_exit`；
      2. 实际输出的**每一行**都必须出现在 `output_recorded_in` 那个文件里。

    第 2 条用"包含"而不是"相等"：日志通常还有 `$ command` 头、时间戳、总结行等装饰，
    要求逐字节相等会让任务作者为了过自检而删掉那些真实性细节。
    而反向的"日志里的每一行都在实际输出里"**故意不做** ——
    装饰行不应被判为造假。但关键结论行（PASSED/FAILED）两边对不上时，
    实际输出的那一行一定进不了日志 → 第 2 条就会报。
    """

    command: str
    output_recorded_in: str
    expect_exit: int = 1


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
    # 任务用途：capability = 探测能力边界（允许不稳定）；regression = 回归门禁用（要求稳定）。
    #
    # 为什么要这个（审计 §4.9）：semireal 大部分任务已经 100%（已饱和），
    # 100% 的任务**提不了改进信号，只能追回归**；而少数不稳定任务（如
    # `sem_markdown_toc` ~70%）会淹掉 A/B 实验里的小效应（实测：8.3pt 的差异
    # 完全由它一个任务的 3 次运行决定）。两者必须分开看：
    #   - regression 任务上的 pass^k 才是「回归门禁」的指标；
    #   - capability 任务上的成功率是「能力边界」的描述，它的波动是发现，不是噪声。
    purpose: str = "regression"

    solution: List[SolutionStep] = Field(default_factory=list)
    wrong: List[SolutionStep] = Field(default_factory=list)
    # 可选的"fixture 真实性"自检（见 SelfCheck）。只对"输入是一份日志/报告"
    # 这类任务有意义，所以是可选而不是必填。
    selfcheck: Optional[SelfCheck] = None

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


def task_purposes() -> Dict[str, str]:
    """任务 uid → purpose（capability / regression）。

    为什么放在这里而不是报告里：**报告与回归门禁都必须用同一个口径**。
    两处各写一份的话，早晚会一个改了一个没改（而这类不一致恰恰是静默的）。
    """
    try:
        return {task.uid: task.purpose for task in load_all_tasks()}
    except Exception:  # noqa: BLE001
        return {}


def regression_uids() -> set:
    """只属于 regression 的任务 uid 集合（回归口径的唯一真源）。"""
    return {uid for uid, purpose in task_purposes().items() if purpose == "regression"}


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
