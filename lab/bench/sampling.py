#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""judge 校准的**抽样口径**（P3a）：把"失败优先"换成**有配额、有分级、跨任务**。

== 为什么要重做口径 ==
`sample_runs_for_judging` 原本是"失败优先，再补成功"。听起来合理，但实测产出的
100 条金标里 **89 条三个维度全 true**，而**所有** `false` 都来自同一个任务
（`sem_markdown_toc`）—— 于是那次的 κ 实质上只测了一个任务，谈不上"能推广"。

更根本的是：**库里的"失败"大多不是 Agent 失败**。逐条核对 23 条失败样本：
15 条人工认为"做完了"（其中 10 条是任务期望值写错的假失败、3 条是工作区被覆盖、
2 条是格式/内容归因分歧）。所以"多抽失败"这条路本身就靠不住 —— 它抽到的主要是
测量缺陷，不是坏产物。

== 所以改成两个来源拼起来 ==
1. **真实运行**（`select_from_runs`）：按任务配额挑正/负样本，且**先剔掉 stale 工作区**
   （产物不是那次运行的）与被排除的任务。
2. **分级负样本**（`plan_synthetic`）：不再依赖"库里碰巧有失败"，而是**为每个任务造**
   三个级别 —— 轻微扰动 / 任务自带的错误解 / 完全没产出。这样负样本天然跨任务，
   且难度是分层的（judge 能不能分辨"细微错"和"明显错"，才是有信息量的问题）。

⚠️ `level` 是**来源元数据**，不是金标。金标只有人工填的那三列 ——
一个"轻微扰动"样本完全可能仍然是合格的（那就该标 true）。
把 level 当标签用，就等于用抽样设计去伪造结论。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

# 级别：从"轻微"到"完全没做"
LEVEL_POSITIVE = "positive"  # 真实运行，判定通过
LEVEL_DB_NEGATIVE = "db_negative"  # 真实运行，判定失败（可能仍是假失败 → 由人工定）
LEVEL_MILD = "mild"  # 从正确产物**扰动**一个字符（细微错）
LEVEL_WRONG = "wrong_solution"  # 任务自带的错误解（明显错）
LEVEL_EMPTY = "empty"  # 只铺 fixtures、不产出（完全没做）

# 造负样本时默认用哪几级（顺序即难度递进）
SYNTHETIC_LEVELS = (LEVEL_MILD, LEVEL_WRONG, LEVEL_EMPTY)


@dataclass
class Candidate:
    """一条**真实运行**候选（由调用方从库里取出并把新鲜度算好）。"""

    sample_id: str
    task_key: str
    group: str
    ok: bool
    stale: bool
    workspace: str


@dataclass
class Sample:
    """一条抽样结果（artifact_dir 是产物目录，level 只是来源元数据）。"""

    sample_id: str
    task_key: str
    group: str
    level: str
    artifact_dir: str
    note: str = ""


def select_from_runs(
    candidates: Sequence[Candidate],
    *,
    exclude_tasks: Iterable[str] = (),
    per_task: int = 2,
) -> List[Sample]:
    """从真实运行里挑样本：**先剔 stale 与被排除的任务**，再按任务配额取正/负。

    为什么 stale 必须剔掉（而不是"标一下继续用"）：stale 意味着产物属于**另一次运行**，
    于是"judge 判得对不对"和"人标得对不对"都失去了共同对象 —— 用它们算出来的 κ
    是三个东西混在一起（judge × 人 × 另一个运行的产物）。

    为什么按任务配额：不配额时样本量会被少数"跑得多"的任务吃掉（实测 100 条里
    `sem_bug_fix`/`sem_sqlite_report` 等占了绝大多数），任务之间的分布极不均衡。
    """
    excluded = set(exclude_tasks)
    kept = [c for c in candidates if not c.stale and c.task_key not in excluded]

    by_task: Dict[str, List[Candidate]] = {}
    for candidate in kept:
        by_task.setdefault(candidate.task_key, []).append(candidate)

    out: List[Sample] = []
    for task_key in sorted(by_task):
        # 排序键里放 sample_id 是为了**确定性**：同样输入必然给同样样本（可复现）
        rows = sorted(by_task[task_key], key=lambda c: (c.ok, c.sample_id))
        for candidate in [c for c in rows if c.ok][:per_task]:
            out.append(
                Sample(candidate.sample_id, task_key, candidate.group, LEVEL_POSITIVE, candidate.workspace)
            )
        for candidate in [c for c in rows if not c.ok][:per_task]:
            out.append(
                Sample(
                    candidate.sample_id,
                    task_key,
                    candidate.group,
                    LEVEL_DB_NEGATIVE,
                    candidate.workspace,
                    note="库里判定失败（可能是假失败 —— 由人工定，不预设答案）",
                )
            )
    return out


