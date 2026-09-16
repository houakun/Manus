#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务集自检：**在跑真模型之前**先证明任务集本身是可信的。

== 为什么这一步不能省 ==
一个任务集有三种常见的致命缺陷，而且它们都不会报错：

1. **验证器太松**：无论 Agent 做什么都判通过 → 报告里出现虚高的成功率。
2. **验证器太严**：正确的做法也被判失败 → 所有加固看起来都"没用"，
   于是你去改 Agent，改了半天其实该改的是验证器。
3. **参考解本身就是错的**：任务根本不可解 → 成功率永远上不去，
   而你会以为是模型能力问题。

所以每个任务都要过三步：

| 步骤 | 铺什么 | 期望判定器结论 | 抓什么缺陷 |
|---|---|---|---|
| fixtures_only | 只铺输入文件 | **失败** | 验证器太松（空文件也能过） |
| reference_solution | 输入 + 参考解 | **通过** | 验证器太严 / 参考解写错 / 任务不可解 |
| wrong_solution | 输入 + 错误解 | **失败** | 验证器接受错答案 |

== 参考解的定位（重要）==
参考解**只用于验证判定器**，不用于证明"Agent 能做出来"。
所以多数任务的参考解是**直接写出期望产物**（而不是真的去执行一遍计算）：
那样最稳定，且它验证的正是我们关心的东西 —— "给定正确的产物，判定器会不会放行"。
需要验证"能执行命令"的任务（如修 bug、用 sqlite）才用 exec。

== 这一步不需要 API Key，也不花钱 ==
它只跑文件操作和判定器。所以可以放心地在 CI 里对每个任务都跑。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from lab.bench.task import BenchTask, Fixture, SolutionStep, load_all_tasks
from lab.bench.verifier import CheckResult, run_checks
from lab.infra.local_sandbox import LocalSandbox


class StepValidation(BaseModel):
    """一步自检的结果。"""

    name: str
    expected_pass: bool  # 期望判定器给出"通过"
    actual_pass: bool  # 判定器实际结论
    details: List[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.expected_pass == self.actual_pass


class TaskValidation(BaseModel):
    """一个任务的自检结果。"""

    uid: str
    title: str = ""
    steps: List[StepValidation] = Field(default_factory=list)
    problems: List[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps) and not self.problems


async def _apply_fixtures(sandbox: LocalSandbox, fixtures: List[Fixture]) -> None:
    for fixture in fixtures:
        result = await sandbox.write_file(fixture.path, fixture.content)
        if not result.success:
            raise RuntimeError(f"铺设输入文件失败: {fixture.path}: {result.message}")


async def _apply_steps(sandbox: LocalSandbox, steps: List[SolutionStep], step_name: str) -> None:
    """执行参考解/错误解的步骤。"""
    for index, step in enumerate(steps):
        if step.kind == "write_file":
            result = await sandbox.write_file(step.path, step.content or "")
        elif step.kind == "exec":
            result = await sandbox.exec_command(f"validate-{step_name}-{index}", "/home/ubuntu", step.command or "")
        else:
            raise RuntimeError(f"未知的步骤类型: {step.kind}")
        if not result.success:
            raise RuntimeError(f"[{step_name}] 第 {index + 1} 步执行失败: {result.message}")


async def _evaluate(task: BenchTask, workspace: Path, steps: Optional[List[SolutionStep]], name: str) -> StepValidation:
    """在干净的工作区里铺输入（可选再执行 steps），然后跑判定器。"""
    sandbox = LocalSandbox(workspace, exec_timeout=60)
    await sandbox.ensure_sandbox()
    await _apply_fixtures(sandbox, task.fixtures)
    if steps:
        await _apply_steps(sandbox, steps, name)

    checks: List[CheckResult] = await run_checks(task.verify, sandbox)
    actual_pass = all(check.ok for check in checks) and bool(checks)
    return StepValidation(
        name=name,
        expected_pass=(name == "reference_solution"),
        actual_pass=actual_pass,
        details=[check.line() for check in checks if not check.ok][:5] or [c.line() for c in checks][:3],
    )


async def validate_task(task: BenchTask, root: Path) -> TaskValidation:
    """三步自检一个任务。"""
    result = TaskValidation(uid=task.uid, title=task.title)

    if not task.verify:
        result.problems.append("任务没有任何 verify 规则（判定器会永远通过）")
    if not task.solution:
        result.problems.append("任务没有提供参考解（无法验证判定器会不会误杀正确做法）")
    if not task.wrong:
        # 只作为提醒：没有错误解时我们无法确认判定器会不会放行错答案
        result.problems.append("任务没有提供错误解（无法验证判定器会拒绝错答案）")

    base = root / task.group / task.key

    result.steps.append(await _evaluate(task, base / "empty", None, "fixtures_only"))
    if task.solution:
        result.steps.append(await _evaluate(task, base / "solution", task.solution, "reference_solution"))
    if task.wrong:
        result.steps.append(await _evaluate(task, base / "wrong", task.wrong, "wrong_solution"))

    return result


async def validate_all(tasks: Optional[List[BenchTask]] = None, root: Optional[Path] = None) -> List[TaskValidation]:
    """自检全部任务。"""
    import tempfile

    tasks = tasks if tasks is not None else load_all_tasks()
    with tempfile.TemporaryDirectory(prefix="lab-bench-validate-") as tmp:
        base = Path(root) if root else Path(tmp)
        return [await validate_task(task, base) for task in tasks]


def render_validations(validations: List[TaskValidation]) -> str:
    """把自检结果渲染成文本表。"""
    lines: List[str] = []
    passed = sum(1 for item in validations if item.ok)
    lines.append("=" * 78)
    lines.append(f"任务集自检：{passed}/{len(validations)} 通过")
    lines.append("=" * 78)
    for item in validations:
        mark = "✓" if item.ok else "✗"
        lines.append(f"{mark} {item.uid}  {item.title}")
        for step in item.steps:
            step_mark = "✓" if step.ok else "✗"
            verdict = "通过" if step.actual_pass else "失败"
            lines.append(
                f"    {step_mark} {step.name:20} 判定器={verdict}"
                f"（期望={'通过' if step.expected_pass else '失败'}）"
            )
            if not step.ok:
                for detail in step.details:
                    lines.append(f"        {detail}")
        for problem in item.problems:
            lines.append(f"    ⚠ {problem}")
    return "\n".join(lines)
