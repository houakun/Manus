#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM-judge 校准（T14）的回归测试。

守的是三条**纪律**，而不是三个函数：
1. `Unknown` 是有效答案，且**不计入 κ 的分母**（算成对或错都会伪造一致性）；
2. 金标只有单一类别时 κ **无法计算** → 必须返回 None，不许返回 0 或 1；
3. κ 不达标就**拒绝启用** —— 不是"先用着回头再校准"。
"""

from __future__ import annotations

import json

import pytest

from lab.bench.calibrate import (
    KAPPA_THRESHOLD,
    calibrate,
    cohen_kappa,
    read_gold_csv,
    render_calibration,
    write_label_sheet,
)
from lab.bench.judge import JUDGE_DIMENSIONS, parse_verdict


def _perfect(n: int = 40):
    """两个标注者完全一致的金标/judge 对（每维都达标）。"""
    gold = {f"s{i}": {d: bool(i % 2) for d in JUDGE_DIMENSIONS} for i in range(n)}
    return gold, dict(gold)


def test_cohen_kappa_extremes():
    """完全一致 → 1.0；按几率一致（金标与 judge 独立）→ 接近 0。"""
    assert cohen_kappa([(True, True)] * 10 + [(False, False)] * 10) == pytest.approx(1.0)
    chance = [(True, True), (True, False), (False, True), (False, False)] * 5
    assert cohen_kappa(chance) == pytest.approx(0.0, abs=1e-9)


def test_kappa_is_none_when_gold_has_single_class():
    """双方都只有单一类别时**必须**返回 None（此时期望一致率退化为 1，公式除零）。

    为什么单列一条：返回 1 会假装"完美一致"，返回 0 会假装"完全不一致" —— 两个都是编数据。
    注意"只有一方是常数"并不退化：例如金标全 True、judge 一半 True 时，
    pe = 1×0.5 + 0×0.5 = 0.5，κ = 0 是**有意义**的（不是无法计算）。
    """
    # 双方都恒定 → 退化（pe = 1）
    assert cohen_kappa([(True, True), (True, True)]) is None
    assert cohen_kappa([(False, False)] * 3) is None
    # 空样本 → 无法计算
    assert cohen_kappa([]) is None
    # 只有一方恒定 → **不**退化（这是有意义的 κ）
    assert cohen_kappa([(True, True), (True, False)]) == pytest.approx(0.0)


def test_unknown_is_excluded_from_kappa_denominator():
    """judge 答 Unknown 的样本不进配对，但**要单独计数**（可见性）。"""
    gold = {f"s{i}": {d: True for d in JUDGE_DIMENSIONS} for i in range(30)}
    judged = {sid: {d: True for d in JUDGE_DIMENSIONS} for sid in gold}
    for sid in list(judged)[:10]:
        judged[sid] = {d: None for d in JUDGE_DIMENSIONS}  # 10 条 Unknown

    cal = calibrate(gold, judged)
    for item in cal.dimensions:
        assert item.n_gold == 30
        assert item.n_unknown == 10
        assert item.compared == 20  # 只有 20 条进了 κ


def test_calibration_rejects_below_threshold():
    """κ 不达标 → `enabled is False`，且拒绝理由要点名是哪个维度、κ 是多少。"""
    gold = {f"s{i}": {d: True for d in JUDGE_DIMENSIONS} for i in range(40)}
    # judge 在一半样本上答 False → 一致性很差（κ 明显低于 0.6）
    judged = {sid: {d: (i % 2 == 0) for d in JUDGE_DIMENSIONS} for i, sid in enumerate(gold)}
    cal = calibrate(gold, judged)
    assert cal.enabled is False
    reason = cal.refusal_reason()
    assert "κ=" in reason and any(d in reason for d in JUDGE_DIMENSIONS)


def test_calibration_enabled_when_all_dimensions_pass():
    """每个维度都达标时才允许启用（木桶原理：一维不可信就不下结论）。"""
    gold, judged = _perfect()
    cal = calibrate(gold, judged)
    assert cal.enabled is True
    assert cal.refusal_reason() == ""
    assert f"κ ≥ {KAPPA_THRESHOLD}" in render_calibration(cal)


def test_parse_verdict_treats_garbage_as_unknown():
    """解析失败一律算 Unknown —— 绝不当成 true/false（否则会伪造一致性）。"""
    assert parse_verdict("x", '{"verdict": true, "reason": "ok"}').verdict is True
    assert parse_verdict("x", '{"verdict": false}').verdict is False
    assert parse_verdict("x", '{"verdict": "unknown"}').verdict is None
    assert parse_verdict("x", "我看不出来").verdict is None
    assert parse_verdict("x", "我看不出来").error is not None


def test_judge_run_calls_each_dimension_independently():
    """按维度**独立调用**：三次调用，每次的提示里只出现一个维度。

    为什么钉住它：把三件事塞进一个 prompt，会让不同维度的错误混在一起，
    校准就分不清"哪一维不可信" —— 而恰恰是某些维度不可信。
    """
    import asyncio

    class _FakeLLM:
        def __init__(self) -> None:
            self.prompts = []

        async def invoke(self, messages, **kwargs):
            self.prompts.append(json.dumps(messages, ensure_ascii=False))
            return {"content": '{"verdict": true, "reason": "ok"}'}

    from lab.bench.judge import judge_run

    llm = _FakeLLM()
    report = asyncio.run(judge_run(llm, goal="把结果写入 out.txt", artifacts="内容"))
    assert len(llm.prompts) == len(JUDGE_DIMENSIONS)
    for dimension, prompt in zip(JUDGE_DIMENSIONS, llm.prompts):
        assert dimension in prompt
        others = [d for d in JUDGE_DIMENSIONS if d != dimension]
        assert not any(other in prompt for other in others)
    assert len(report.decided) == len(JUDGE_DIMENSIONS)


def test_label_sheet_does_not_leak_judge_verdicts(tmp_path):
    """标注表里**不能**出现 judge 的判定（否则 κ 测的是"人愿不愿意同意机器"）。"""
    path = tmp_path / "judge_gold.csv"
    rows = [{"sample_id": "s1", "task_key": "syn_sum_range", "artifact_dir": "/tmp/ws"}]
    assert write_label_sheet(rows, path) == 1
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert "human_task_completion" in header
    assert "judge" not in header.lower()
    # 空单元格读回时必须是 None（未标注 ≠ 判为 False）
    gold = read_gold_csv(path)
    assert gold["s1"]["task_completion"] is None
