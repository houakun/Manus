#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工作区新鲜度（`lab/bench/workspace.py`）的回归测试。

守的是一条**测量纪律**：读产物之前必须知道"这些产物是不是那一次运行留下的"。

为什么它不是"锦上添花"：工作区路径曾经不含 suite_id，后跑的 suite 会**静默覆盖**
先跑的（实测 100 条校准样本里 39 条中招）。判定是当场做的、所以是对的，于是
"判定通过 + 产物是别人的"可以同时成立 —— 而所有事后读产物的人（`bench show` /
`judge` / 人工标注）都在读别人的产物，不报任何错。

三态必须分清：`fresh` / `stale` / `unknown`。把 `unknown` 当成 `fresh` 就是把
"没验证过"写成"验证过了"（本项目自纠 #1/#8 都是这类错误的变体）。
"""

from __future__ import annotations

import os
from datetime import datetime

from lab.bench.workspace import (
    DIGEST_MATCH,
    DIGEST_MISMATCH,
    DIGEST_UNKNOWN,
    FRESH,
    STALE,
    UNKNOWN,
    artifact_digest,
    check_freshness,
    compare_digest,
    render_freshness,
)
from lab.cli import _render_artifacts, _stale_flag

RUN_TIME = "2026-01-01T00:00:00"
BEFORE = datetime(2025, 12, 31, 12, 0, 0).timestamp()
AFTER = datetime(2026, 6, 1, 12, 0, 0).timestamp()


def _workspace(tmp_path, mtime: float, name: str = "out.txt"):
    """造一个带产物的工作区，并把产物 mtime 钉到指定时刻。"""
    root = tmp_path / "ws" / "home" / "ubuntu"
    root.mkdir(parents=True)
    path = root / name
    path.write_text("内容", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return tmp_path / "ws"


def test_fresh_when_products_precede_the_run(tmp_path):
    """产物时间早于运行时间 → fresh（这才是正常情况）。"""
    ws = _workspace(tmp_path, BEFORE)
    freshness = check_freshness(str(ws), RUN_TIME)
    assert freshness.state == FRESH
    assert freshness.stale is False


def test_stale_when_a_later_run_overwrote_products(tmp_path):
    """产物时间**晚于**运行时间 → stale（被后来的运行覆盖过）。"""
    ws = _workspace(tmp_path, AFTER)
    freshness = check_freshness(str(ws), RUN_TIME)
    assert freshness.state == STALE
    assert freshness.stale is True
    # 前置条件写出来（顺带给类型检查器一个收窄点）
    assert freshness.newest_mtime is not None and freshness.run_time is not None
    assert freshness.newest_mtime > freshness.run_time


def test_unknown_without_run_time_or_workspace(tmp_path):
    """没时间戳 / 目录不存在 → **unknown**，不是 fresh。

    为什么单列一条：把"没法判断"默认成"没问题"，正是这类缺陷能潜伏 39% 的原因。
    """
    ws = _workspace(tmp_path, BEFORE)
    assert check_freshness(str(ws), None).state == UNKNOWN
    assert check_freshness(str(ws), "不是时间").state == UNKNOWN
    assert check_freshness(str(tmp_path / "nope"), RUN_TIME).state == UNKNOWN


def test_pycache_does_not_make_a_workspace_look_stale(tmp_path):
    """`__pycache__` 是运行残留，不算产物 —— 不能让它的时间影响判断。"""
    ws = _workspace(tmp_path, BEFORE)
    cache = ws / "home" / "ubuntu" / "__pycache__"
    cache.mkdir()
    pyc = cache / "x.pyc"
    pyc.write_bytes(b"\x00")
    os.utime(pyc, (AFTER, AFTER))  # 缓存比运行时间晚
    assert check_freshness(str(ws), RUN_TIME).state == FRESH


def test_render_always_says_something(tmp_path):
    """三种状态**都要有输出**：静默的 fresh 会让人把"没告警"读成"没检查"。"""
    fresh = check_freshness(str(_workspace(tmp_path / "a", BEFORE)), RUN_TIME)
    stale = check_freshness(str(_workspace(tmp_path / "b", AFTER)), RUN_TIME)
    unknown = check_freshness(str(tmp_path / "nope"), RUN_TIME)
    assert (fresh.state, stale.state, unknown.state) == (FRESH, STALE, UNKNOWN)
    for report in (fresh, stale, unknown):
        assert render_freshness(report), "任何状态都必须有输出"
    assert "产物不可信" in render_freshness(stale)[0]


def test_freshness_baseline_is_keyed_by_run_not_workspace(tmp_path):
    """基准时间必须按**运行身份**（suite+task+run+arm）取，不能按 workspace 反查。

    这是实测抓到的真 bug：第一版实现用 `load_task_by_workspace()` 取基准，而
    **工作区正是被覆盖的那个东西** —— 覆盖后反查拿到的是“后来那次”的时间，
    基准跟着漂到后面去，于是同一批 100 条样本里 39 条 stale **只报出 1 条**。
    是“在真实数据上跑一遍”把它抓出来的（单测当时全绿）。
    """
    from lab.trace.store import SpanStore

    store = SpanStore(tmp_path / "traces.db")
    # 直接造两行：save_outcome 会把 created_at 写成 now()，控制不了时间
    with store._connect() as conn:  # noqa: SLF001 —— 测试故意绕过写入路径
        for run_id, suite, when in (
            ("r-old", "aaaa1111", "2026-01-01T00:00:00"),
            ("r-new", "bbbb2222", "2026-06-01T00:00:00"),
        ):
            conn.execute(
                "INSERT INTO bench_runs (run_id, suite_id, task_key, run_index, arm, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, suite, "syn_sum_range", 0, "noise-a", when),
            )
        conn.commit()

    # 同一个 task/run/arm，两个 suite 时间不同 —— 必须各拿各的（短 id 前缀匹配）
    assert store.run_created_at("aaaa1111", "syn_sum_range", 0, "noise-a") == "2026-01-01T00:00:00"
    assert store.run_created_at("bbbb2222", "syn_sum_range", 0, "noise-a") == "2026-06-01T00:00:00"
    # 查不到就是 None（→ 新鲜度 unknown），不能默认成“新鲜”
    assert store.run_created_at("cccc3333", "syn_sum_range", 0, "noise-a") is None


def test_stale_flag_is_tristate(tmp_path):
    """`_stale_flag` 必须区分 `"0"`（验过没问题）与 `""`（没法判断）。"""
    assert _stale_flag(str(_workspace(tmp_path, BEFORE)), RUN_TIME) == "0"
    assert _stale_flag(str(tmp_path / "nope"), RUN_TIME) == ""
    assert _stale_flag(str(tmp_path / "nope"), None) == ""


def test_bench_show_artifacts_warn_on_stale_workspace(tmp_path):
    """`bench show` 的关键产物必须带**显式告警**，而不是安静地把别人的产物列出来。"""
    stale_ws = _workspace(tmp_path / "stale", AFTER)
    text = "\n".join(_render_artifacts(str(stale_ws), run_time=RUN_TIME, max_files=10))
    assert "产物不可信" in text and "已被后来的运行覆盖" in text
    assert "out.txt" in text  # 产物仍然列出来（告警而不是隐瞒）

    fresh_ws = _workspace(tmp_path / "fresh", BEFORE)
    fresh_text = "\n".join(_render_artifacts(str(fresh_ws), run_time=RUN_TIME, max_files=10))
    assert "产物不可信" not in fresh_text
    assert "新鲜度" in fresh_text  # 但也要说一句"检查过了"


def test_render_artifacts_without_run_time_stays_silent(tmp_path):
    """不传运行时间时不告警（旧调用点/旧数据没有时间戳，不能凭空判 stale）。"""
    ws = _workspace(tmp_path, AFTER)
    text = "\n".join(_render_artifacts(str(ws), max_files=10))
    assert "产物不可信" not in text
    assert "新鲜度" not in text


# ==================== 产物摘要（内容层校验，比 mtime 可靠） ====================


def test_digest_match_and_mismatch(tmp_path):
    """摘要的两条主路径：内容没变 → match；改动一个字节 → mismatch。

    为什么需要它而不只靠 mtime：mtime 会被复制、恢复、时钟调整骗过 ——
    而内容摘要不会（“这个目录还是判定时那份吗”的硬证据）。
    """
    ws = _workspace(tmp_path, BEFORE)
    stored = artifact_digest(str(ws))
    assert stored
    assert compare_digest(str(ws), stored) == DIGEST_MATCH

    (ws / "home" / "ubuntu" / "out.txt").write_text("改过了", encoding="utf-8")
    assert compare_digest(str(ws), stored) == DIGEST_MISMATCH


def test_digest_missing_is_unknown_not_match(tmp_path):
    """没存过摘要（历史行）→ **unknown**，绝不能读成 match。

    把“没算过”当成“一致”，就是把未验证写成已验证 —— 这个项目最忌讳的那类错误。
    """
    ws = _workspace(tmp_path, BEFORE)
    assert compare_digest(str(ws), "") == DIGEST_UNKNOWN
    assert compare_digest(str(ws), None) == DIGEST_UNKNOWN
    # 目录不存在也是 unknown（不猜）
    assert compare_digest(str(tmp_path / "nope"), "deadbeef") == DIGEST_UNKNOWN


def test_digest_covers_input_and_ignores_pycache(tmp_path):
    """口径必须与新鲜度一致：**含 `input/`**（fixtures 也是这次运行的一部分），
    但 `__pycache__` 是运行残留、不算 —— 否则它会让摘要每次都不一样。"""
    ws = _workspace(tmp_path, BEFORE)
    (ws / "home" / "ubuntu" / "input").mkdir()
    (ws / "home" / "ubuntu" / "input" / "data.txt").write_text("素材", encoding="utf-8")
    before = artifact_digest(str(ws))

    cache = ws / "home" / "ubuntu" / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"\x00")
    assert artifact_digest(str(ws)) == before  # pycache 不影响摘要

    (ws / "home" / "ubuntu" / "input" / "data.txt").write_text("素材被改了", encoding="utf-8")
    assert artifact_digest(str(ws)) != before  # input/ 被覆盖要能看出来


def test_existing_rows_without_a_digest_read_as_unknown(tmp_path):
    """既有行（没算过摘要）仍可读，且校验只能是 unknown —— 不能读成 match。

    这是 success criteria 里写明的一条：“既有 661 行仍可读，缺失按未知”。
    历史行**无法回填**（当时没算），所以它们的 `artifact_digest` 永远是空 ——
    能做的只是让工具如实说“无法校验”。
    """
    from lab.trace.store import SpanStore

    store = SpanStore(tmp_path / "traces.db")  # init_schema：迁移机制保证新库自带该列
    with store._connect() as conn:  # noqa: SLF001
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "artifact_digest" in columns
        # 模拟一条历史行：列存在但值为空
        conn.execute("INSERT INTO tasks (task_id, workspace, artifact_digest) VALUES ('t1', '/w', NULL)")
        conn.commit()

    row = store.load_task("t1")
    assert row is not None
    assert not row.get("artifact_digest")
    assert compare_digest("/w", row.get("artifact_digest")) == DIGEST_UNKNOWN
