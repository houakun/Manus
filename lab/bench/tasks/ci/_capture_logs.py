#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CI 归因任务（`lab/bench/tasks/ci/*.yaml`）的**日志捕获 + YAML 生成**工具。

== 为什么需要它（这条是踩坑换来的）==
`validate` 有一条 **fixture 真实性** 规则：它真的执行 `selfcheck.command`，
再拿**实际输出**去比对 `output_recorded_in` 里那份日志 ——
手编的日志会被立刻抓住（报"说明那份日志是编的，不是跑出来的"）。

所以每加一个 ci 任务都必须：**先跑一遍 buggy 代码、把真实输出写进 ci_log.txt**。
手工做这件事既麻烦又容易抄错，于是做成脚本：

    python lab/bench/tasks/ci/_capture_logs.py            # 生成/覆盖全部 ci 任务的 YAML

== 每个任务的结构（照 ci/01、ci/02 的形状）==
1. `fixtures`：`ci_log.txt`（真跑出来的失败输出）+ 仓库代码（含 bug）+ `tests/run_tests.py`（CI-FROZEN）
2. `selfcheck`：跑测试、期望退出码 1、日志与真实输出逐行一致
3. `verify`：`exec_script`（任务定义里的一段脚本，Agent 改不了）真的跑一遍修完的代码；
   外加 `file_content` 检查 CI-FROZEN 标记与归因内容
4. `solution` / `wrong`：正确修复 / **症状级**错解（只在调用点打补丁，必须被拒绝）
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import textwrap

# 仓库根：本文件在 lab/bench/tasks/ci/ 下，所以要往上 4 级（ci→tasks→bench→lab→仓库根）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

from lab.infra.local_sandbox import LocalSandbox  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def block(text: str, indent: int = 4) -> str:
    """把多行文本转成 YAML 块标量（保持文件可读，和既有 ci 任务一致）。"""
    pad = " " * indent
    lines = textwrap.dedent(text).strip("\n").splitlines()
    return "\n".join(pad + line if line.strip() else "" for line in lines)


