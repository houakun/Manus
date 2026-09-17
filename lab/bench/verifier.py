#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""判定器：只根据**最终产物**判断任务是否完成。

== 三条铁律 ==
1. **不看 Agent 的自述**。SUT 会返回 `answer` 和 `attachments`，那些只用于报告，
   判定只看工作区里的文件。理由：Step 3 已经证明"工具说成功"和"真的成功"是两件事，
   Agent 的自述更不可信。
2. **归一化要显式**。任务往往有合理的格式差异（结尾换行、大小写、行序）。
   如果不用 `normalize` 显式声明容忍范围，你就会得到大量"其实做对了但被判失败"
   的假阴性，而这会直接毁掉基线数字。
3. **失败必须给出证据**。每条检查都返回可读的 detail（例如"期望 500500，实际 500499"），
   否则报告读者只能看到一串 ✗，无法判断是任务太难还是验证器写错了。
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from pydantic import BaseModel

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from lab.bench.task import BenchTask, VerifyCheck  # noqa: E402


class CheckResult(BaseModel):
    """一条判定的结果。"""

    kind: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"{'✓' if self.ok else '✗'} [{self.kind}] {self.detail}"


# ==================== 归一化 ====================

def normalize_text(text: str, mode: str) -> str:
    """按声明的方式归一化文本。"""
    if mode == "none":
        return text
    if mode == "lower":
        return text.strip().lower()
    if mode == "collapse_ws":
        # 把所有连续空白折叠成单个空格：用于"内容对但排版不同"的场景
        return re.sub(r"\s+", " ", text).strip()
    if mode == "sorted_lines":
        # 行序无关：用于"输出多行结果但顺序不要求"的场景
        return "\n".join(sorted(line.strip() for line in text.splitlines() if line.strip()))
    return text.strip()  # 默认 strip


def _as_dict(payload: Any) -> Optional[dict]:
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return None


async def _read_text(sandbox: Any, path: str) -> Optional[str]:
    """通过沙箱读文件内容（走同一套路径映射，保证与 Agent 的视野一致）。"""
    result = await sandbox.read_file(path, max_length=1_000_000)
    if not result.success:
        return None
    data = _as_dict(result.data) or {}
    return data.get("content")


def _parse_csv(text: str) -> List[List[str]]:
    """极简 CSV 解析（够用即可，刻意不引入 pandas 依赖）。

    支持逗号分隔 + 双引号包裹字段，去掉表头以外的空行。
    """
    import csv
    import io

    reader = csv.reader(io.StringIO(text))
    return [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]


# ==================== 单条检查 ====================

async def run_check(check: VerifyCheck, sandbox: Any) -> CheckResult:
    """执行一条判定。"""
    label = check.description or check.kind

    # --- 存在性 ---
    if check.kind == "file_exists":
        expected = check.exists if check.exists is not None else True
        result = await sandbox.check_file_exists(check.path)
        actual = bool((_as_dict(result.data) or {}).get("exists"))
        return CheckResult(
            kind=check.kind, ok=(actual == expected),
            detail=f"{label}: {check.path} 期望存在={expected}，实际={actual}",
        )

    # --- 目录文件数 / 无多余文件 ---
    if check.kind == "dir_count":
        result = await sandbox.find_files(check.path, "*")
        files = (_as_dict(result.data) or {}).get("files") or []
        expected = check.count if check.count is not None else 0
        return CheckResult(
            kind=check.kind, ok=(len(files) == expected),
            detail=f"{label}: {check.path} 期望 {expected} 个文件，实际 {len(files)} 个 {files[:5]}",
        )

    if check.kind == "no_extra_files":
        result = await sandbox.find_files(check.path, "*")
        files = (_as_dict(result.data) or {}).get("files") or []
        allow = set(check.allow or [])
        extra = [f for f in files if f.split("/")[-1] not in allow]
        return CheckResult(
            kind=check.kind, ok=(not extra),
            detail=f"{label}: 期望只有 {sorted(allow)}，多出 {extra}",
        )

    text = await _read_text(sandbox, check.path)
    if text is None:
        return CheckResult(kind=check.kind, ok=False, detail=f"{label}: 读取失败，文件不存在或不可读: {check.path}")

    # --- 内容相等 ---
    if check.kind == "file_content":
        normalized = normalize_text(text, check.normalize)
        if check.equals is not None:
            expected = normalize_text(check.equals, check.normalize)
            return CheckResult(
                kind=check.kind, ok=(normalized == expected),
                detail=f"{label}: 期望 {expected!r}，实际 {normalized[:120]!r}",
            )
        if check.contains is not None:
            return CheckResult(
                kind=check.kind, ok=(check.contains in text),
                detail=f"{label}: 期望包含 {check.contains!r}",
            )
        if check.not_contains is not None:
            return CheckResult(
                kind=check.kind, ok=(check.not_contains not in text),
                detail=f"{label}: 期望**不**包含 {check.not_contains!r}",
            )
        if check.matches is not None:
            found = re.search(check.matches, text, re.MULTILINE)
            return CheckResult(
                kind=check.kind, ok=bool(found),
                detail=f"{label}: 期望匹配 /{check.matches}/，实际 {text[:120]!r}",
            )
        return CheckResult(kind=check.kind, ok=False, detail=f"{label}: 检查定义不完整")

    # --- 数值相等（带容差） ---
    if check.kind == "numeric":
        try:
            actual = float(normalize_text(text, check.normalize).split()[0])
        except (ValueError, IndexError):
            return CheckResult(kind=check.kind, ok=False, detail=f"{label}: 无法解析出数字，实际 {text[:80]!r}")
        expected = float(check.value)
        ok = abs(actual - expected) <= check.tolerance
        return CheckResult(
            kind=check.kind, ok=ok,
            detail=f"{label}: 期望 {expected}（容差 {check.tolerance}），实际 {actual}",
        )

    # --- JSON 相等 ---
    if check.kind == "json_equals":
        try:
            actual_json = json.loads(text)
        except json.JSONDecodeError as e:
            return CheckResult(kind=check.kind, ok=False, detail=f"{label}: 不是合法 JSON: {e}")
        try:
            expected_json = json.loads(check.equals)
        except json.JSONDecodeError as e:
            return CheckResult(kind=check.kind, ok=False, detail=f"{label}: 检查定义里的期望值不是合法 JSON: {e}")
        ok = actual_json == expected_json
        return CheckResult(
            kind=check.kind, ok=ok,
            detail=f"{label}: 期望 {expected_json}，实际 {actual_json}",
        )

    # --- CSV 行比较（把"表头/行序/空白"这类噪声交给判定器处理） ---
    if check.kind == "csv_rows":
        actual_rows = _parse_csv(text)
        expected_rows = [[str(c).strip() for c in row] for row in (check.rows or [])]
        ok = actual_rows == expected_rows
        return CheckResult(
            kind=check.kind, ok=ok,
            detail=f"{label}: 期望 {len(expected_rows)} 行，实际 {len(actual_rows)} 行"
                   + ("" if ok else f"；首个差异: 期望 {expected_rows[:2]} 实际 {actual_rows[:2]}"),
        )

    # --- 行数 ---
    if check.kind == "line_count":
        lines = [line for line in text.splitlines() if line.strip()]
        ok = len(lines) == (check.count or 0)
        return CheckResult(
            kind=check.kind, ok=ok,
            detail=f"{label}: 期望 {check.count} 行，实际 {len(lines)} 行",
        )

    return CheckResult(kind=check.kind, ok=False, detail=f"{label}: 未知的检查类型 {check.kind!r}")