def plan_synthetic(
    task_keys: Iterable[str],
    *,
    exclude_tasks: Iterable[str] = (),
    root: str,
    levels: Sequence[str] = SYNTHETIC_LEVELS,
) -> List[Sample]:
    """为每个任务造**分级负样本**（纯规划，不碰文件系统；物化见 `materialize`）。

    这样负样本的来源就从"库里碰巧有失败"变成"每个任务都有" —— 跨任务覆盖是
    success criteria 明写的一条（`负样本跨多个任务`）。
    """
    excluded = set(exclude_tasks)
    out: List[Sample] = []
    for task_key in sorted(set(task_keys) - excluded):
        for level in levels:
            out.append(
                Sample(
                    sample_id=f"synthetic:{task_key}:{level}",
                    task_key=task_key,
                    group="synthetic",
                    level=level,
                    artifact_dir=str(Path(root) / task_key / level / "workspace"),
                    note=f"构造样本（{level}）—— 来源是任务定义，不是某次真实运行",
                )
            )
    return out


def perturb_text(text: str) -> str:
    """把文本里**第一个数字**改掉（没有数字就改最后一个字符）。

    为什么是"改一个数字"：这是最接近"Agent 算错一步/抄错一位"的细微错，
    也正是确定型判定器能抓、而 judge 未必能抓的那一类 —— 校准要的正是这种分辨力。
    """
    for index, char in enumerate(text):
        if char.isdigit():
            replacement = "0" if char != "0" else "1"
            return text[:index] + replacement + text[index + 1 :]
    if not text:
        return text
    return text[:-1] + ("0" if text[-1] != "0" else "1")


def _relative_to_home(path) -> str:
    """把 `/home/ubuntu/x` 或 `home/ubuntu/x` 转成相对路径 `x`（其它形式原样去前导斜杠）。"""
    rel = str(path or "").replace("\\", "/")
    for prefix in ("/home/ubuntu/", "home/ubuntu/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix) :]
            break
    return rel.lstrip("/")


def product_paths_from_verify(task) -> List[str]:
    """从任务的 `verify` 里取出**该被扰动的产物路径**（相对 `home/ubuntu`），顺序即优先级。

    两条都是实测校准出来的（每一条都对应一次"差点的编数据"）：
    1. **必须按 verify 的路径来**，而不是"最大的文本文件" —— 后者会把扰动打到
       agent 自己写的辅助脚本（`verify_*.py`）或报告文档上，而那些**不是**被判定产物：
       任务其实**依然是对的**，把它标成负样本就是编数据；
    2. **fixture 路径排到最后**：`ci` 任务的 verify 会检查
       `repo/tests/run_tests.py`（只要求 `# CI-FROZEN` 标记还在 = 冻结文件没被删改），
       那是**输入**；扰动它只会制造"违规"，与任务要测的能力无关。

    取不到任何路径 → 返回空列表（调用方据此**放弃**这条样本，而不是伪造）。
    """
    fixtures = {_relative_to_home(fixture.path) for fixture in (getattr(task, "fixtures", None) or [])}
    preferred: List[str] = []
    fallback: List[str] = []
    for check in getattr(task, "verify", None) or []:
        rel = _relative_to_home(getattr(check, "path", None))
        if not rel:
            continue
        bucket = fallback if rel in fixtures else preferred
        if rel not in bucket:
            bucket.append(rel)
    return preferred + [rel for rel in fallback if rel not in preferred]


def derive_mild(source_workspace: str, dest_workspace: str, targets: Sequence[str]) -> bool:
    """从**正确产物**派生一个"轻微扰动"样本（复制目录 + 改一个数字）。

    `targets` 必须是**被判定器检查的产物路径**（见 `product_paths_from_verify`）。
    找不到任何一个可扰动的目标 → 返回 False（**放弃这条，不伪造**）。
    """
    source = Path(source_workspace)
    root = source / "home" / "ubuntu"
    if not root.is_dir():
        return False

    chosen: str | None = None
    for rel in targets:
        candidate = root / rel
        if not candidate.is_file():
            continue
        try:
            if b"\x00" in candidate.read_bytes()[:2048]:
                continue  # 二进制产物不扰动（改一个字节不是"轻微错"，是损坏）
        except OSError:
            continue
        chosen = rel
        break
    if chosen is None:
        return False

    dest = Path(dest_workspace)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)

    product = dest / "home" / "ubuntu" / chosen
    try:
        text = product.read_text(encoding="utf-8", errors="replace")
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        return False
    product.write_text(perturb_text(text), encoding="utf-8")
    return True
async def materialize(sample: Sample, task, *, level: str) -> bool:
    """把一条构造样本真正写进磁盘（复用 `bench validate` 的物化器，不重写）。

    - `wrong_solution`：铺 fixtures + 执行任务自带的错误解
    - `empty`：只铺 fixtures（模拟"完全没做"）
    - `mild`：由调用方先准备好源工作区（见 `derive_mild`），这里不动
    """
    from lab.bench.validate import _apply_fixtures, _apply_steps
    from lab.infra.local_sandbox import LocalSandbox

    if level == LEVEL_MILD:
        return False  # mild 的物化是"复制 + 扰动"，由 derive_mild 负责

    workspace = Path(sample.artifact_dir)
    sandbox = LocalSandbox(workspace, exec_timeout=60)
    await sandbox.ensure_sandbox()
    await _apply_fixtures(sandbox, task.fixtures)
    if level == LEVEL_WRONG:
        await _apply_steps(sandbox, task.wrong, "wrong_solution")
    return True
