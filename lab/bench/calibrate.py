#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM-judge 的校准（T14 / 审计 §1④）：人工金标 → Cohen's κ → **低于阈值就拒绝启用**。

== 这个模块的立场 ==
LLM judge 是**被测对象**，不是裁判。在它被校准之前，它的输出不参与任何指标。
所以这里的核心 API 不是"算一个 κ"，而是 `Calibration.enabled`：
**没达标就是没达标**，而不是"先用着，回头再校准"。

== 三个必须写清的口径 ==
1. **Unknown 不计入 κ 的分母**：模型说"看不出来"是有效答案；
   把它算成错会拉低一致性，算成对更糟（伪造一致）。
2. **κ 是逐维度算的**：合并成一个总分会让"哪一维不可信"消失 ——
   而恰恰是某些维度不可信（例如指令遵守），不是整体不可信。
3. **样本量与类别分布都要看**：若金标里全是 true（或全是 false），
   κ 的期望一致率会退化成 1，公式直接除零 —— 那种情况下必须报"无法计算"，
   而不是返回 0 或 1（两个都会误导）。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from lab.bench.judge import JUDGE_DIMENSIONS, JUDGE_RUBRIC_VERSION, active_rubric_version

# 启用门槛：κ < 0.6 视为"一致性不足"，**拒绝启用**。
# 为什么是 0.6：这是"substantial agreement"的常用下界；本项目所有阈值都要求有来源，
# 所以它写在常量里、并进报告，而不是散在 if 里。
KAPPA_THRESHOLD = 0.6
# 低于这个对照样本量时不下结论（κ 在小样本上极不稳定）。
MIN_COMPARED_SAMPLES = 20


def cohen_kappa(pairs: List[Tuple[bool, bool]]) -> Optional[float]:
    """2×2 Cohen's κ。`None` = **无法计算**（空样本 / 期望一致率退化为 1）。

    退化情形必须返回 None 而不是 0 或 1：
    金标全是同一类别时（例如 100 条全 true），pe=1 → 公式除零；
    返回 1 会假装"完美一致"，返回 0 会假装"完全不一致" —— 两个都是编数据。
    """
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(1 for human, judge in pairs if human == judge) / n
    human_true = sum(1 for human, _ in pairs if human) / n
    judge_true = sum(1 for _, judge in pairs if judge) / n
    expected = human_true * judge_true + (1 - human_true) * (1 - judge_true)
    if abs(1 - expected) < 1e-12:
        return None
    return (observed - expected) / (1 - expected)


def confusion(pairs: List[Tuple[bool, bool]]) -> Dict[str, int]:
    """金标(行) × judge(列) 的 2×2 计数。"""
    return {
        "human_true_judge_true": sum(1 for h, j in pairs if h and j),
        "human_true_judge_false": sum(1 for h, j in pairs if h and not j),
        "human_false_judge_true": sum(1 for h, j in pairs if not h and j),
        "human_false_judge_false": sum(1 for h, j in pairs if not h and not j),
    }


@dataclass
class DimensionCalibration:
    """一个维度的一致性结果。"""

    dimension: str
    n_gold: int = 0  # 有人工标注的样本数
    n_unknown: int = 0  # judge 回答 Unknown（**不计入 κ**）
    kappa: Optional[float] = None
    confusion: Dict[str, int] = field(default_factory=dict)

    @property
    def compared(self) -> int:
        return sum(self.confusion.values()) if self.confusion else 0

    @property
    def agreement(self) -> Optional[float]:
        if not self.compared:
            return None
        return (self.confusion["human_true_judge_true"] + self.confusion["human_false_judge_false"]) / self.compared

    @property
    def ok(self) -> bool:
        return self.kappa is not None and self.kappa >= KAPPA_THRESHOLD and self.compared >= MIN_COMPARED_SAMPLES


