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
from typing import Dict, Optional

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
    # 显式标注三态：`{d: None ...}` 要能赋值回去，否则静态检查会因为
    # `dict[str, bool]` 的值类型**不变**而报错（运行时没问题，但注解该说真话）。
    judged: Dict[str, Dict[str, Optional[bool]]] = {sid: {d: True for d in JUDGE_DIMENSIONS} for sid in gold}
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
    """按维度**独立调用**：三次调用，每次只判一个维度（且只带**该维度**的 rubric 正文）。

    为什么钉住它：把三件事塞进一个 prompt，会让不同维度的错误混在一起，
    校准就分不清“哪一维不可信” —— 而恰恰是某些维度不可信。

    ⚠️ 不能断言“提示里不出现其他维度的**名字**”：rubric v2 正是**故意提及**
    `output_quality` 来划清边界（“内容错属于 output_quality”）——
    所以这里改成断言“只带本维度的 **rubric 正文**”，那才是“分开判”的真正含义。
    """
    import asyncio

    class _FakeLLM:
        def __init__(self) -> None:
            self.prompts = []

        async def invoke(self, messages, **kwargs):
            self.prompts.append(json.dumps(messages, ensure_ascii=False))
            return {"content": '{"verdict": true, "reason": "ok"}'}

    from lab.bench.judge import active_rubric_version, judge_run, rubric_for

    rubric = rubric_for(active_rubric_version())
    llm = _FakeLLM()
    report = asyncio.run(judge_run(llm, goal="把结果写入 out.txt", artifacts="内容"))
    assert len(llm.prompts) == len(JUDGE_DIMENSIONS)
    for dimension, prompt in zip(JUDGE_DIMENSIONS, llm.prompts):
        assert f"评审维度：{dimension}" in prompt
        assert rubric[dimension] in prompt  # 本维度的 rubric 正文在
        for other in (d for d in JUDGE_DIMENSIONS if d != dimension):
            assert rubric[other] not in prompt, f"{dimension} 的提示里混进了 {other} 的 rubric"
    assert len(report.decided) == len(JUDGE_DIMENSIONS)
    assert report.rubric_version == active_rubric_version()


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


# ==================== 跑 judge（`bench judge`）的材料收集与批量执行 ====================


def test_collect_artifacts_keeps_input_and_skips_noise(tmp_path):
    """收集材料的三条口径：排除 `__pycache__`、二进制只列不展开、`input/` 要**保留并标注**。

    为什么 `input/` 要保留：`sem_refactor_config` 要求改写题目给的 `input/service.py` ——
    排除它 judge 就无从判断。（`bench show` 排除它是因为那里的目的是"给人看产物"。）
    """
    from lab.bench.judge import collect_artifacts

    root = tmp_path / "home" / "ubuntu"
    (root / "input").mkdir(parents=True)
    (root / "__pycache__").mkdir(parents=True)
    (root / "out.txt").write_text("385\n", encoding="utf-8")
    (root / "input" / "service.py").write_text("port = 8080\n", encoding="utf-8")
    (root / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01\x02")
    (root / "blob.bin").write_bytes(b"\x00\xff\x00")

    text = collect_artifacts(str(tmp_path))
    assert "out.txt" in text and "385" in text
    assert "input/service.py" in text and "题目输入" in text
    assert "__pycache__" not in text
    # 二进制要**列出来**（否则"少了一个文件"看不出来），但不展开内容
    assert "blob.bin" in text and "二进制" in text


def test_collect_artifacts_says_so_when_nothing_to_judge(tmp_path):
    """没有材料时必须**如实说**，不能返回空串 —— 空串会被 judge 当成"材料里没提"。"""
    from lab.bench.judge import collect_artifacts

    assert "工作区不存在" in collect_artifacts(str(tmp_path / "nope"))
    (tmp_path / "home" / "ubuntu").mkdir(parents=True)
    assert "没有任何文件" in collect_artifacts(str(tmp_path))


def test_judge_entries_tristate_and_failure_isolation():
    """批量跑 judge：返回三态，且**单条失败不炸整批**（失败的样本仍留下 None 键）。"""
    import asyncio

    from lab.bench.judge import judge_entries

    class _FakeLLM:
        async def invoke(self, messages, **kwargs):
            prompt = messages[-1]["content"]
            if "boom" in prompt:
                raise RuntimeError("上游 500")
            if "不确定" in prompt:
                return {"content": '{"verdict": "unknown"}'}
            return {"content": '{"verdict": true, "reason": "ok"}'}

    entries = [
        {"sample_id": "s-ok", "goal": "写文件", "artifacts": "内容"},
        {"sample_id": "s-unknown", "goal": "写文件 不确定", "artifacts": "内容"},
        {"sample_id": "s-boom", "goal": "写文件 boom", "artifacts": "内容"},
    ]
    judged = asyncio.run(judge_entries(_FakeLLM(), entries, concurrency=2))

    assert set(judged) == {"s-ok", "s-unknown", "s-boom"}
    assert all(v is True for v in judged["s-ok"].values())
    assert all(v is None for v in judged["s-unknown"].values())
    # 抛异常的那条也必须**留下键**：省略键会让 calibrate 当它不存在，
    # 而"judge 崩了"和"judge 说不知道"对 κ 的影响不同，必须可见。
    assert all(v is None for v in judged["s-boom"].values())


def test_judge_entries_calls_on_sample_incrementally():
    """`on_sample` 必须**每完成一条就回调** —— 它是增量落盘的基础。

    为什么钉住它：294 次调用要跑十几分钟，只在最后写一次的话，第 293 条断掉
    就是"整批白跑"；有回调才能做到"最坏只丢一条"。
    """
    import asyncio

    from lab.bench.judge import judge_entries

    class _FakeLLM:
        async def invoke(self, messages, **kwargs):
            return {"content": '{"verdict": true}'}

    seen = []
    entries = [{"sample_id": f"s{i}", "goal": "g", "artifacts": "a"} for i in range(4)]
    asyncio.run(
        judge_entries(
            _FakeLLM(),
            entries,
            concurrency=2,
            on_sample=lambda sample_id, verdicts: seen.append(sample_id),
        )
    )
    assert sorted(seen) == ["s0", "s1", "s2", "s3"]


def test_read_gold_rows_keeps_material_columns(tmp_path):
    """`read_gold_rows` 必须带出 `task_key` / `artifact_dir` —— 否则无法还原"目标 + 产物"。"""
    from lab.bench.calibrate import read_gold_rows

    path = tmp_path / "g.csv"
    path.write_text(
        "sample_id,task_key,artifact_dir,human_task_completion\n"
        "s1,syn_sum_range,/tmp/ws,true\n"
        "s2,syn_sum_range,/tmp/ws2,\n",
        encoding="utf-8",
    )
    rows = read_gold_rows(path)
    assert [row["sample_id"] for row in rows] == ["s1", "s2"]
    assert rows[0]["task_key"] == "syn_sum_range"
    assert rows[0]["artifact_dir"] == "/tmp/ws"


def test_meta_key_in_judged_json_does_not_disturb_calibration():
    """`bench judge` 写的 `__meta__` 不能污染 κ（calibrate 只按**金标**的 sample_id 取判定）。"""
    gold = {f"s{i}": {d: bool(i % 2) for d in JUDGE_DIMENSIONS} for i in range(40)}
    payload = {"__meta__": {"rubric_version": "v1", "model_name": "m"}, **gold}
    cal = calibrate(gold, payload)
    assert cal.enabled is True