SCENARIOS = [
    {
        "key": "ci_none_in_total",
        "title": "CI 归因：合计把 None 当数字（一处聚合，两个调用方）",
        "tags": "[ci-attribution, code-fix, aggregation, exec]",
        "bug_file": "stats.py",
        "symbol": "total",
        "why": "sum() 直接吃进 None 元素会抛 TypeError；metrics 里缺失值是合法数据，应当跳过而不是让整个报表崩掉",
        "goal_note": "注意：metrics 里**允许**有 None（表示缺失），上游不保证全是数字。",
        "modules": {
            "repo/stats.py": '''
                """报表用的聚合工具。"""


                def total(values):
                    """把一组数字加起来。缺失值（None）应当被跳过。"""
                    # BUG: 直接把 values 交给 sum —— 只要有一个 None 就抛
                    # TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'。
                    # 注意它**不是每次都炸**：全数字的那条用例会通过。
                    return sum(values)
            ''',
            "repo/report.py": '''
                """日报：把两个来源的指标合并成一行汇总。"""
                from stats import total


                def daily_sum(counters, gauges):
                    """日报的两个来源都必须计入汇总。"""
                    return total(list(counters) + list(gauges))
            ''',
            "repo/dashboard.py": '''
                """看板：逐段汇总，缺值那段要能显示。"""
                from stats import total


                def section_sum(rows):
                    """看板的分段汇总（rows 里可能有缺失值）。"""
                    return total(rows)
            ''',
        },
        "cases": [
            ("test_all_numbers_are_summed", "report.daily_sum([1, 2], [3])", "[1, 2] + [3]", 6),
            ("test_missing_value_is_skipped", "report.daily_sum([1, None], [3])", "[1, None] + [3]", 4),
            ("test_section_with_missing_value", "dashboard.section_sum([5, None, None])", "[5, None, None]", 5),
            ("test_empty_input_is_zero", "report.daily_sum([], [])", "[] + []", 0),
        ],
        "fixed": {
            "repo/stats.py": '''
                """报表用的聚合工具。"""


                def total(values):
                    """把一组数字加起来。缺失值（None）被跳过。"""
                    return sum(value for value in values if value is not None)
            ''',
        },
        "wrong": {
            "repo/report.py": '''
                """日报：把两个来源的指标合并成一行汇总。"""
                from stats import total


                def daily_sum(counters, gauges):
                    """日报的两个来源都必须计入汇总。"""
                    # 症状级修复：在这里把 None 过滤掉，共享的 total() 仍然是坏的
                    merged = [value for value in list(counters) + list(gauges) if value is not None]
                    return total(merged)
            ''',
        },
        "check": """
            from dashboard import section_sum
            from report import daily_sum

            problems = []
            if daily_sum([1, 2], [3]) != 6:
                problems.append(f"daily_sum(数字)={daily_sum([1, 2], [3])!r}")
            if daily_sum([1, None], [3]) != 4:
                problems.append(f"daily_sum(含 None)={daily_sum([1, None], [3])!r}")
            if section_sum([5, None, None]) != 5:
                problems.append(f"section_sum(含 None)={section_sum([5, None, None])!r}")
            if section_sum([]) != 0:
                problems.append(f"section_sum(空)={section_sum([])!r}")
            if daily_sum([None], [None]) != 0:
                problems.append(f"daily_sum(全 None)={daily_sum([None], [None])!r}")
            if problems:
                print("FAIL:", problems)
                raise SystemExit(1)
            print("OK: 缺失值被跳过，两个调用方都正确")
        """,
        "extra_edge": "daily_sum([None], [None])",
    },
    {
        "key": "ci_range_boundary",
        "title": "CI 归因：编号区间漏掉上界（range 少一）",
        "tags": "[ci-attribution, code-fix, boundary, exec]",
        "bug_file": "ids.py",
        "symbol": "id_range",
        "why": "range(1, last) 不含 last：调用方以为包含上界，于是最后一个编号永远发不出去",
        "goal_note": "注意：接口文档写的是**闭区间**（含首尾）。",
        "modules": {
            "repo/ids.py": '''
                """编号区间生成。"""


                def id_range(first, last):
                    """生成 [first, last] 的编号（**闭区间**）。"""
                    # BUG: range 的上界是开区间，漏掉了 last。
                    # 注意它不是每次都错：first==last 时两边都只差一个，
                    # 而调用方 batch() 恰好会过滤空结果，所以"单个编号"那条能过。
                    return list(range(first, last))
            ''',
            "repo/batch.py": '''
                """发号器。"""
                from ids import id_range


                def issue(first, last):
                    """发一批编号（含首尾）。"""
                    return id_range(first, last)
            ''',
            "repo/audit.py": '''
                """审计：把整段编号标记为已用。"""
                from ids import id_range


                def mark_used(first, last):
                    """标记 [first, last] 全部已用。"""
                    return len(id_range(first, last))
            ''',
        },
        "cases": [
            ("test_single_id_is_issued", "batch.issue(7, 7)", "issue(7, 7)", [7]),
            ("test_two_ids_are_issued", "batch.issue(3, 4)", "issue(3, 4)", [3, 4]),
            ("test_full_block_is_issued", "batch.issue(10, 13)", "issue(10, 13)", [10, 11, 12, 13]),
            ("test_audit_counts_inclusive", "audit.mark_used(1, 5)", "mark_used(1, 5)", 5),
        ],
        "fixed": {
            "repo/ids.py": '''
                """编号区间生成。"""


                def id_range(first, last):
                    """生成 [first, last] 的编号（闭区间）。"""
                    return list(range(first, last + 1))
            ''',
        },
        "wrong": {
            "repo/batch.py": '''
                """发号器。"""
                from ids import id_range


                def issue(first, last):
                    """发一批编号（含首尾）。"""
                    # 症状级修复：只在发号器里把 last 补回来，共享的 id_range 仍然漏上界
                    result = id_range(first, last)
                    if last not in result:
                        result.append(last)
                    return result
            ''',
        },
        "check": """
            from audit import mark_used
            from batch import issue

            problems = []
            for first, last, expected in ((7, 7, [7]), (3, 4, [3, 4]), (10, 13, [10, 11, 12, 13])):
                actual = issue(first, last)
                if actual != expected:
                    problems.append(f"issue({first}, {last})={actual!r}")
            if mark_used(1, 5) != 5:
                problems.append(f"mark_used(1, 5)={mark_used(1, 5)!r}")
            if mark_used(4, 4) != 1:
                problems.append(f"mark_used(4, 4)={mark_used(4, 4)!r}")
            if problems:
                print("FAIL:", problems)
                raise SystemExit(1)
            print("OK: 闭区间在两个调用方都正确")
        """,
        "extra_edge": "mark_used(4, 4)",
    },
    {
        "key": "ci_float_settled",
        "title": "CI 归因：浮点等值判断（0.1+0.2-0.3 不等于 0）",
        "tags": "[ci-attribution, code-fix, float, exec]",
        "bug_file": "money.py",
        "symbol": "is_settled",
        "why": "浮点寄存器误差让 0.1+0.2-0.3 != 0，等值比较永远判不出'已结清'；应当按容差比较（或换算成分）",
        "goal_note": "注意：金额来自多步加减，**不要**假设二进制里能精确表示。",
        "modules": {
            "repo/money.py": '''
                """金额工具。"""


                def is_settled(balance):
                    """余额是否为 0（已结清）。"""
                    # BUG: 浮点等值比较。0.1 + 0.2 - 0.3 在二进制里不等于 0，
                    # 于是"理论上已结清"的账永远结不了。
                    # 注意它不是每次都错：整数金额那条会通过。
                    return balance == 0
            ''',
            "repo/ledger.py": '''
                """账单。"""
                from money import is_settled


                def account_status(balance):
                    """账户状态。"""
                    return "settled" if is_settled(balance) else "open"
            ''',
            "repo/reconcile.py": '''
                """对账。"""
                from money import is_settled


                def rows_to_close(rows):
                    """统计已结清的笔数。"""
                    return sum(1 for row in rows if is_settled(row))
            ''',
        },
        "cases": [
            ("test_integer_zero_is_settled", "ledger.account_status(0)", "account_status(0)", "settled"),
            (
                "test_float_residue_is_settled",
                "ledger.account_status(0.1 + 0.2 - 0.3)",
                "account_status(0.1+0.2-0.3)",
                "settled",
            ),
            ("test_small_residue_is_settled", "ledger.account_status(1e-12)", "account_status(1e-12)", "settled"),
            (
                "test_reconcile_counts_residue_rows",
                "reconcile.rows_to_close([0, 0.1 + 0.2 - 0.3])",
                "rows_to_close([0, residue])",
                2,
            ),
            ("test_nonzero_is_open", "ledger.account_status(0.5)", "account_status(0.5)", "open"),
        ],
        "fixed": {
            "repo/money.py": '''
                """金额工具。"""

                # 容差取 1e-9：比金额的最小单位（分）小 7 个量级，
                # 既能吃掉二进制误差，又不会把真正未结清的账判成已结清。
                TOLERANCE = 1e-9


                def is_settled(balance):
                    """余额是否为 0（已结清，按容差比较）。"""
                    return abs(balance) < TOLERANCE
            ''',
        },
        "wrong": {
            "repo/ledger.py": '''
                """账单。"""
                from money import is_settled


                def account_status(balance):
                    """账户状态。"""
                    # 症状级修复：只在账单这一处四舍五入到分，共享的 is_settled 仍然用等值比较
                    return "settled" if is_settled(round(balance, 2)) else "open"
            ''',
        },
        "check": """
            from ledger import account_status
            from reconcile import rows_to_close

            problems = []
            if account_status(0) != "settled":
                problems.append(f"account_status(0)={account_status(0)!r}")
            if account_status(0.1 + 0.2 - 0.3) != "settled":
                problems.append(f"account_status(残差)={account_status(0.1 + 0.2 - 0.3)!r}")
            if account_status(1e-12) != "settled":
                problems.append(f"account_status(1e-12)={account_status(1e-12)!r}")
            if account_status(0.5) != "open":
                problems.append(f"account_status(0.5)={account_status(0.5)!r}")
            if rows_to_close([0, 0.1 + 0.2 - 0.3, 0.5]) != 2:
                problems.append(f"rows_to_close={rows_to_close([0, 0.1 + 0.2 - 0.3, 0.5])!r}")
            if problems:
                print("FAIL:", problems)
                raise SystemExit(1)
            print("OK: 浮点按容差比较，两个调用方都正确")
        """,
        "extra_edge": "account_status(0.5) 仍须是 open",
    },
    {
        "key": "ci_naive_datetime",
        "title": "CI 归因：naive 与 aware 时间混比（TypeError）",
        "tags": "[ci-attribution, code-fix, datetime, exec]",
        "bug_file": "sla.py",
        "symbol": "hours_left",
        "why": "naive datetime 与 aware datetime 相减直接抛 TypeError；应当统一到同一时区再比较",
        "goal_note": "注意：外部传入的截止时间是 **带时区** 的（UTC），而内部现在产出的是 naive 时间。",
        # 用例表达式里用到了这几个名字 —— runner 必须先把它们导入，
        # 否则日志里出现的会是 "NameError: name 'datetime' is not defined"，
        # 那种"题目自己坏了"的日志会把 Agent 引到错方向。
        "runner_prelude": "from datetime import datetime, timedelta, timezone",
        "modules": {
            "repo/sla.py": '''
                """SLA 计算。"""
                from datetime import datetime


                def hours_left(deadline):
                    """距离截止还有多少小时（deadline 是 **aware**，UTC）。"""
                    # BUG: datetime.now() 是 naive，aware - naive 会抛
                    # TypeError: can't subtract offset-naive and offset-aware datetimes。
                    # 注意：传入 naive 截止时间的那条用例会"通过"（两个都 naive）。
                    now = datetime.now()
                    return (deadline - now).total_seconds() / 3600
            ''',
            "repo/tickets.py": '''
                """工单。"""
                from sla import hours_left


                def priority(deadline):
                    """按剩余时间定优先级。"""
                    return "urgent" if hours_left(deadline) < 24 else "normal"
            ''',
            "repo/alerts.py": '''
                """告警。"""
                from sla import hours_left


                def expiring(tickets):
                    """统计即将到期的数量。"""
                    return sum(1 for deadline in tickets if hours_left(deadline) < 24)
            ''',
        },
        "cases": [
            # ⚠️ 用例表达式必须得出**布尔**（生成器只做 `actual == expected`）。
            # ⚠️ naive 截止时间要用 **naive 形式的 UTC 时刻**构造：
            #   `datetime.now()` 是**本地时间**（本机 UTC+8），而修复把 naive 一律当 UTC 解释，
            #   于是"本地 now +48h"会被算成 56h —— 那是正确地反映了时区偏移，不是 bug。
            #   实测就踩过这一下：`hours_left(naive)=56.0` 把参考解判成了失败。
            (
                "test_naive_deadline_is_handled",
                "47 <= sla.hours_left(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=48)) <= 49",
                "hours_left(naive +48h)",
                True,
            ),
            (
                "test_aware_deadline_is_handled",
                "47 <= sla.hours_left(datetime.now(timezone.utc) + timedelta(hours=48)) <= 49",
                "hours_left(aware +48h)",
                True,
            ),
            (
                "test_priority_with_aware_deadline",
                "tickets.priority(datetime.now(timezone.utc) + timedelta(hours=2)) == 'urgent'",
                "priority(aware +2h)",
                True,
            ),
            (
                "test_alerts_counts_aware_deadlines",
                "alerts.expiring([datetime.now(timezone.utc) + timedelta(hours=1)]) == 1",
                "expiring([aware +1h])",
                True,
            ),
        ],
        "fixed": {
            "repo/sla.py": '''
                """SLA 计算。"""
                from datetime import datetime, timezone


                def _as_aware(moment):
                    """统一成 aware（UTC）：naive 一律按 UTC 解释。"""
                    if moment.tzinfo is None:
                        return moment.replace(tzinfo=timezone.utc)
                    return moment


                def hours_left(deadline):
                    """距离截止还有多少小时。"""
                    # 两边都归一到 aware 再相减：naive 与 aware 混比会直接抛异常
                    return (_as_aware(deadline) - datetime.now(timezone.utc)).total_seconds() / 3600
            ''',
        },
        "wrong": {
            "repo/tickets.py": '''
                """工单。"""
                from sla import hours_left


                def priority(deadline):
                    """按剩余时间定优先级。"""
                    # 症状级修复：在这里给 deadline 去掉时区（只在调用点绕过），
                    # 共享的 hours_left 仍然会在别的调用方炸
                    return "urgent" if hours_left(deadline.replace(tzinfo=None)) < 24 else "normal"
            ''',
        },
        "check": """
            from datetime import datetime, timedelta, timezone

            from alerts import expiring
            from sla import hours_left
            from tickets import priority

            problems = []
            for label, deadline in (("naive", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=48)),
                                    ("aware", datetime.now(timezone.utc) + timedelta(hours=48))):
                try:
                    value = hours_left(deadline)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"hours_left({label}) 抛 {type(exc).__name__}")
                    continue
                if not 47 <= value <= 49:
                    problems.append(f"hours_left({label})={value!r}")
            if priority(datetime.now(timezone.utc) + timedelta(hours=2)) != "urgent":
                problems.append("priority(aware +2h) 不是 urgent")
            if expiring([datetime.now(timezone.utc) + timedelta(hours=1)]) != 1:
                problems.append("expiring 计数错误")
            if problems:
                print("FAIL:", problems)
                raise SystemExit(1)
            print("OK: naive/aware 都能算出剩余小时，两个调用方都正确")
        """,
        "extra_edge": "expiring([aware +1h]) 必须能算",
    },
    {
        "key": "ci_identity_cache",
        "title": "CI 归因：用 is 比较整数（小整数碰巧对，大整数不对）",
        "tags": "[ci-attribution, code-fix, identity, exec]",
        "bug_file": "cache.py",
        "symbol": "key_hit",
        "why": "小整数有驻留缓存（-5..256）所以 is 恰好为真，大整数不驻留 → 同一个值两个对象，命中判定随机失效",
        "goal_note": "注意：键来自 **反序列化** 的结果，不保证是同一个对象。",
        "modules": {
            "repo/cache.py": '''
                """缓存键比较。"""


                def key_hit(stored, incoming):
                    """两个键是否指向同一条缓存。"""
                    # BUG: 用 is 比较整数。CPython 只对 -5..256 的小整数做驻留，
                    # 所以小键"恰好"能命中、大键（来自反序列化，是新对象）不命中。
                    # 注意它不是每次都错：用例里的 7 和 100 会通过。
                    return stored is incoming
            ''',
            "repo/store.py": '''
                """本地缓存。"""
                import json

                from cache import key_hit


                def lookup(stored_key, payload):
                    """从序列化 payload 里恢复键并比较。"""
                    incoming = json.loads(payload)["key"]
                    return key_hit(stored_key, incoming)
            ''',
            "repo/evict.py": '''
                """淘汰。"""
                from cache import key_hit


                def count_alive(keys, payload_keys):
                    """统计仍然存活的键。"""
                    return sum(1 for a, b in zip(keys, payload_keys) if key_hit(a, b))
            ''',
        },
        "cases": [
            ("test_small_key_hits", "cache.key_hit(7, 7)", "key_hit(7, 7)", True),
            ("test_small_key_from_json_hits", "store.lookup(100, '{\"key\": 100}')", "lookup(100, json)", True),
            ("test_large_key_from_json_hits", "store.lookup(70000, '{\"key\": 70000}')", "lookup(70000, json)", True),
            (
                "test_two_large_keys_are_evicted",
                "evict.count_alive([70000], [70001])",
                "count_alive([70000], [70001])",
                0,
            ),
        ],
        "fixed": {
            "repo/cache.py": '''
                """缓存键比较。"""


                def key_hit(stored, incoming):
                    """两个键是否指向同一条缓存（按**值**比较）。"""
                    # 键可能是反序列化出来的新对象，identity 不成立 —— 必须比 value
                    return stored == incoming
            ''',
        },
        "wrong": {
            "repo/store.py": '''
                """本地缓存。"""
                import json

                from cache import key_hit


                def lookup(stored_key, payload):
                    """从序列化 payload 里恢复键并比较。"""
                    incoming = json.loads(payload)["key"]
                    # 症状级修复：在这里把小/大整数都转成字符串再比（只在 store 绕过），
                    # 共享的 key_hit 仍然用 is
                    return key_hit(str(stored_key), str(incoming))
            ''',
        },
        "check": """
            from cache import key_hit
            from evict import count_alive
            from store import lookup

            problems = []
            if key_hit(7, 7) is not True:
                problems.append("key_hit(7, 7) 不是 True")
            if lookup(100, '{"key": 100}') is not True:
                problems.append("lookup(100) 不是 True")
            if lookup(70000, '{"key": 70000}') is not True:
                problems.append("lookup(70000) 不是 True（大整数不驻留）")
            if lookup(70000, '{"key": 70001}') is not False:
                problems.append("lookup(70001) 应为 False")
            if count_alive([70000, 70001], [70000, 70001]) != 2:
                problems.append(f"count_alive={count_alive([70000, 70001], [70000, 70001])!r}")
            if problems:
                print("FAIL:", problems)
                raise SystemExit(1)
            print("OK: 按值比较，大整数也能命中")
        """,
        "extra_edge": "lookup(70000, ...) 与 lookup(70001, ...) 必须区分",
    },
    {
        "key": "ci_regex_escape",
        "title": "CI 归因：正则未转义（含元字符的字面量匹配错）",
        "tags": "[ci-attribution, code-fix, regex, exec]",
        "bug_file": "search.py",
        "symbol": "contains_literal",
        "why": "用户输入被当成正则模式：`a.b` 会匹配 `axb`，`[` 直接抛 re.error；字面量查找必须 re.escape",
        "goal_note": "注意：needle 是 **用户输入的字面量**，不是正则。",
        "modules": {
            "repo/search.py": '''
                """字面量查找（把用户输入当**字面量**，不是正则）。"""
                import re


                def contains_literal(text, needle):
                    """text 里是否包含 needle 这个**字面量**。"""
                    # BUG: 直接把用户输入当正则模式。
                    # `a.b` 会匹配到 `axb`（多匹配了），`[` 会直接抛 re.error（少匹配/报错）。
                    # 注意它不是每次都错：纯字母数字的 needle 会通过。
                    return re.search(needle, text) is not None
            ''',
            "repo/logfilter.py": '''
                """日志过滤（模块名不叫 filter：避免遮蔽内置函数）。"""
                from search import contains_literal


                def pick(lines, needle):
                    """挑出包含该字面量的行。"""
                    return [line for line in lines if contains_literal(line, needle)]
            ''',
            "repo/audit_log.py": '''
                """审计。"""
                from search import contains_literal


                def count_hits(entries, needle):
                    """统计命中条数。"""
                    return sum(1 for entry in entries if contains_literal(entry, needle))
            ''',
        },
        "cases": [
            (
                "test_plain_needle_matches",
                "search.contains_literal('hello world', 'world')",
                "contains_literal('hello world', 'world')",
                True,
            ),
            ("test_dot_is_literal", "search.contains_literal('a.b', 'a.b')", "contains_literal('a.b', 'a.b')", True),
            (
                "test_dot_does_not_match_other_char",
                "search.contains_literal('axb', 'a.b')",
                "contains_literal('axb', 'a.b')",
                False,
            ),
            (
                "test_bracket_needle_does_not_crash",
                "search.contains_literal('x[1]y', '[1]')",
                "contains_literal('x[1]y', '[1]')",
                True,
            ),
            (
                "test_filter_uses_literal",
                "logfilter.pick(['a.b', 'axb'], 'a.b')",
                "pick(['a.b', 'axb'], 'a.b')",
                ["a.b"],
            ),
        ],
        "fixed": {
            "repo/search.py": '''
                """字面量查找（把用户输入当字面量，不是正则）。"""
                import re


                def contains_literal(text, needle):
                    """text 里是否包含 needle 这个字面量。"""
                    # 用户输入里可能有 . [ ] ( ) * + ? 等元字符 —— 必须转义
                    return re.search(re.escape(needle), text) is not None
            ''',
        },
        "wrong": {
            "repo/logfilter.py": '''
                """日志过滤（模块名不叫 filter：避免遮蔽内置函数）。"""
                from search import contains_literal


                def pick(lines, needle):
                    """挑出包含该字面量的行。"""
                    # 症状级修复：只在 logfilter 里把元字符替换掉（只在调用点绕过），
                    # audit_log 与 search.contains_literal 本身仍然是错的
                    safe = needle.replace(".", chr(92) + ".")
                    return [line for line in lines if contains_literal(line, safe)]
            ''',
        },
        "check": """
            from audit_log import count_hits
            from logfilter import pick
            from search import contains_literal

            problems = []
            if contains_literal("hello world", "world") is not True:
                problems.append("普通字面量匹配失败")
            if contains_literal("a.b", "a.b") is not True:
                problems.append("包含点号的字面量匹配失败")
            if contains_literal("axb", "a.b") is not False:
                problems.append("点号被当成通配符（多匹配了）")
            try:
                if contains_literal("x[1]y", "[1]") is not True:
                    problems.append("含方括号的字面量匹配失败")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"含方括号的 needle 抛 {type(exc).__name__}")
            if pick(["a.b", "axb"], "a.b") != ["a.b"]:
                problems.append(f"pick={pick(['a.b', 'axb'], 'a.b')!r}")
            if count_hits(["a+b", "aab"], "a+b") != 1:
                problems.append(f"count_hits={count_hits(['a+b', 'aab'], 'a+b')!r}")
            if problems:
                print("FAIL:", problems)
                raise SystemExit(1)
            print("OK: 用户输入被当字面量处理，两个调用方都正确")
        """,
        "extra_edge": "count_hits(['a+b','aab'], 'a+b') 必须只命中 1 条",
    },
]