@dataclass
class Calibration:
    """全部维度的校准结果 + 是否允许启用。"""

    dimensions: List[DimensionCalibration] = field(default_factory=list)
    rubric_version: str = JUDGE_RUBRIC_VERSION

    @property
    def enabled(self) -> bool:
        """**只有在每个维度都达标时才允许启用**（木桶原理：一维不可信就不能用它下结论）。"""
        return bool(self.dimensions) and all(item.ok for item in self.dimensions)

    def refusal_reason(self) -> str:
        """为什么不能启用 —— 必须说清，否则人会直接去看数字然后自己决定。"""
        if not self.dimensions:
            return "还没有任何维度被校准（需要人工金标）"
        problems = []
        for item in self.dimensions:
            if item.kappa is None:
                problems.append(f"{item.dimension}: κ 无法计算（样本 {item.compared}）")
            elif item.compared < MIN_COMPARED_SAMPLES:
                problems.append(f"{item.dimension}: 对照样本不足（{item.compared} < {MIN_COMPARED_SAMPLES}）")
            elif item.kappa < KAPPA_THRESHOLD:
                problems.append(f"{item.dimension}: κ={item.kappa:.3f} < {KAPPA_THRESHOLD}")
        return "；".join(problems) if problems else ""


def calibrate(
    gold: Mapping[str, Mapping[str, Optional[bool]]],
    judged: Mapping[str, Mapping[str, Optional[bool]]],
    *,
    dimensions: Iterable[str] = JUDGE_DIMENSIONS,
    rubric_version: Optional[str] = None,
) -> Calibration:
    """用人工金标校准 judge。

    :param gold: `{sample_id: {dimension: True/False/None}}`（None = 该样本未标注）
    :param judged: `{sample_id: {dimension: True/False/None}}`（None = judge 答 Unknown）
    :param rubric_version: **产生这批判定的 rubric 版本**。不传就取当前生效版本 ——
        但调用方（CLI）应当从判定文件的 `__meta__` 里取，否则报告头会写出”“当前默认版本”
        而不是“这批数据到底是哪个版本跑的”，跨版本混算的判断依据就没了（实测踩过：
        用 v3 跑的判定，报告头却写 `rubric v2`）。

    参数类型用 `Mapping` 而不是 `Dict`：这里**只读不写**，而 `Dict` 的值类型是不变的 ——
    调用方手里是 `dict[str, dict[str, bool]]`（测试里的金标就是这样）时会被静态检查判为不兼容，
    于是每个调用点都得加注解。用 `Mapping` 后 `dict[str, dict[str, bool]]` 天然可传。
    """
    items: List[DimensionCalibration] = []
    for dimension in dimensions:
        pairs: List[Tuple[bool, bool]] = []
        n_gold = n_unknown = 0
        for sample_id, per_dimension in gold.items():
            human = per_dimension.get(dimension)
            if human is None:
                continue
            n_gold += 1
            judge = (judged.get(sample_id) or {}).get(dimension)
            if judge is None:
                n_unknown += 1  # Unknown **不进配对**
                continue
            pairs.append((bool(human), bool(judge)))
        items.append(
            DimensionCalibration(
                dimension=dimension,
                n_gold=n_gold,
                n_unknown=n_unknown,
                kappa=cohen_kappa(pairs),
                confusion=confusion(pairs),
            )
        )
    return Calibration(dimensions=items, rubric_version=rubric_version or active_rubric_version())


# ==================== 标注清单（给人工填） ====================

GOLD_COLUMNS = (
    "sample_id",
    "task_key",
    "group",
    "suite_id",
    "run_index",
    "arm",
    "self_reported_ok",
    "artifact_dir",
    # 新鲜度标记（读产物前算好，见 `lab/bench/workspace.py`）：
    # "1" = 工作区已被后来的运行覆盖（产物**不是**这次运行的）
    # "0" = 新鲜　"" = 无法判断（旧数据没记运行时间）
    # 它不进 κ，只影响“这条样本该不该信” —— 所以与 human_* 列分开。
    "workspace_stale",
    # 样本来源元数据（P3a 分级抽样）：positive / db_negative / mild / wrong_solution / empty。
    # ⚠️ 它**不是标签** —— 金标只有 human_* 三列。一个 “mild” 样本完全可能仍然合格
    # （那就该标 true）。把 level 当标签用，等于用抽样设计伪造结论。
    "level",
    *[f"human_{dimension}" for dimension in JUDGE_DIMENSIONS],
    "notes",
)


