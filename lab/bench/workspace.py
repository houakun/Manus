#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工作区新鲜度：判断目录里的产物**是不是那一次运行留下的**。

== 为什么必须有这道守门 ==
工作区路径曾经**不含 suite_id**（见 `lab/bench/runner.py` 的说明），于是所有用同一
arm + 同一 run 编号的 suite 写同一个目录，后跑的**静默覆盖**先跑的。实测：100 条校准
样本里 **39 条**的产物属于另一次运行 —— 而判定是当场做的（正确），所以
"判定通过 + 产物是别人的"可以同时成立，且不报任何错。

这类"静默污染"是本项目最想防的东西，防它的第一步是**让工具自己喊出来**：
读产物之前比对"最新文件时间"与"那次运行的时间"，晚了就告警。
（README 自纠 #1/#8/#11 都是"观测手段本身没被验证"栽的跟头。）

== 基准为什么用 `tasks.started_at` ==
那个字段名有误导性：它实际是**该次运行结果落库的时刻**（`store.save_task` 里写的，
字典里全是 `cost_usd` / `elapsed_ms` 这类 post-run 字段），在 SUT 跑完之后。
于是：运行期间写出的文件 mtime < 基准（fresh）；被后来的运行覆盖过的文件 > 基准（stale）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"


@dataclass
class Freshness:
    """一次新鲜度判断的结果。`state` 取 `fresh` / `stale` / `unknown`。"""

    state: str
    newest_mtime: Optional[datetime] = None
    run_time: Optional[datetime] = None
    detail: str = ""

    @property
    def stale(self) -> bool:
        return self.state == STALE


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def check_freshness(workspace: str, run_time: Optional[str]) -> Freshness:
    """比对工作区里最新文件的 mtime 与那次运行的时间。

    解析不了时间、或目录不存在 → `unknown`（**不猜** —— 与项目其它地方一致：
    不确定就说不知道，而不是默认"没问题"）。
    """
    run_dt = _parse(run_time)
    if run_dt is None:
        return Freshness(UNKNOWN, detail="没有可用的运行时间（旧数据可能没记）")
    root = Path(workspace) / "home" / "ubuntu"
    if not root.is_dir():
        return Freshness(UNKNOWN, run_time=run_dt, detail=f"工作区不存在：{root}")

    newest: Optional[datetime] = None
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        # 空目录不算"被覆盖"（没有文件可被覆盖），但也别让它看起来像验证过了
        return Freshness(FRESH, run_time=run_dt, detail="工作区里没有文件（无可覆盖）")

    state = STALE if newest > run_dt else FRESH
    return Freshness(state, newest_mtime=newest, run_time=run_dt)


DIGEST_MATCH = "match"
DIGEST_MISMATCH = "mismatch"
DIGEST_UNKNOWN = "unknown"


def artifact_digest(workspace: str) -> Optional[str]:
    """工作区里所有文件的**内容摘要**（排除 `__pycache__`）。

    为什么需要它，而不只靠 mtime：mtime 会被复制、恢复、时钟调整骗过，内容摘要不会。
    口径与 `check_freshness` 一致：**含 `input/`** —— fixtures 也是这次运行的一部分，
    它们被后来的运行重写过同样说明“这个目录不是那一次的了”。
    目录不存在 → `None`（**不猜**）。
    """
    root = Path(workspace) / "home" / "ubuntu"
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            # 读不到就标记一下：既不把它当“内容相同”，也不让整条链崩掉
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def compare_digest(workspace: str, stored: Optional[str]) -> str:
    """事后校验：`match` / `mismatch` / `unknown`。

    `stored` 为空（**历史行**没算过摘要）→ `unknown`，**不是** `match`。
    把“没算过”读成“一致”，就等于把未验证写成已验证。
    """
    if not stored:
        return DIGEST_UNKNOWN
    actual = artifact_digest(workspace)
    if actual is None:
        return DIGEST_UNKNOWN
    return DIGEST_MATCH if actual == stored else DIGEST_MISMATCH


def render_digest_check(state: str, *, indent: str = "    ") -> List[str]:
    """把校验结果说成一句人话（三种状态都要有输出）。"""
    if state == DIGEST_MATCH:
        return [f"{indent}（产物校验：内容与判定时**一致**）"]
    if state == DIGEST_UNKNOWN:
        return [f"{indent}（产物校验：**无法校验** —— 该次运行没留下摘要（历史数据）或目录不存在）"]
    return [f"{indent}⚠️ **产物已被改动**：内容与判定时不一致（这个目录不是判定时的那一份）"]


def render_freshness(freshness: Freshness, *, indent: str = "    ") -> List[str]:
    """给人看的告警行。

    三种状态**都要输出一句**：静默的 fresh 会让人把"没告警"误读成"检查过了"，
    而这里恰恰有一条纪律 —— 不确定/没检查时必须显式说。
    """
    if freshness.state == FRESH:
        return [f"{indent}（新鲜度：产物时间不晚于运行时间 —— 是这次运行的产物）"]
    if freshness.state == UNKNOWN:
        return [f"{indent}⚠️ 新鲜度**无法判断**：{freshness.detail}"]
    return [
        f"{indent}⚠️ **产物不可信**：该工作区已被后来的运行覆盖"
        f"（最新文件 {freshness.newest_mtime:%Y-%m-%d %H:%M:%S}"
        f" 晚于运行时间 {freshness.run_time:%Y-%m-%d %H:%M:%S}）"
        f" —— 下面读到的产物**不是**这次运行的。"
    ]
