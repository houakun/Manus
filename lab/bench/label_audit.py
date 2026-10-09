#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""标注审计：把"**失败的检查属于哪一类**"推成 `(IF, OQ)`，再与人给的标签 / judge 判定逐条对。

== 为什么需要它 ==
`instruction_following` 与 `output_quality` 的分歧（三轮 κ 0.21~0.56）不是"judge 随机犯错"，
而是**两类失败该记到哪一维**没有明文。这个工具把那套边界**编码成可执行的规则**，于是：
- 规则 vs 人 不一致 → 说明"规则没说清"或"标签内在不一致"（= 灰区）；
- judge vs 规则 偏离 → 把聚合的 κ **还原成具体条目**（哪几条、朝哪个方向错）。

== 两条规则缺口是实测揪出来的（不是想出来的）==
1. **"文件不存在"是形状问题**，即使检查类型是 `file_content` —— 只看 `kind` 会把
   "产物根本没产出"误判成内容错（实测 27 条不一致）；
2. **任何失败 → `OQ=false`** —— 形状坏了就是"没有可接受的产物"，人就是这么判的
   （实测 18 条不一致：`empty` 与"该删的没删"）。

补完之后：**规则 vs 人 = 111/111**。⚠️ 但那 111/111 的含义是"规则准确描述了这份标签"，
**不是**"标签被独立验证了"（规则是照着不一致逐条调出来的）。

用法：
    python -m lab bench audit-labels --gold lab/runs/judge_gold_v3_human.csv
    python -m lab bench audit-labels --gold <金标> --judged <judge 判定 JSON>
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# 形状类：产物"该有/该没"的东西在形状上不对
SHAPE_KINDS = frozenset({"file_exists", "no_extra_files", "dir_count"})
# 内容类：产物在，但里面的东西不对
CONTENT_KINDS = frozenset({"file_content", "json_equals", "csv_rows", "numeric", "line_count", "matches"})
# 行为类：跑起来还不对
BEHAVIOR_KINDS = frozenset({"exec_script"})

# 检查失败信息里的这个短语 = 文件缺失（→ 形状问题，即使类型是 file_content）
_MISSING_MARK = "文件不存在或不可读"


def kind_of(check: Any) -> str:
    """把一条**失败**的检查归成 `形状` / `内容` / `行为`。

    ⚠️ **不能只看 `kind`**：`file_content` 失败有两种完全不同的原因 ——
    ① 文件根本不存在 → 形状问题；② 文件在、内容不对 → 内容问题。
    """
    kind = getattr(check, "kind", "") or ""
    try:
        line = check.line()
    except Exception:  # noqa: BLE001 —— 渲染失败不该让审计挂掉
        line = ""
    if _MISSING_MARK in line:
        return "形状"
    if kind in SHAPE_KINDS:
        return "形状"
    if kind in CONTENT_KINDS:
        return "内容"
    if kind in BEHAVIOR_KINDS:
        return "行为"
    return f"其它({kind})"


def rule_labels(kinds: Mapping[str, int]) -> Tuple[bool, bool, bool]:
    """现行规则 → `(task_completion, instruction_following, output_quality)`。

    - **TC**：任何失败 → false（“**交付物完整性**”读法：要求的产物没按要求交出来，就是没完成）。
    - **IF 只看形式**：形状坏了 → false；内容错**不影响** IF。
    - **OQ 看“有没有一个可接受的产物”**：**任何失败都算 false**。
      ⚠️ 反过来不成立：OQ 还能看到**判定器看不见的内容错**（那正是 judge 存在的理由）——
      所以“判据全过但人/judge 说 OQ=false”是**合法**的，不是规则违反。

    ⚠️ **TC 也要比**：实测踩过 —— 审计只比 IF/OQ 时，我把 16 条 `ci_*`（诊断没点出正确模块、
    但代码修好、CI 通过）标成 TC=true，**违背了本函数写的规则，却没有工具报出来**。
    """
    if not kinds:
        return True, True, True
    return False, (not any(k == "形状" for k in kinds)), False


def _tri(row: Mapping[str, str], prefix: str) -> Optional[Tuple[bool, bool, bool]]:
    """从一行里取 `(TC, IF, OQ)`；任一为空 → `None`（未标注/Unknown）。"""
    values = []
    for column in (
        f"{prefix}task_completion",
        f"{prefix}instruction_following",
        f"{prefix}output_quality",
    ):
        raw = (row.get(column) or "").strip().lower()
        if raw in ("true", "1", "yes", "y"):
            values.append(True)
        elif raw in ("false", "0", "no", "n"):
            values.append(False)
        else:
            return None
    return values[0], values[1], values[2]


