#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""标注审计（`lab/bench/label_audit.py`）的回归测试。

守的是**两条实测揪出来的规则缺口** —— 它们不是想出来的，是"规则推的标签与人的标签对不上"
反推出来的：

1. **"文件不存在"是形状问题**，即使检查类型是 `file_content`。
   只看 `kind` 会把"产物根本没产出"误判成内容错（实测 27 条不一致）。
2. **任何失败 → `OQ=false`**。形状坏了就是"没有可接受的产物"，人就是这么判的
   （实测 18 条不一致：`empty` 与"该删的没删"）。

把这两条钉住，是为了它们不会再被"顺手简化"回去 —— 那一简化，审计就会重新开始骗人。
"""

from __future__ import annotations

from lab.bench.label_audit import _judge_tri, kind_of, render_audit, rule_labels


class _FakeCheck:
    """只需要 `.kind` 与 `.line()` —— 故意不依赖 `CheckResult` 的内部形状。"""

    def __init__(self, kind: str, line: str) -> None:
        self.kind = kind
        self._line = line

    def line(self) -> str:
        return self._line


def test_kind_of_treats_missing_file_as_shape():
    """缺口 1：`file_content` 失败但文件不存在 → **形状**（不是内容）。"""
    missing = _FakeCheck("file_content", "✗ [file_content] 生成的目录: 读取失败，文件不存在或不可读: /home/ubuntu/TOC.md")
    assert kind_of(missing) == "形状"


def test_kind_of_treats_present_but_wrong_content_as_content():
    """对照：文件在、内容不对 → 内容。"""
    wrong = _FakeCheck("file_content", "✗ [file_content] 生成的目录: 期望 'A'，实际 'B'")
    assert kind_of(wrong) == "内容"
    assert kind_of(_FakeCheck("numeric", "✗ [numeric] 值: 期望 385，实际 386")) == "内容"
    assert kind_of(_FakeCheck("file_exists", "✗ [file_exists] x: 期望存在=True，实际=False")) == "形状"
    assert kind_of(_FakeCheck("exec_script", "✗ [exec_script] 退出码 1")) == "行为"


def test_rule_labels_any_failure_makes_oq_false():
    """`(TC, IF, OQ)`：**任何失败 → TC=false 且 OQ=false**；IF **只看形状**。

    ⚠️ TC 也要比（这行是踩坑后补的）：审计原先只比 IF/OQ，于是我把 16 条 `ci_*`
    （诊断没点出正确模块、**但代码修好且 CI 通过**）标成 TC=true，
    **违背了同文件写的规则，却没有工具报出来** —— 要等到人肉看 κ 才发现。
    """
    assert rule_labels({}) == (True, True, True)
    assert rule_labels({"内容": 1}) == (False, True, False)  # 内容错不影响 IF
    assert rule_labels({"行为": 1}) == (False, True, False)
    assert rule_labels({"形状": 1}) == (False, False, False)  # 形状坏了 → IF 也 false
    assert rule_labels({"形状": 2, "内容": 1}) == (False, False, False)


def test_judge_tri_returns_none_when_any_dimension_is_unknown():
    """judge 答 Unknown 时**整体算缺失**（不参与对照），不猜成 true/false。"""
    full = {"task_completion": True, "instruction_following": True, "output_quality": False}
    assert _judge_tri(full) == (True, True, False)
    assert _judge_tri({**full, "task_completion": None}) is None
    assert _judge_tri({**full, "instruction_following": None}) is None
    assert _judge_tri({}) is None


def test_render_audit_reports_rule_vs_human_and_judge_deviation():
    """报告必须把"规则 vs 人"和"judge 偏离规则"**分开**说 —— 它们是两件不同的事。"""
    from lab.bench.label_audit import AuditRow

    rows = [
        AuditRow(
            "s1",
            "task_a",
            "内容×1",
            rule=(False, True, False),
            human=(False, True, False),
            judge=(False, False, False),
        ),
        AuditRow("s2", "task_a", "形状×1", rule=(False, False, False), human=(True, True, False)),
    ]
    text = "\n".join(render_audit(rows, limit=5))
    assert "规则 vs 人：一致 1/2" in text
    assert "规则与人**不一致**" in text and "`s2`" in text
    assert "judge **偏离规则**" in text and "`s1`" in text