async def run_checks(checks: List[VerifyCheck], sandbox: Any) -> List[CheckResult]:
    """执行全部判定。**任何一条不过就算任务失败**（不做部分给分）。

    为什么不做部分分：部分分需要为每条检查分配权重，而权重是主观的，
    会让"成功率"这个指标失去可比性。过程分由 process_flags 单独表达。
    """
    results: List[CheckResult] = []
    for check in checks:
        try:
            results.append(await run_check(check, sandbox))
        except Exception as exc:  # noqa: BLE001
            # 判定器自己的异常必须被记成"失败"而不是让 harness 崩掉 ——
            # 但 detail 里要说清是判定器出错，避免被误读成任务失败
            results.append(CheckResult(
                kind=check.kind, ok=False,
                detail=f"{check.description or check.kind}: 判定器异常 {type(exc).__name__}: {exc}",
            ))
    return results


# ==================== 过程规则 ====================

def evaluate_process(task: BenchTask, result: Any) -> List[str]:
    """评估过程规则，返回**警告 flag 列表**。

    设计要点：过程问题**不影响 `ok`**（那是结果分），只作为独立维度报告。
    理由：把两者混在一起，"成功率"这个指标就没法解释 ——
    提高成功率可能是因为模型变强，也可能只是因为放宽了过程要求。
    """
    flags: List[str] = []
    rule = task.process
    guard = result.guard or {}
    loop = guard.get("loop") or {}

    tool_calls = loop.get("tool_calls")
    if rule.max_tool_calls is not None and tool_calls is not None and tool_calls > rule.max_tool_calls:
        flags.append(f"too_many_tool_calls:{int(tool_calls)}>{rule.max_tool_calls}")

    if rule.max_llm_calls is not None and result.llm_usage.llm_calls > rule.max_llm_calls:
        flags.append(f"too_many_llm_calls:{result.llm_usage.llm_calls}>{rule.max_llm_calls}")

    if rule.max_steps is not None and result.steps.done > rule.max_steps:
        flags.append(f"too_many_steps:{result.steps.done}>{rule.max_steps}")

    # 越权访问：这属于**安全**类问题，必须单独可见
    if rule.forbid_path_escape and (guard.get("error_types") or {}).get("path_escape"):
        flags.append("path_escape_attempted")

    # 硬编码答案：Agent 直接把答案写进文件，而不是算出来
    if rule.answer_literals and guard.get("hardcode_suspects"):
        flags.append(f"hardcoded_answer:{guard['hardcode_suspects']}")

    # 同一步里重复动作（Step 3 发现的"近似重复"信号）
    diversity = loop.get("action_diversity")
    if diversity is not None and diversity < 0.4 and (tool_calls or 0) >= 5:
        flags.append(f"low_action_diversity:{diversity}")

    # 工作区外写入：fast mode 特有的失真（沙箱路径映射管不到脚本内容）。
    # 为什么归为**过程问题**而不是直接判失败：产物可能同时存在正确的一份（工具写的），
    # 所以不能断定任务失败；但它一定意味着"有东西跑到工作区外了"，需要人工看一眼。
    escaped = guard.get("escaped_writes") or {}
    if escaped:
        flags.append(f"writes_outside_workspace:{sum(len(v) for v in escaped.values())}")

    return flags