def render_yaml(scenario: dict, log_text: str) -> str:
    """按既有 ci 任务的形状渲染 YAML。"""
    check = textwrap.dedent(scenario["check"]).strip("\n")
    goal = (
        "  /home/ubuntu/ci_log.txt 是最近一次 CI 的失败输出。代码在 /home/ubuntu/repo/ 下，\n"
        "  测试在 /home/ubuntu/repo/tests/ 下（**测试文件由 CI 提供，不得修改、不得删除**）。\n"
        "  请定位失败的根本原因并修好代码，使 `python repo/tests/run_tests.py`\n"
        "  （工作目录 /home/ubuntu）退出码为 0；然后把归因写入 /home/ubuntu/diagnosis.json，\n"
        '  格式为 {"root_causes": [{"file": "...", "symbol": "...", "why": "..."}, ...]}，\n'
        "  file 用相对 /home/ubuntu/repo/ 的路径，symbol 填出问题的函数名。\n"
        f"  {scenario['goal_note']}"
    )

    parts = [
        f"key: {scenario['key']}",
        "group: ci",
        "# capability：新任务类，方差未知（见 test_ci_tasks_are_capability_not_regression）。",
        "purpose: capability",
        f"title: {scenario['title']}",
        f"tags: {scenario['tags']}",
        "goal: >-",
        goal,
        "selfcheck:",
        "  # 这份日志**必须**是真跑出来的：validate 会执行它并逐行比对（手编的会被抓住）。",
        "  command: python repo/tests/run_tests.py",
        "  expect_exit: 1",
        "  output_recorded_in: /home/ubuntu/ci_log.txt",
        "fixtures:",
        "  - path: /home/ubuntu/ci_log.txt",
        "    content: |",
        block(log_text, 6),
    ]

    for path, content in scenario["modules"].items():
        parts += [f"  - path: /home/ubuntu/{path}", "    content: |", block(content, 6)]

    parts += [
        "  - path: /home/ubuntu/repo/tests/run_tests.py",
        "    content: |",
        block(scenario["runner_source"], 6),
        "verify:",
        "  # 执行型检查（最强）：脚本来自**任务定义**（不在工作区），Agent 改不了断言，",
        "  # 而且它真的跑一遍修完的代码 —— 症状级修复会在这里挂掉。",
        "  - kind: exec_script",
        "    script_cwd: /home/ubuntu/repo",
        "    description: " + scenario["title"].split("：", 1)[-1],
        "    script: |",
        block(check, 6),
        "  - kind: file_content",
        "    path: /home/ubuntu/repo/tests/run_tests.py",
        '    contains: "# CI-FROZEN"',
        "    description: CI 提供的测试运行器未被删除或整体覆盖",
        "  - kind: file_content",
        "    path: /home/ubuntu/diagnosis.json",
        f'    contains: "{scenario["bug_file"]}"',
        "    description: 归因里点名了真正有 bug 的模块",
        "  - kind: file_content",
        "    path: /home/ubuntu/diagnosis.json",
        f'    contains: "{scenario["symbol"]}"',
        "    description: 归因里点名了出问题的符号",
        "process:",
        "  max_tool_calls: 40",
        "solution:",
    ]

    for path, content in scenario["fixed"].items():
        parts += [
            "  - kind: write_file",
            f"    path: /home/ubuntu/{path}",
            "    content: |",
            block(content, 6),
        ]
    diagnosis = '{"root_causes": [{"file": "%s", "symbol": "%s", "why": "%s"}]}' % (
        scenario["bug_file"],
        scenario["symbol"],
        scenario["why"],
    )
    parts += [
        "  - kind: write_file",
        "    path: /home/ubuntu/diagnosis.json",
        "    content: |",
        block(diagnosis, 6),
        "wrong:",
        "  # 症状级错解：只在**调用点**打补丁，共享模块仍然是坏的 —— 必须被拒绝。",
    ]
    for path, content in scenario["wrong"].items():
        parts += [
            "  - kind: write_file",
            f"    path: /home/ubuntu/{path}",
            "    content: |",
            block(content, 6),
        ]
    parts += [
        "  - kind: write_file",
        "    path: /home/ubuntu/diagnosis.json",
        "    content: |",
        block(
            '{"root_causes": [{"file": "%s", "symbol": "%s", "why": "只在调用点补回来的症状级修复"}]}'
            % (next(iter(scenario["wrong"])).split("/")[-1], "?"),
            6,
        ),
        "notes: >-",
        "  **自动生成**（`lab/bench/tasks/ci/_capture_logs.py`）：`ci_log.txt` 是真跑 buggy 代码"
        "捕获的输出，所以 `selfcheck` 的真实性规则必然通过；手编日志会被 validate 抓住。",
        "  设计要点：同一处共享模块被**两个调用方**使用（"
        + "、".join(sorted({p.split("/")[-1] for p in scenario["modules"] if p != "repo/tests/run_tests.py"}))
        + "），",
        '  所以只在调用点打补丁的"症状级修复"会在另一个调用方上暴露；',
        f"  另外还有一条日志里**没有**的边界用例（{scenario['extra_edge']}）挡住过拟合。",
    ]
    return "\n".join(parts) + "\n"


