#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分级抽样（P3a）的回归测试。

守的是**三条约束**（success criteria 明写的），而不是几个函数：
1. `stale 工作区 = 0` —— 产物不属于那次运行的样本一律不进表；
2. 不含那两个已修缺陷的任务（它们的历史运行是在**旧 goal** 下产生的）；
3. **负样本跨多个任务** —— 这是旧口径最大的毛病（100 条里所有反例都来自同一个任务）。

另外守一条**认识论**纪律：`level` 是**来源元数据，不是标签**。
一个 "mild" 样本完全可能仍然合格（那就该标 true）—— 把 level 当标签用，
等于用抽样设计去伪造结论。
"""

from __future__ import annotations

import csv
import pathlib

import pytest

from lab.bench.sampling import (
    LEVEL_EMPTY,
    LEVEL_POSITIVE,
    LEVEL_WRONG,
    Candidate,
    derive_mild,
    perturb_text,
    plan_synthetic,
    product_paths_from_verify,
    select_from_runs,
)


def _c(sample_id: str, task: str, *, ok: bool = True, stale: bool = False) -> Candidate:
    return Candidate(sample_id, task, "synthetic", ok, stale, f"/ws/{sample_id}")


# ==================== 约束 1 + 2：剔 stale、排缺陷任务 ====================


def test_select_from_runs_drops_stale_and_excluded_tasks():
    """stale 的必须丢（产物不是那次的），被排除的任务也必须丢。"""
    candidates = [
        _c("keep", "task_a"),
        _c("stale", "task_a", stale=True),
        _c("excluded", "sem_markdown_toc"),
        _c("keep2", "task_b", ok=False),
    ]
    samples = select_from_runs(candidates, exclude_tasks=["sem_markdown_toc"], per_task=2)
    assert {s.sample_id for s in samples} == {"keep", "keep2"}
    assert all(s.task_key != "sem_markdown_toc" for s in samples)


def test_select_from_runs_quota_is_per_task_and_deterministic():
    """配额是**按任务**算的，且同输入必然同输出（抽样要可复现，否则 κ 不可审计）。"""
    candidates = [_c(f"p{i}", "task_a") for i in range(5)] + [_c(f"n{i}", "task_a", ok=False) for i in range(5)]
    first = select_from_runs(candidates, per_task=2)
    second = select_from_runs(list(reversed(candidates)), per_task=2)
    assert [s.sample_id for s in first] == [s.sample_id for s in second]
    assert sum(1 for s in first if s.level == LEVEL_POSITIVE) == 2
    assert sum(1 for s in first if s.level != LEVEL_POSITIVE) == 2


# ==================== 约束 3：负样本跨多个任务 ====================


def test_plan_synthetic_spans_many_tasks():
    """构造负样本必须**每个任务都有** —— 旧口径下所有反例来自一个任务，κ 谈不上能推广。"""
    task_keys = [f"task_{i}" for i in range(6)]
    samples = plan_synthetic(task_keys, exclude_tasks=["task_5"], root="/tmp/root", levels=[LEVEL_WRONG])
    assert {s.task_key for s in samples} == {f"task_{i}" for i in range(5)}
    assert len({s.task_key for s in samples}) >= 3
    # 每条的 artifact_dir 必须各不相同（否则又会互相覆盖）
    assert len({s.artifact_dir for s in samples}) == len(samples)


def test_plan_synthetic_default_levels_are_graded():
    """默认级别是**分层**的：轻微扰动 → 明显错 → 完全没做。"""
    samples = plan_synthetic(["task_a"], root="/tmp/root")
    assert [s.level for s in samples] == ["mild", "wrong_solution", "empty"]


# ==================== 构造材料本身要正确 ====================


def test_perturb_text_changes_the_first_digit():
    """`mild` 的做法是"改一个数字" —— 最接近 Agent 算错一步的细微错。"""
    assert perturb_text("结果=385") == "结果=085"
    assert perturb_text("abc") == "ab0"  # 没有数字就动最后一个字符
    assert perturb_text("") == ""


def test_derive_mild_copies_and_perturbs(tmp_path):
    """`mild` 必须真的从**正确产物**派生（复制整目录 + 改一处），而不是凭空造。"""
    source = tmp_path / "src"
    product = source / "home" / "ubuntu" / "out.txt"
    product.parent.mkdir(parents=True)
    product.write_text("385", encoding="utf-8")
    (source / "home" / "ubuntu" / "input").mkdir()
    (source / "home" / "ubuntu" / "input" / "data.txt").write_text("素材", encoding="utf-8")

    dest = tmp_path / "dst" / "workspace"
    assert derive_mild(str(source), str(dest), ["out.txt"]) is True

    assert (dest / "home" / "ubuntu" / "input" / "data.txt").read_text(encoding="utf-8") == "素材"
    assert (dest / "home" / "ubuntu" / "out.txt").read_text(encoding="utf-8") == "085"
    # 源目录不能被改动（派生不是破坏）
    assert product.read_text(encoding="utf-8") == "385"


def test_derive_mild_only_touches_the_graded_product(tmp_path):
    """扰动目标必须**只**来自 `targets`（判定器检查的产物）—— 不能碰其它文件。

    实测踩过：用“最大的文本文件”时，18 条 mild 里有 **4 条改到了
    `repo/tests/run_tests.py`**（CI-FROZEN，任务禁止修改），另有 5 条改到了
    agent 自己写的 verify 脚本 —— 那些样本其实**依然是对的**，标为负样本就是编数据。
    """
    source = tmp_path / "src2"
    root = source / "home" / "ubuntu"
    root.mkdir(parents=True)
    (root / "graded.txt").write_text("123", encoding="utf-8")
    # 一个很大、但**不是**被判定产物的文件：绝不能被扰动
    (root / "aux_report.md").write_text("x" * 5000, encoding="utf-8")

    dest = tmp_path / "dst2"
    assert derive_mild(str(source), str(dest), ["graded.txt"]) is True
    assert (dest / "home" / "ubuntu" / "graded.txt").read_text(encoding="utf-8") == "023"
    assert (dest / "home" / "ubuntu" / "aux_report.md").read_text(encoding="utf-8") == "x" * 5000


def test_product_paths_from_verify_strips_the_sandbox_prefix(tmp_path):
    """`/home/ubuntu/x` 必须被转成相对路径 `x`（否则永远找不到文件）。"""
    from lab.bench.task import load_all_tasks

    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    assert product_paths_from_verify(task) == ["sum.txt"]


def test_derive_mild_refuses_when_no_graded_product_is_present(tmp_path):
    """`targets` 里一个都不存在 → 返回 False，且**不留下副本**（不伪造样本）。"""
    source = tmp_path / "src3"
    (source / "home" / "ubuntu").mkdir(parents=True)
    (source / "home" / "ubuntu" / "other.txt").write_text("内容", encoding="utf-8")
    dest = tmp_path / "dst3"
    assert derive_mild(str(source), str(dest), ["sum.txt"]) is False
    assert not dest.exists(), "派生失败时不能留下副本（否则就是一条伪装的 mild 样本）"
    assert derive_mild(str(tmp_path / "nope"), str(tmp_path / "dst4"), ["sum.txt"]) is False
    assert not (tmp_path / "dst4").exists()


# ==================== 端到端：生成出来的表必须满足三条约束 ====================


def test_balanced_sheet_satisfies_the_three_constraints(tmp_path, monkeypatch):
    """直接对**产出的标注表**验证三条约束（只测规划函数是不够的）。

    物化被替换成空操作：这一条要测的是"表对不对"，不是"磁盘写得好不好"
    （后者另有专门用例）。
    """
    from lab import api as lab_api
    from lab.cli import main

    fresh_ws = tmp_path / "fresh_ws"
    (fresh_ws / "home" / "ubuntu").mkdir(parents=True)
    (fresh_ws / "home" / "ubuntu" / "out.txt").write_text("385", encoding="utf-8")

    class _FakeStore:
        def sample_runs_for_judging(self, limit=100):
            base = {
                "group_name": "synthetic",
                "run_index": 0,
                "arm": "",
                "self_reported_ok": 1,
                "workspace": str(fresh_ws),
            }
            return [
                # 新鲜（created_at 在未来 → 文件一定不晚于它）
                {**base, "suite_id": "aaaa1111-x", "task_key": "syn_sum_range", "ok": 1,
                 "created_at": "2099-01-01T00:00:00"},
                # stale（created_at 在过去 → 文件晚于它）
                {**base, "suite_id": "bbbb2222-x", "task_key": "syn_sum_range", "ok": 0,
                 "created_at": "2000-01-01T00:00:00"},
                # 已修缺陷的任务 → 必须被排除
                {**base, "suite_id": "cccc3333-x", "task_key": "sem_markdown_toc", "ok": 1,
                 "created_at": "2099-01-01T00:00:00"},
            ]

    async def _materialize_dirs(samples, by_key, positives):
        """真实物化会写 fixtures/产物；这里只需把目录造出来（本用例测的是"表对不对"）。"""
        for sample in samples:
            pathlib.Path(sample.artifact_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _FakeStore())
    monkeypatch.setattr("lab.cli.ensure_runs_dir", lambda: tmp_path)
    monkeypatch.setattr("lab.cli._materialize_synthetic", _materialize_dirs)

    out = tmp_path / "sheet.csv"
    code = main(["bench", "judge-sheet", "--balanced", "--out", str(out), "--level", "empty"])
    assert code == 0

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows, "必须真的产出样本"
    # 约束 1：stale 一律不进表
    assert all(row["workspace_stale"] == "0" for row in rows)
    assert all("2000-01-01" not in row["artifact_dir"] for row in rows)
    # 约束 2：不含已修缺陷的任务
    assert not any(row["task_key"] in ("sem_markdown_toc", "syn_date_diff") for row in rows)
    # 约束 3：负样本跨多个任务
    negative_tasks = {row["task_key"] for row in rows if row["level"] != LEVEL_POSITIVE}
    assert len(negative_tasks) >= 3, f"负样本只覆盖了 {negative_tasks}"
    # level 是元数据列，必须真的写出来（分布才可读）
    assert all(row["level"] for row in rows)
    # **不许有幽灵样本**：每一行的 artifact_dir 必须真实存在
    # （实测踩过：库里没正样本的任务，`mild` 派生失败却仍在表里占一行）
    assert all(pathlib.Path(row["artifact_dir"]).is_dir() for row in rows)


def test_balanced_sheet_drops_unmaterialized_samples(tmp_path, monkeypatch):
    """物化失败的构造样本必须被**丢弃**，不能留在表里变成"幽灵样本"。

    为什么这条重要：幽灵样本的 artifact_dir 不存在 → judge 去评空材料、
    人去标看不见的东西。它不会报错，只会安静地把噪声混进 κ ——
    与本次要修的“工作区覆盖”是同一类错误（静默失真）。
    """
    from lab import api as lab_api
    from lab.cli import main

    fresh_ws = tmp_path / "fresh_ws"
    (fresh_ws / "home" / "ubuntu").mkdir(parents=True)
    (fresh_ws / "home" / "ubuntu" / "out.txt").write_text("385", encoding="utf-8")

    class _FakeStore:
        def sample_runs_for_judging(self, limit=100):
            return [
                {"suite_id": "aaaa1111-x", "task_key": "syn_sum_range", "group_name": "synthetic",
                 "run_index": 0, "arm": "", "ok": 1, "self_reported_ok": 1,
                 "workspace": str(fresh_ws), "created_at": "2099-01-01T00:00:00"}
            ]

    async def _materialize_nothing(samples, by_key, positives):
        return None  # 全部物化失败

    monkeypatch.setattr(lab_api, "default_trace_store", lambda: _FakeStore())
    monkeypatch.setattr("lab.cli.ensure_runs_dir", lambda: tmp_path)
    monkeypatch.setattr("lab.cli._materialize_synthetic", _materialize_nothing)

    out = tmp_path / "sheet.csv"
    assert main(["bench", "judge-sheet", "--balanced", "--out", str(out), "--level", "empty"]) == 0

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    # 构造样本全部被丢 → 只剩那条真实运行；关键是**没有幽灵行**
    assert all(pathlib.Path(row["artifact_dir"]).is_dir() for row in rows)
    assert all(not row["sample_id"].startswith("synthetic:") for row in rows)


def test_materialize_writes_the_tasks_wrong_solution(tmp_path):
    """物化 `wrong_solution` 必须真把任务自带的错误解写进去（否则负样本是空的）。"""
    import asyncio

    from lab.bench.sampling import Sample, materialize
    from lab.bench.task import load_all_tasks

    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    sample = Sample("synthetic:syn_sum_range:wrong_solution", task.key, "synthetic",
                    LEVEL_WRONG, str(tmp_path / "ws"))
    assert asyncio.run(materialize(sample, task, level=LEVEL_WRONG)) is True

    # 任务自带错误解写的是 500499（正确是 500500）
    written = (tmp_path / "ws" / "home" / "ubuntu" / "sum.txt").read_text(encoding="utf-8")
    assert written.strip() == "500499"


@pytest.mark.parametrize("level", [LEVEL_EMPTY])
def test_materialize_empty_leaves_only_fixtures(tmp_path, level):
    """`empty` 级别只铺 fixtures、不产出 —— 模拟"完全没做"。"""
    import asyncio

    from lab.bench.sampling import Sample, materialize
    from lab.bench.task import load_all_tasks

    task = next(t for t in load_all_tasks() if t.key == "syn_sum_range")
    sample = Sample("synthetic:syn_sum_range:empty", task.key, "synthetic", level, str(tmp_path / "ws2"))
    assert asyncio.run(materialize(sample, task, level=level)) is True
    root = tmp_path / "ws2" / "home" / "ubuntu"
    assert root.is_dir()
    assert not (root / "sum.txt").exists()
