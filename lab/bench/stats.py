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


def bootstrap_ci(
        values: Sequence[float],
        *,
        statistic: str = "mean",
        resamples: int = 2000,
        confidence: float = 0.95,
        seed: int = 42,
) -> Interval:
    """自助法（bootstrap）置信区间。

    == 为什么除了 t 区间还需要它 ==
    分组对比实测：semireal 的 tokens 是 210k ± 58k、**max/均值 = 4.5×** ——
    这是明显的重尾分布。t 区间假设（近似正态、方差有限）在重尾下会低估不确定性，
    尤其是当最贵的那次运行刚好在/不在样本里时，均值会大幅飘移。

    自助法不假设分布形状：它直接重采样"如果重新跑 n 次，均值会怎么变"。
    代价是它无法超出观测到的极值范围（对尾部仍偏乐观），
    所以报告里会**同时给 t 区间和 bootstrap 区间**，两者差得多就说明分布很偏。

    固定 seed → 可复现（评测工具的基本要求）。
    """
    import random

    values = [float(v) for v in values]
    n = len(values)
    if n == 0:
        return Interval(point=0.0, n=0, method="none")
    if n == 1:
        return Interval(point=values[0], n=1, method="none")

    def _stat(sample: List[float]) -> float:
        if statistic == "median":
            ordered = sorted(sample)
            mid = len(ordered) // 2
            return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        return sum(sample) / len(sample)

    rng = random.Random(seed)
    estimates = []
    for _ in range(resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        estimates.append(_stat(sample))
    estimates.sort()

    alpha = (1 - confidence) / 2
    low = estimates[max(0, int(alpha * resamples) - 1)]
    high = estimates[min(resamples - 1, int((1 - alpha) * resamples))]
    point = _stat(values)
    return Interval(
        point=point, low=low, high=high, half_width=(high - low) / 2,
        n=n, method=f"bootstrap{int(confidence * 100)}:{statistic}",
    )


def paired_bootstrap_ci(
        differences: Sequence[float],
        *,
        resamples: int = 2000,
        confidence: float = 0.95,
        seed: int = 42,
) -> Interval:
    """配对差值的 bootstrap 区间。

    为什么要配对：同一个任务在两个方案上各跑一次，差值只反映**方案差异**，
    任务本身简单或困难的影响被减掉了。本项目的任务难度跳度极大
    （syn_sum_range 与 sem_sqlite_report 差 10 倍以上），不配对的话
    任务集排序或调度的小变化就会污染结论。

    注意：配对**抵消不了时间相关的混淆**（两组跑在不同时段时，服务端漂移会同时
    影响两边）—— 那需要交错设计（interleaved），见 Step 5 报告的诚实声明。
    """
    return bootstrap_ci(differences, resamples=resamples, confidence=confidence, seed=seed)


def quantile(values: Sequence[float], p: float) -> float:
    """经验分位数（线性插值）。

    == 为什么用分位数而不是均值来定预算阈值 ==
    分组对比给出的直接证据：semireal 的 tokens 区间是 210k ± 58k（相对宽 28%）、
    max/均值 = **4.5×**；synthetic 是 124k ± 14k（宽 11%）、max/均值 = 2.2×。
    在这么重的长尾下，**按"均值 + 一点余量"设的阈值会被长尾频繁击穿** ——
    实测 semireal 的软阈值越界率高达 60%，切 enforce 就等于随机砍任务。

    改用分位数后，越界率就是**定义上**的概率：
    软线取 P95 → 约 5% 的正常运行会越界；硬线取 P99 → 约 1%。

    == n 很小时不要平滑 ==
    n=5 时 P99 几乎就是最大值。这是诚实的行为：样本少就不该假装知道尾部形状。
    调用方应该用 min_samples 拦住样本太少的情况（见 lab/bench/policy.py）。
    """
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = p * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


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