async def capture_log(scenario: dict, runner_source: str) -> str:
    """真跑一遍 buggy 代码，拿到**真实**失败输出（这就是 ci_log.txt 的内容）。"""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ci-capture-") as tmp:
        sandbox = LocalSandbox(pathlib.Path(tmp) / "ws", exec_timeout=60)
        await sandbox.ensure_sandbox()
        for path, content in scenario["modules"].items():
            written = await sandbox.write_file("/home/ubuntu/" + path, textwrap.dedent(content))
            assert written.success, written.message
        written = await sandbox.write_file("/home/ubuntu/repo/tests/run_tests.py", runner_source)
        assert written.success, written.message

        result = await sandbox.exec_command("capture", "/home/ubuntu", "python repo/tests/run_tests.py")
        output = (result.data or {}).get("output", "")
        exit_code = (result.data or {}).get("returncode")
        assert exit_code == 1, f"{scenario['key']}: 期望 buggy 代码退出码 1，实际 {exit_code}\n{output}"
        await sandbox.destroy()
    return "$ python tests/run_tests.py\n" + output.rstrip("\n") + "\n"


def main() -> int:
    """为每个场景：真跑 buggy 代码拿真实日志 → 渲染 YAML → 落盘。"""
    written = []
    for scenario in SCENARIOS:
        runner_source = _runner(scenario)
        scenario["runner_source"] = runner_source
        log_text = asyncio.run(capture_log(scenario, runner_source))
        target = HERE / f"{scenario['key'].removeprefix('ci_')}.yaml"
        target.write_text(render_yaml(scenario, log_text), encoding="utf-8")
        written.append(target.name)
        print(f"  ✓ {target.name}")
    print(f"生成 {len(written)} 个任务文件")
    return 0


