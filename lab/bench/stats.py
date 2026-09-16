#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统计：均值置信区间、成功率的 Wilson 区间、配对比较。

== 为什么必须给区间而不是一个点估计 ==
Step 1 的实测已经证明：**同一个任务跑两次，token 差 34%**。
在这个噪声水平下报"平均 63k"是把噪声当信号 —— 你必须同时给出"这个数字有多不确定"。
handoff 第 8 节的模板写的 `78.3% ± 2.1%（n=5, 95% CI）` 就是这个意思。

== 两种区间，用在不同类型的指标上 ==
| 指标类型 | 例子 | 用什么 | 为什么 |
|---|---|---|---|
| 连续量 | token / 成本 / 耗时 | t 分布区间 | 样本小、方差未知；且极端值会拉偏正态近似 |
| 比例 | 成功率 | **Wilson 区间** | n 小或 p 接近 0/1 时，正态近似会给出越界（<0 或 >1）的荒谬区间 |

== 刻意不引入 scipy ==
只用到 95% 的 t 临界值，硬编码一张 1~30 自由度的小表就够了（精确到小数点后 3 位）。
多一个科学计算依赖，就多一份"环境装不上"的风险 —— 而这是个评测工具，不是统计研究工具。
零依赖的代价是只支持 95%（其它置信度会退回正态近似），这个限制写在这里而不是藏着。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel

# 95% 置信度、双侧、自由度 1..30 的 t 临界值
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_Z95 = 1.96


def t_critical(df: int, confidence: float = 0.95) -> float:
    """取 t 临界值。自由度超过 30 或非 95% 时退回正态近似。"""
    if confidence != 0.95:
        return _Z95  # 明确降级，不假装精确
    if df <= 0:
        return float("inf")  # n=1 时无法估方差，区间无穷宽（如实表达"完全不确定"）
    return _T95.get(df, _Z95)


class Interval(BaseModel):
    """一个带不确定性的估计值。"""

    point: float = 0.0
    low: Optional[float] = None
    high: Optional[float] = None
    half_width: Optional[float] = None
    n: int = 0
    method: str = ""  # t95 / wilson95 / none

    def render(self, unit: str = "", digits: int = 2) -> str:
        if self.low is None or self.high is None:
            return f"{self.point:.{digits}f}{unit} (n={self.n}, 无区间)"
        return f"{self.point:.{digits}f}{unit} ± {self.half_width:.{digits}f} (n={self.n}, {self.method})"


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: Sequence[float]) -> float:
    """样本标准差（n-1 分母）。"""
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / (len(values) - 1))


def mean_ci(values: Sequence[float], confidence: float = 0.95) -> Interval:
    """连续量的均值 ± 置信区间（t 分布）。

    n=1 时返回 half_width=None（无法估方差）——**不要**伪造成 ±0，
    那等于宣称"我这一次跑出来的就是真值"。
    """
    values = [float(v) for v in values]
    n = len(values)
    if n == 0:
        return Interval(point=0.0, n=0, method="none")
    point = mean(values)
    if n == 1:
        return Interval(point=point, n=1, method="none")

    half = t_critical(n - 1, confidence) * stdev(values) / math.sqrt(n)
    return Interval(
        point=point, low=point - half, high=point + half, half_width=half,
        n=n, method="t95" if confidence == 0.95 else "normal",
    )


def wilson_interval(successes: int, n: int, z: float = _Z95) -> Interval:
    """比例的 Wilson 置信区间。

    为什么不用 p ± z*sqrt(p(1-p)/n)：
    - n=5、5 次全成功时，正态近似给出 ±0，等于宣称"成功率就是 100%，不可能失败"，
      而 Wilson 给出 [0.566, 1.0] —— 这才诚实地表达了"5 次样本说明不了太多"。
    - p=0 或 1 时正态近似还会给出越界区间。
    """
    if n <= 0:
        return Interval(point=0.0, n=0, method="none")
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Interval(
        point=p,
        low=max(0.0, center - margin),
        high=min(1.0, center + margin),
        half_width=margin,
        n=n,
        method="wilson95",
    )


class PairedComparison(BaseModel):
    """配对比较（A/B）。

    为什么优先用配对而不是比较两组独立的均值：
    配对会消掉**任务之间的难度差异** —— 同一个任务在 A 和 B 上各跑一次，
    差值只反映"方案差异"，而任务本身简单或困难的影响被减掉了。
    在本项目里这一点尤其重要：任务难度跨度很大（求和 vs 修 bug），
    不做配对的话，任务集顺序或并行调度的小变化都会污染结论。
    """

    name: str = ""
    n_pairs: int = 0
    mean_diff: float = 0.0
    diff_ci: Interval = None  # type: ignore[assignment]
    wins: int = 0  # A 优于 B 的次数
    losses: int = 0
    ties: int = 0

    def model_post_init(self, __context) -> None:  # pragma: no cover - pydantic 钩子
        if self.diff_ci is None:
            self.diff_ci = Interval()

    def render(self, unit: str = "", digits: int = 3, lower_is_better: bool = True) -> str:
        better = "A 更优" if (self.mean_diff < 0) == lower_is_better else "B 更优"
        return (
            f"{self.name}: Δ={self.mean_diff:+.{digits}f}{unit} "
            f"[{self.diff_ci.low:.{digits}f}, {self.diff_ci.high:.{digits}f}] "
            f"n={self.n_pairs} 胜/负/平={self.wins}/{self.losses}/{self.ties} → {better}"
        )


def compare_paired(
        name: str,
        baseline: Dict[str, float],
        candidate: Dict[str, float],
        *,
        lower_is_better: bool = True,
) -> PairedComparison:
    """按 key 配对比较两组数值（差值定义为 candidate - baseline）。"""
    keys = sorted(set(baseline) & set(candidate))
    diffs = [candidate[k] - baseline[k] for k in keys]
    wins = sum(1 for d in diffs if (d < 0) == lower_is_better and d != 0)
    losses = sum(1 for d in diffs if (d < 0) != lower_is_better and d != 0)

    return PairedComparison(
        name=name,
        n_pairs=len(keys),
        mean_diff=mean(diffs),
        diff_ci=mean_ci(diffs),
        wins=wins,
        losses=losses,
        ties=len(diffs) - wins - losses,
    )


def aggregate(values: Sequence[float]) -> Dict[str, float]:
    """一组数值的常用汇总（供报告用）。"""
    values = [float(v) for v in values]
    if not values:
        return {"n": 0, "mean": 0.0, "min": 0.0, "max": 0.0, "sd": 0.0, "p95": 0.0}
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "n": float(len(values)),
        "mean": mean(values),
        "min": ordered[0],
        "max": ordered[-1],
        "sd": stdev(values),
        "p95": ordered[index],
    }