def _judge_tri(verdict: Mapping[str, Optional[bool]]) -> Optional[Tuple[bool, bool, bool]]:
    """judge 判定里的 `(TC, IF, OQ)`；**任一项是 None（Unknown）→ 整体返回 None**。

    与 `_tri` 分开：金标里的值是从 CSV 读出来的**字符串**，judge 判定里是真正的 `bool`/`None`。
    把两者合成一个函数就得写一堆宽容判断，而那种"宽容"恰恰是这类工具最不该有的东西。
    """
    values: List[bool] = []
    for dimension in ("task_completion", "instruction_following", "output_quality"):
        value = verdict.get(dimension)
        if value is None:
            return None
        values.append(bool(value))
    return values[0], values[1], values[2]


@dataclass
class AuditRow:
    """一条样本的审计结果。"""

    sample_id: str
    task_key: str
    failed: str  # 失败分类的可读描述（如 `形状×2、内容×1`）
    rule: Tuple[bool, bool, bool]
    human: Optional[Tuple[bool, bool, bool]] = None
    judge: Optional[Tuple[bool, bool, bool]] = None
    notes: List[str] = field(default_factory=list)

    @property
    def rule_vs_human_ok(self) -> bool:
        return self.human is None or self.human == self.rule

    @property
    def judge_deviates(self) -> bool:
        return self.judge is not None and self.judge != self.rule


async def audit(
    gold_rows: Sequence[Mapping[str, str]],
    tasks: Mapping[str, Any],
    judged: Optional[Mapping[str, Mapping[str, Optional[bool]]]] = None,
) -> List[AuditRow]:
    """对每条样本跑判定器、归类失败、推规则标签，并与人的标签 / judge 判定并列。"""
    from lab.bench.verifier import run_checks
    from lab.infra.local_sandbox import LocalSandbox

    rows: List[AuditRow] = []
    for row in gold_rows:
        task = tasks.get(row.get("task_key", ""))
        if task is None:
            continue
        workspace = row.get("artifact_dir") or ""
        checks = await run_checks(task.verify, LocalSandbox(Path(workspace)))
        kinds = Counter(kind_of(c) for c in checks if not c.ok)
        detail = "、".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "无失败检查"
        item = AuditRow(
            sample_id=row.get("sample_id", ""),
            task_key=row.get("task_key", ""),
            failed=detail,
            rule=rule_labels(kinds),
            human=_tri(row, "human_"),
        )
        if judged is not None:
            item.judge = _judge_tri(judged.get(item.sample_id) or {})
        if not item.rule_vs_human_ok:
            item.notes.append("规则与人不一致")
        if item.judge_deviates:
            item.notes.append("judge 偏离规则")
        rows.append(item)
    return rows


def render_audit(rows: Sequence[AuditRow], *, limit: int = 20) -> List[str]:
    """文本报告（可 grep、可进 CI）：先给统计，再给两类问题条目。"""
    lines = ["# 标注审计（规则 vs 人 vs judge）", ""]
    total = len(rows)
    human_checked = [r for r in rows if r.human is not None]
    mismatch = [r for r in human_checked if not r.rule_vs_human_ok]
    lines.append(f"- 样本 {total} 条；有人工标签的 {len(human_checked)} 条")
    lines.append(f"- **规则 vs 人：一致 {len(human_checked) - len(mismatch)}/{len(human_checked)}**")
    deviations = [r for r in rows if r.judge_deviates]
    unknown = [r for r in rows if r.judge is None]
    if any(r.judge is not None for r in rows):
        lines.append(f"- judge vs 规则：偏离 {len(deviations)} 条；judge 答 Unknown {len(unknown)} 条")

    if mismatch:
        lines += ["", f"## 规则与人**不一致**（{len(mismatch)} 条）—— 说明规则没说清或标签不一致", ""]
        for item in mismatch[:limit]:
            lines.append(f"- `{item.sample_id}` {item.task_key} 失败={item.failed} 规则={item.rule} 人={item.human}")
    if deviations:
        lines += ["", f"## judge **偏离规则**（{len(deviations)} 条）—— κ 的下降具体在这几条上", ""]
        buckets: Dict[str, List[AuditRow]] = {}
        for item in deviations:
            judge = item.judge
            if judge is None:  # 理论上进不来（`deviations` 由 `judge_deviates` 选出）
                continue
            key = "；".join(
                f"{name} 规则={rule_value} judge={judge_value}"
                for name, rule_value, judge_value in (
                    ("TC", item.rule[0], judge[0]),
                    ("IF", item.rule[1], judge[1]),
                    ("OQ", item.rule[2], judge[2]),
                )
                if rule_value != judge_value
            )
            buckets.setdefault(key, []).append(item)
        for key, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- **{key}**：{len(items)} 条")
            for item in items[:limit]:
                lines.append(f"    - `{item.sample_id}` {item.task_key} 失败={item.failed}")
    return lines