def write_label_sheet(rows: Sequence[Mapping[str, object]], path: Path) -> int:
    """写一份**待人工标注**的 CSV（judge 的结果刻意不写进去，避免锚定）。

    为什么不把 judge 的判定放进同一张表：那会让人在"看到机器答案"之后标注，
    于是 κ 测的就不再是"两个独立标注者的一致性"，而是"人愿不愿意同意机器"。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(GOLD_COLUMNS))
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row.get(name, "") for name in GOLD_COLUMNS})
    except OSError as exc:
        # 写不进去时要说清是**哪个路径**失败，而不是抛一个裸 OSError
        raise RuntimeError(f"写标注表失败 {path}: {exc}") from exc
    return len(rows)


def read_gold_csv(path: Path) -> Dict[str, Dict[str, Optional[bool]]]:
    """读回人工填好的金标。空单元格 = 未标注（None），不当作 False。"""
    gold: Dict[str, Dict[str, Optional[bool]]] = {}
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sample_id = (row.get("sample_id") or "").strip()
                if not sample_id:
                    continue
                per_dimension: Dict[str, Optional[bool]] = {}
                for dimension in JUDGE_DIMENSIONS:
                    raw = (row.get(f"human_{dimension}") or "").strip().lower()
                    if raw in ("1", "true", "yes", "y", "对", "是"):
                        per_dimension[dimension] = True
                    elif raw in ("0", "false", "no", "n", "错", "否"):
                        per_dimension[dimension] = False
                    else:
                        per_dimension[dimension] = None
                gold[sample_id] = per_dimension
    except OSError as exc:
        raise RuntimeError(f"读金标失败 {path}: {exc}") from exc
    return gold


def read_gold_rows(path: Path) -> List[Dict[str, str]]:
    """读回标注表的**原始行**（给“跑 judge”用：它还需要 `task_key` 与 `artifact_dir`）。

    与 `read_gold_csv` 的分工：那个只关心**标签**（用于算 κ）；
    这个关心**材料从哪里来** —— 没有它就无法把样本还原成“目标 + 产物”。
    """
    rows: List[Dict[str, str]] = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if (row.get("sample_id") or "").strip():
                    rows.append(dict(row))
    except OSError as exc:
        raise RuntimeError(f"读标注表失败 {path}: {exc}") from exc
    return rows


def render_calibration(cal: Calibration) -> str:
    """文本报告（可进 CI / 可 grep）。"""
    lines = [f"# judge 校准（rubric {cal.rubric_version}）", ""]
    lines.append(f"- 启用门槛：κ ≥ {KAPPA_THRESHOLD}，且对照样本 ≥ {MIN_COMPARED_SAMPLES}")
    lines.append(f"- **是否允许启用：{'✅ 是' if cal.enabled else '❌ 否'}**")
    if not cal.enabled:
        lines.append(f"- 原因：{cal.refusal_reason()}")
    lines += [
        "",
        "| 维度 | 金标条数 | 其中 Unknown | 参与 κ | κ | 一致率 | 混淆矩阵 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in cal.dimensions:
        kappa = "无法计算" if item.kappa is None else f"{item.kappa:.3f}"
        agreement = "—" if item.agreement is None else f"{item.agreement:.1%}"
        lines.append(
            f"| {item.dimension} | {item.n_gold} | {item.n_unknown} | {item.compared} "
            f"| {kappa} | {agreement} | {json.dumps(item.confusion, ensure_ascii=False)} |"
        )
    lines += [
        "",
        ('> ⚠️ 这份报告只说明**κ 是多少**；在没有人工金标时，**不得**写成"已做 κ 校准"。'),
    ]
    return "\n".join(lines)
