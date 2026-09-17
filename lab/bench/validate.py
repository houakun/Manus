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
    # 同义反复验证（tautology）告警：参考解直接把期望值写了出来。
    # 单独放一个字段而不是塞进 problems 的原因：
    # 它是"验证强度弱"而不是"任务不可用"，不应该阻断评测；
    # 但它必须**显式可见**，否则就会重演那次事故（见下面 _tautology_flags 的注释）。
    tautology_flags: List[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps) and not self.problems


def _tautology_flags(task: BenchTask) -> List[str]:
    """检测"参考解直接把期望值写出来"的同义反复验证。

    == 为什么必须有这个检测（真实事故）==
    synthetic 任务集里有两个任务的**期望值本身写错了**：
      - `syn_string_reverse`："Manus Agent" 反转+大写应该是 `TNEGA SUNAM`，
        我写成了 `TNEGA SUNUM`；
      - `syn_word_freq`：词频 Top3 应该是 `the:4 / dog:3 / brown:2`，我写成了 `the:3 / brown:2 / dog:2`。
    两个任务的三步自检**全部通过**，因为参考解就是"把期望值写进文件" ——
    它怎么可能发现期望值是错的？自检验证的只是"判定器能不能读回自己刚写的东西"。

    后果很严重：跑真实评测时这两个任务 0/5、0/5，
    看起来像"Agent 能力不足"，实际是**任务集把对的答案判成了错**（10/60 次假失败）。

    == 检测规则 ==
    对每条带期望值的检查，看参考解里是否有一模一样的 write_file。
    有 → 标记：这个任务的验证是自证自话的，期望值的正确性**没有被独立验证过**。

    正确做法：让参考解**真的算一遍**（用 exec 跑 python 从 fixtures 推导），
    这样期望值写错时参考解就对不上，自检会失败 —— 自检才真正有意义。
    """
    flags: List[str] = []
    written = {
        step.path: (step.content or "")
        for step in task.solution
        if step.kind == "write_file" and step.path
    }
    if not written:
        return flags  # 纯 exec 型参考解：拿不到静态内容，无法判定

    def _norm(text: str, mode: str) -> str:
        return text.strip() if mode != "none" else text

    for check in task.verify:
        content = written.get(check.path)
        if content is None:
            continue
        if check.kind == "file_content" and check.equals is not None:
            if _norm(content, check.normalize) == _norm(check.equals, check.normalize):
                flags.append(
                    f"{check.path}: 参考解直接写出了期望内容 → 验证是同义反复"
                )
        elif check.kind == "numeric" and check.value is not None:
            try:
                if abs(float(content.strip().split()[0]) - float(check.value)) <= check.tolerance:
                    flags.append(f"{check.path}: 参考解直接写出了期望数值 → 验证是同义反复")
            except (ValueError, IndexError):
                pass
        elif check.kind == "json_equals" and check.equals is not None:
            import json as _json

            try:
                if _json.loads(content) == _json.loads(check.equals):
                    flags.append(f"{check.path}: 参考解直接写出了期望 JSON → 验证是同义反复")
            except (TypeError, ValueError):
                pass
        elif check.kind == "csv_rows" and check.rows:
            from lab.bench.verifier import _parse_csv

            expected_rows = [[str(c).strip() for c in row] for row in check.rows]
            if _parse_csv(content) == expected_rows:
                flags.append(f"{check.path}: 参考解直接写出了期望行 → 验证是同义反复")
    return flags


async def _apply_fixtures(sandbox: LocalSandbox, fixtures: List[Fixture]) -> None:
    for fixture in fixtures:
        result = await sandbox.write_file(fixture.path, fixture.content)
        if not result.success:
            raise RuntimeError(f"铺设输入文件失败: {fixture.path}: {result.message}")


async def _apply_steps(sandbox: LocalSandbox, steps: List[SolutionStep], step_name: str) -> None:
    """执行参考解/错误解的步骤。

    ⚠️ 关键细节：`exec` 步必须检查**返回码**，不能只看 `result.success`。
    LocalSandbox（与真沙箱一致）对非零返回码仍然返回 success=True，
    返回码放在 data 里 —— 于是"参考解脚本报错退出"会被静默忽略，
    最终表现为"判定器失败"，把排查方向带到完全错误的地方（责怪判定器而不是参考解）。
    这个坑真实踩过：9 个改成 `python _solve.py` 的参考解全都没跑起来，
    而报错却全部指向判定器。
    """
    for index, step in enumerate(steps):
        if step.kind == "write_file":
            result = await sandbox.write_file(step.path, step.content or "")
        elif step.kind == "exec":
            result = await sandbox.exec_command(f"validate-{step_name}-{index}", "/home/ubuntu", step.command or "")
        else:
            raise RuntimeError(f"未知的步骤类型: {step.kind}")

        if not result.success:
            raise RuntimeError(f"[{step_name}] 第 {index + 1} 步执行失败: {result.message}")

        data = result.data if isinstance(result.data, dict) else {}
        returncode = data.get("returncode")
        if returncode not in (None, 0):
            output = (data.get("output") or "")[-800:]
            raise RuntimeError(
                f"[{step_name}] 第 {index + 1} 步（{step.kind}）返回码 {returncode}，步骤失败。\n"
                f"命令: {step.command}\n输出: {output}"
            )


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

    result.tautology_flags = _tautology_flags(task)

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
    tautological = [item for item in validations if item.tautology_flags]
    lines.append("=" * 78)
    lines.append(f"任务集自检：{passed}/{len(validations)} 通过")
    if tautological:
        lines.append(
            f"⚠️  {len(tautological)} 个任务的参考解直接写出了期望值："
            f"自检无法发现它们期望值写错（历史上真出过两次事故）"
        )
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
        for flag in item.tautology_flags:
            lines.append(f"    ⚠ 同义反复: {flag}")
        for problem in item.problems:
            lines.append(f"    ⚠ {problem}")
    return "\n".join(lines)