def _runner(scenario: dict) -> str:
    """干净版 runner 生成（只依赖场景的用例表）。"""
    # 模块名从**场景的文件清单**推导，而不是从用例表达式里猜：
    # 初版用 `call.split(".")[0]`，而像 `40 <= sla.hours_left(...)` 这种
    # 以区间判断开头的用例会生成出 `import 40 <= sla` （SyntaxError）——
    # 于是日志里只剩一条语法错误轨迹，被真实性规则当场抓住。
    modules = sorted(
        pathlib.PurePosixPath(path).stem
        for path in scenario["modules"]
        if path.startswith("repo/") and "/" not in path.removeprefix("repo/")
    )
    imports = "\n".join(f"import {module}" for module in modules)
    prelude = scenario.get("runner_prelude", "").strip()
    if prelude:
        imports = imports + "\n" + prelude
    funcs = "\n\n".join(
        f"def {name}():\n    actual = {call}\n    return actual == {expected!r}, actual, {expected!r}"
        for name, call, _, expected in scenario["cases"]
    )
    cases = "\n".join(f'    ("{name}", {name}),' for name, _, _, _ in scenario["cases"])
    return (
        "# CI-FROZEN: 本文件由 CI 提供，不得修改\n"
        '"""最小的测试运行器（不依赖 pytest，只用标准库）。"""\n'
        "import pathlib\n"
        "import sys\n\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n\n"
        + imports
        + "\n\n\n"
        + funcs
        + "\n\n\nCASES = [\n"
        + cases
        + "\n]\n\n\ndef main():\n"
        "    failed = 0\n"
        "    for name, fn in CASES:\n"
        "        try:\n"
        "            ok, actual, expected = fn()\n"
        "        except Exception as exc:  # noqa: BLE001\n"
        '            print(f"{name} ... ERROR {type(exc).__name__}: {exc}")\n'
        "            failed += 1\n"
        "            continue\n"
        "        if ok:\n"
        '            print(f"{name} ... PASSED")\n'
        "        else:\n"
        '            print(f"{name} ... FAILED")\n'
        '            print(f"    expected {expected!r}，actual {actual!r}")\n'
        "            failed += 1\n"
        '    print("-" * 45)\n'
        '    print(f"{len(CASES)} 项：{len(CASES) - failed} 通过 / {failed} 失败")\n'
        "    return 1 if failed else 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
