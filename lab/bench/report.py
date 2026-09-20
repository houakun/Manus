#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""报告生成：把 SuiteResult 渲染成 Markdown。

== 两条写作原则 ==
1. **每个数字都必须带它的不确定性**。只给"成功率 78%"是不合格的报告 ——
   读者无法判断这 78% 和上次的 75% 有没有区别。所以成功率一律带 Wilson 区间，
   连续量一律带 t 区间，并且标注 n。
2. **把"这个数字不可信"的地方也写出来**。并发运行时耗时不可比、
   缺少错误解的任务判定强度弱、某个指标的价格表没命中导致成本为 0 ——
   这些都要在报告的"已知限制"里明说。
   一份不写自己弱点的报告，读者只能选择"全信"或"全不信"，两者都是坏的。
"""

from __future__ import annotations

from typing import Any, Dict, List

from lab.bench.runner import SuiteResult
from lab.bench.stats import Interval


def _interval_row(name: str, interval: Interval, unit: str = "", digits: int = 1,
                  clamp_min: Optional[float] = None) -> str:
    """渲染一行"均值 + 区间"。

    clamp_min：对 token/成本/耗时这类**不可能为负**的指标把下界截到 0。
    为什么需要：n 很小时 t 区间会宽到出现负值（例如 tokens 的区间 [-1.1e6, 1.4e6]），
    数学上没错，但印在报告里像 bug，会让读者对整份报告失去信任。
    截断**不会**掩盖"区间很宽"这个事实 —— 我们用 ⚠️ 单独标出来（见下）。
    """
    if interval.low is None:
        value = f"{interval.point:,.{digits}f}{unit}"
        return f"| {name} | {value} | — | {interval.n} |"

    low, high = interval.low, interval.high
    if clamp_min is not None:
        low = max(clamp_min, low)
        if high is not None:
            high = max(clamp_min, high)

    value = f"{interval.point:,.{digits}f}{unit}"
    spread = f"[{low:,.{digits}f}, {high:,.{digits}f}]{unit}"
    # 区间比点估计本身还宽 → 这个数字基本没有信息量，必须显式提醒
    if interval.half_width is not None and interval.point and interval.half_width > abs(interval.point):
        spread += " ⚠️"
    return f"| {name} | {value} | {spread} | {interval.n} |"


def _task_purposes() -> Dict[str, str]:
    """任务 uid → purpose（capability / regression）。

    报告要按 purpose 分开看口径：regression 任务算回归指标，
    capability 任务的波动是能力边界发现，**不能进回归门禁**。
    """
    try:
        from lab.bench.task import load_all_tasks

        return {task.uid: task.purpose for task in load_all_tasks()}
    except Exception:  # noqa: BLE001
        return {}


def _uid_by_key(task_key: str, purposes: Dict[str, str]) -> str:
    """task_level.unstable 里存的是短名，这里反查回 uid 用来查 purpose。"""
    for uid in purposes:
        if uid.endswith("/" + task_key) or uid == task_key:
            return uid
    return ""


def render_report(suite: SuiteResult, *, task_count: Optional[int] = None) -> str:
    """渲染完整报告。"""
    lines: List[str] = []

    # ==================== 头部 ====================
    lines.append("# Agent 可靠性基线报告")
    lines.append("")
    lines.append(f"- **suite**: `{suite.suite_id}`（{suite.label}）")
    lines.append(f"- **开始时间**: {suite.started_at}   **总耗时**: {suite.elapsed_s}s")
    lines.append(f"- **模型**: `{suite.model_name}`   **温度**: {suite.temperature}（评测固定，保证可复现）")
    lines.append(f"- **SUT**: `{suite.outcomes[0].sut_name if suite.outcomes else 'n/a'}`")
    lines.append(f"- **规模**: {task_count or len(suite.task_summaries)} 个任务 × {suite.runs_per_task} 次 = "
                 f"**{suite.total_runs}** 次运行")
    lines.append(f"- **预算模式**: `{suite.budget_mode}`（只记录不干预）")
    # ---- 实验条件：没有这一块，这个报告的数字无法被归因 ----
    lines.append(f"- **加固配置**: `{suite.guard_config or 'all'}`"
                 + ("　**故障注入**: ``" + suite.fault_spec + "``" if suite.fault_spec else "")
                 + ("　**LLM 回放**: `" + suite.replay_mode + "`"
                    if suite.replay_mode and suite.replay_mode != "off" else ""))
    if suite.interleaved:
        lines.append(f"- **交错 A/B**: 是（{len(suite.arm_labels)} 个臂："
                     f"{'、'.join(suite.arm_labels)}）")
    if suite.concurrency > 1:
        lines.append(f"- ⚠️ **并发度**: {suite.concurrency}（耗时指标不可横向比较）")
    if getattr(suite, "env_digest", ""):
        # 环境指纹："这次跑的时候 Agent 看到了哪些工具"。
        # 没有它，换个 shell 启动就能让两次跑不可比，而报告里看不出来。
        lines.append(f"- **环境指纹**: `{suite.env_digest}`（`python -m lab sandbox env` 看详情）")
    lines.append("")

    # ==================== 总体指标 ====================
    lines.append("## 一、总体指标")
    lines.append("")
    rate = suite.success_rate

    # ---- 两个口径必须并列：运行级成功率会被"任务数少、每任务多次"平摊掉 ----
    #      并且按 purpose 分开：regression 才是回归门禁口径，capability 是能力边界。
    purposes = _task_purposes()
    regression_uids = {uid for uid, purpose in purposes.items() if purpose == "regression"}
    task_level = suite.task_level()
    regression_level = (suite.task_level(only_uids=regression_uids)
                        if regression_uids else None)
    lines.append(f"**运行级成功率 {rate.point:.1%}**（成功运行数/总运行数），"
                 f"95% Wilson 区间 [{rate.low:.1%}, {rate.high:.1%}]，n={rate.n}")
    if task_level.tasks:
        pow_rate, at_rate = task_level.pass_pow_k_rate, task_level.pass_at_k_rate
        lines.append("")
        lines.append(f"**任务级 pass^{task_level.k} = {pow_rate.point:.1%}**"
                     f"（{task_level.pass_pow_k}/{task_level.tasks} 个任务 **{task_level.k} 次全对**），"
                     f"区间 [{pow_rate.low:.1%}, {pow_rate.high:.1%}]；"
                     f"pass@{task_level.k} = {at_rate.point:.1%}（至少成功一次）")
        if (regression_level and regression_level.tasks
                and regression_level.tasks != task_level.tasks):
            rp, ra = regression_level.pass_pow_k_rate, regression_level.pass_at_k_rate
            lines.append("")
            lines.append(
                f"**只看 regression 任务（回归门禁口径）**：pass^{regression_level.k} = "
                f"**{rp.point:.1%}**（{regression_level.pass_pow_k}/{regression_level.tasks}），"
                f"区间 [{rp.low:.1%}, {rp.high:.1%}]；"
                f"capability 任务（{task_level.tasks - regression_level.tasks} 个）**不计入此口径**。"
            )
        # 饱和度：Wilson 下界 > 95% → 已经没有改进信号，只能追回归
        if pow_rate.point >= 1.0 and (pow_rate.low or 0) > 0.95:
            lines.append("")
            lines.append("> 🟡 **已饱和**：pass^k 的 Wilson 下界已超 95% —— "
                         "**100% 的 eval 只能追踪回归，给不出改进信号**（审计 §4.9）。"
                         "要恢复区分度必须加任务难度或换更难的任务集。")
        if task_level.unstable:
            lines.append("")
            lines.append(f"不稳定任务（部分成功）：{', '.join(task_level.unstable)}")
            capability_unstable = [
                name for name in task_level.unstable
                if purposes.get(_uid_by_key(name.split("(")[0], purposes)) == "capability"
            ]
            if capability_unstable:
                lines.append(f"> 其中 **capability 任务**：{', '.join(capability_unstable)} —— "
                             "它们的波动是**能力边界的发现**，不是回归噪声。")
        lines.append("")
        if task_level.unstable:
            lines.append("")
            lines.append(f"不稳定任务（部分成功）：{', '.join(task_level.unstable)}")
        lines.append("")
        lines.append("> **两个口径读法不同**：运行级看「每次尝试的期望」，"
                     "`pass^k` 看「**同一个任务能不能每次都做对**」（生产的 SLA 语义）。"
                     "当少数任务不稳定时，后者会把它暴露出来，前者会把它平摊掉。")
        lines.append("> ⚠️ `pass^k` **不是显著性的提升**：两者的区间可能都重叠；"
                     "它的样本量是**任务数**，想收窄区间必须加任务。")
        for caveat in task_level.caveats:
            lines.append(f"> - {caveat}")
    lines.append("")
    lines.append("| 指标 | 均值 | 95% 区间 | n |")
    lines.append("|---|---|---|---|")
    lines.append(_interval_row("tokens/任务", suite.tokens, digits=0, clamp_min=0))
    lines.append(_interval_row("成本/任务", suite.cost, unit=" 美元", digits=4, clamp_min=0))
    lines.append(_interval_row("耗时/任务", suite.elapsed, unit=" ms", digits=0, clamp_min=0))
    lines.append(_interval_row("工具调用/任务", suite.tool_calls, digits=2, clamp_min=0))
    lines.append("")
    lines.append("> 区间含义：**同样条件下重复这个评测，均值有 95% 的概率落在区间内**。")
    lines.append("> 两个方案的区间重叠时，不要宣称其中一个更好（n 不够，见第四节）。")
    lines.append("> 带 ⚠️ 的区间比点估计本身还宽 —— 说明 **n 太小，这个数字现在没有信息量**。")
    lines.append("")

    # ---- 交叉校验：SUT 自述 vs 独立判定 ----
    lines.append("### SUT 自述 vs 独立判定")
    lines.append("")
    if not suite.self_report_available:
        lines.append("本次评测未记录 SUT 自述字段（旧数据，字段是后来加的）→ 无法做交叉校验。")
        lines.append("")
    else:
        gap = suite.self_report_gap
        lines.append("| 口径 | 成功数 | 成功率 |")
        lines.append("|---|---|---|")
        lines.append(f"| SUT 自述 `result.ok` | {suite.self_reported_successes}/{suite.total_runs} "
                     f"| {suite.self_reported_successes / max(1, suite.total_runs):.1%} |")
        lines.append(f"| **独立判定（权威）** | **{suite.successes}/{suite.total_runs}** "
                     f"| **{rate.point:.1%}** |")
        lines.append("")
        if gap:
            lines.append(f"⚠️ **{gap} 次运行是「自报成功但实际失败」**"
                         f"（占 {gap / max(1, suite.total_runs):.0%}）。"
                         f"这类虚报是最危险的：如果只看 SUT 自述，成功率会被直接抬高。")
            lines.append("")
            lines.append("> 实测案例：某任务 SUT 规划出 **0 个步骤、一个工具都没调**，"
                         "仍然返回 `ok=True`（`error=None`）。"
                         "独立判定则发现产物根本不存在。")
        else:
            lines.append("本次没有出现虚报成功。")

        false_negative = suite.false_negative_runs
        if false_negative:
            lines.append("")
            lines.append(f"⚠️ **{false_negative} 次运行是「自报失败但产物合格」**"
                         f"（占 {false_negative / max(1, suite.total_runs):.0%}）"
                         f"—— 与虚报相反的方向，它会让自述**低估**成功率。")
            lines.append("")
            lines.append("> 实测的三个典型原因：活干完了但**汇总阶段的 LLM 调用失败**、"
                         "活干完了但 Agent **又去问用户问题**（headless 下无法继续）、"
                         "活干完了但**整任务超时**被砍。")
            lines.append("> 三者共同说明：「任务完成」应当看**交付物**，"
                         "而不是看 Agent 有没有好好收尾。")
        lines.append("")

    # ==================== 分组 ====================
    # ---- 交错 A/B：必须**先**拆开看，混在一起的成功率没有任何意义 ----
    if suite.is_multi_arm:
        lines.append("## 一·五、分臂结果（交错 A/B）")
        lines.append("")
        lines.append("| 臂 | 加固 | 故障 | n | 成功 | 成功率 | 95% Wilson |")
        lines.append("|---|---|---|---|---|---|---|")
        for arm_label in suite.arm_labels:
            sub = suite.for_arm(arm_label)
            rate = sub.success_rate
            meta = next((a for a in suite.arms if a.get("label") == arm_label), {})
            lines.append(
                f"| `{arm_label}` | `{meta.get('guard_label', '')}` "
                f"| `{meta.get('fault_spec', '') or '无'}` "
                f"| {sub.total_runs} | {sub.successes} | {rate.point:.1%} "
                f"| [{rate.low:.1%}, {rate.high:.1%}] |"
            )
        lines.append("")
        lines.append("| 臂 | regression pass^k | 成本/任务 | 成本/成功 |")
        lines.append("|---|---|---|---|")
        for arm_label in suite.arm_labels:
            sub = suite.for_arm(arm_label)
            reg = sub.task_level(only_uids=regression_uids) if regression_uids else None
            pow_text = (f"{reg.pass_pow_k}/{reg.tasks} = {reg.pass_pow_k_rate.point:.0%}"
                        if reg and reg.tasks else "—")
            cost = sub.cost.point
            cps = (cost * sub.total_runs / sub.successes) if sub.successes else float("inf")
            lines.append(f"| `{arm_label}` | {pow_text} | ${cost:.4f} "
                         f"| {'∞' if cps == float('inf') else f'${cps:.4f}'} |")
        lines.append("")
        lines.append("> 两臂来自**同一个交错 suite**（A,B,B,A…），所以时间漂移对两边同权 ——"
                     "这比分两段跑强。但两臂**不是独立样本**，配对区间要按配对设计读。")
        lines.append(
            "> 正式对比：`python -m lab bench compare "
            + suite.suite_id[:8]
            + " --arm \"" + suite.arm_labels[0] + "\" --arm \"" + suite.arm_labels[1] + "\"`"
        )
        lines.append("")

    lines.append("## 二、分组对比")
    lines.append("")
    lines.append("| 分组 | 运行数 | 成功率 | 95% 区间 | tokens/任务 | 耗时/任务 |")
    lines.append("|---|---|---|---|---|---|")
    for group, data in sorted(suite.by_group().items()):
        interval = data["success_rate"]
        lines.append(
            f"| {group} | {data['runs']} | {interval.point:.1%} "
            f"| [{interval.low:.1%}, {interval.high:.1%}] "
            f"| {data['tokens'].point:,.0f} | {data['elapsed'].point:,.0f} ms |"
        )
    lines.append("")
    lines.append("> `synthetic` 是纯合成任务（完全可控），`semireal` 是半真实小场景"
                 "（CSV 清洗 / 日志分析 / 改代码等）。两者成功率差多少，"
                 "可以直接回答\"这套系统在玩具任务上刷分、在真实任务上崩掉\"这类质疑。")
    lines.append("")

    # ==================== 逐任务 ====================
    lines.append("## 三、逐任务明细")
    lines.append("")
    lines.append("| 任务 | 分组 | 成功 | 成功率 | tokens | 耗时 | 工具 | 过程 flag |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for summary in sorted(suite.task_summaries, key=lambda s: (s.group, s.task_uid)):
        mark = "✅" if summary.successes == summary.runs else ("⚠️" if summary.successes else "❌")
        flags = ",".join(f"{k}×{v}" for k, v in summary.flag_counts.items()) or ""
        lines.append(
            f"| `{summary.task_key}` | {summary.group} | {mark} {summary.successes}/{summary.runs} "
            f"| {summary.success_rate.point:.0%} | {summary.tokens.point:,.0f} "
            f"| {summary.elapsed_ms.point:,.0f} ms | {summary.tool_calls.point:.1f} | {flags} |"
        )
    lines.append("")

    # ==================== 不稳定任务 ====================
    unstable = [s for s in suite.task_summaries if s.unstable]
    lines.append("## 四、不稳定任务（同一任务多次运行结果不一致）")
    lines.append("")
    if unstable:
        lines.append("这些任务最能说明系统的方差 —— **它们才是加固工作的靶子**：")
        lines.append("")
        for summary in unstable:
            lines.append(f"- `{summary.task_uid}`：{summary.successes}/{summary.runs} 成功 "
                         f"（{summary.title}）")
    else:
        lines.append("本次没有出现\"同一任务部分成功部分失败\"的情况。")
        lines.append("")
        lines.append("> 但这**不等于系统稳定**：如果 n 很小，不稳定任务很可能只是没被抽到。")
    lines.append("")

    # ==================== 失败归因 ====================
    lines.append("## 五、失败归因")
    lines.append("")
    attribution = suite.error_attribution()
    if attribution:
        lines.append("| 原因 | 次数 |")
        lines.append("|---|---|")
        for key, count in attribution.items():
            lines.append(f"| `{key}` | {count} |")
        lines.append("")
        lines.append("> `verification_failed` 表示 Agent 自己认为成功、但判定器判定失败 ——"
                     "这类是**最值得看的失败**（要区分是能力问题还是验证器问题）。")
        lines.append("> `tool:*` 前缀是工具层出错次数（可能不影响最终成败）。")
    else:
        lines.append("本次没有失败。")
    lines.append("")

    # ==================== 过程问题 ====================
    lines.append("## 六、过程问题（答案对但路径可疑）")
    lines.append("")
    flags = suite.process_flag_counts()
    if flags:
        lines.append("| 问题 | 出现次数 |")
        lines.append("|---|---|")
        for key, count in flags.items():
            lines.append(f"| `{key}` | {count} |")
        lines.append("")
        lines.append("> 过程问题**不影响成功率**（那是结果分），但会影响可维护性与安全性：")
        lines.append("> 工具调用超标 = 效率差；动作多样性低 = 在原地打转；")
        lines.append("> `hardcoded_answer` = 抄答案；`path_escape_attempted` = 越权尝试。")
    else:
        lines.append("本次没有检测到过程问题。")
    lines.append("")

    # ==================== 预算观察 ====================
    lines.append("## 七、预算观察（observe 模式的产出）")
    lines.append("")
    stats = suite.budget_stats()
    lines.append(f"- 越界运行数：**{stats['violated_runs']}/{stats['runs']}**"
                 f"（{stats['violated_rate']:.1%}）")
    lines.append(f"- enforce 模式下\"本会中止\"的运行数：**{stats['would_stop_runs']}**")
    if stats["by_metric"]:
        lines.append(f"- 按维度：{stats['by_metric']}")
    lines.append("")
    lines.append("> 这一节是**切换模式前的决策依据**：")
    lines.append("> 若\"本会中止\"的比例很小 → 硬上限设得合适，可以放心切 enforce；")
    lines.append("> 若很大 → 硬上限太紧，切过去会砍掉大量本来能成功的任务。")
    lines.append("")

    # ==================== 已知限制 ====================
    lines.append("## 八、已知限制（请连同上面的数字一起读）")
    lines.append("")
    for note in suite.notes:
        lines.append(f"- {note}")
    if suite.replay_mode and suite.replay_mode != "off":
        lines.append(
            f"- ⚠️ **本次是 LLM 回放（`{suite.replay_mode}`）**：`成本` / `耗时` / `token` "
            "都是**等价量**（录制那些请求当时的值），不是本次真实消费。"
            "回放适合验证 harness/判定器/加固行为；**不能**用来测性能或成本。"
        )
        missed = suite.replay_missed_runs
        if missed:
            lines.append(
                f"- 🔴 **{missed}/{suite.total_runs} 次运行的回放不完整**：这些运行在 LLM 调用"
                "失败后**降级继续**了，所以「仍然成功」**不等于**「复现成功」——"
                "它们的轨迹不是录制时那条，**不能用于对比**。"
                "处置：加 `--replay-ignore-volatile`，或补录缺失的请求。"
            )
        else:
            lines.append(
                "- ✅ **回放是完整的**（0 次缓存未命中）：本次运行与录制时的请求集合逐条一致。"
            )
    if suite.fixture_failures:
        lines.append(f"- ⚠️ 有 {len(suite.fixture_failures)} 次运行在执行前就失败了，"
                     f"**未计入成功率**（否则会把环境问题算成 Agent 能力问题）")
    lines.append("- 耗时指标包含模型服务端排队时间，跨时段对比需要谨慎。")
    lines.append("- 判定器只看最终产物，**不评价过程质量**（过程分在第六节单独给）。")
    lines.append("- 任务集从 20 个任务中抽取，覆盖面有限；结论外推到其它任务类型需要谨慎。")
    lines.append("")

    # ==================== 复现 ====================
    lines.append("## 九、复现方式")
    lines.append("")
    lines.append("```bash")
    lines.append("# 1. 先验证任务集本身可信（不需要 API Key，不花钱）")
    lines.append("python -m lab bench validate")
    lines.append("")
    lines.append("# 2. 复现本次评测（阈值/故障注入/加固/交错都来自命令行，不藏在代码里）")
    replay_flag = "" if suite.replay_mode in ("", "off") else f" --replay {suite.replay_mode}"
    if suite.interleaved:
        arm_flags = "".join(
            f' --ab-guard "{arm.get("guard_spec") or "all"}" --ab-label "{arm.get("label")}"'
            for arm in suite.arms
        )
        lines.append(f"python -m lab bench run --runs {suite.runs_per_task} "
                     f"--label \"{suite.label}\"{arm_flags}{replay_flag}")
    else:
        lines.append(f"python -m lab bench run --runs {suite.runs_per_task} "
                     f"--guard {suite.guard_config or 'all'} "
                     f"--label \"{suite.label}\"{replay_flag}")
    lines.append("")
    lines.append("# 3. 重新出报告（不重跑）")
    lines.append(f"python -m lab bench report {suite.suite_id}")
    lines.append("")
    lines.append("# 4. 解读任何 delta 之前：先确认噪声地板（<20% 的差异未测地板时不可解释）")
    lines.append("python -m lab bench noise-floor --runs 3 --group semireal")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def recipe_n(interval: Interval) -> int:
    """小工具：把 interval 的 n 取出来（用于比率那一行的文案）。"""
    return interval.n


def render_suite_list(suites: List[Dict[str, Any]]) -> str:
    """列出历史评测。"""
    if not suites:
        return "(还没有任何评测记录)"
    lines = ["| suite_id | label | 模型 | n/任务 | 运行数 | 成功率 | 开始时间 |",
             "|---|---|---|---|---|---|---|"]
    for row in suites:
        total = row["total_runs"] or 0
        successes = row["successes"] or 0
        rate = (successes / total) if total else 0.0
        lines.append(
            f"| `{row['suite_id'][:8]}` | {row['label']} | {row['model_name']} "
            f"| {row['runs_per_task']} | {total} | {rate:.1%} | {row['started_at']} |"
        )
    return "\n".join(lines)
