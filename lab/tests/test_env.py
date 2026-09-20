#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境指纹的回归测试。

这些测试保护的是一个**可复现性**不变量：
**同一台机器上，Agent 看到的世界不应该取决于评测是从哪个 shell 启动的。**

实测过的问题（见 lab/infra/env.py 的模块文档）：从 Git Bash 启动时
`sqlite3`/`grep`/`od`/`tr` 都在（因为 Git 把 `usr\\bin` 加进了 PATH），
从 PowerShell 启动则全部不在 —— 于是"同一份代码换个 shell 跑"变成了两个不同的任务。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lab.infra.env import (
    TOOL_PROBES,
    deterministic_path,
    environment_fingerprint,
    guaranteed_dirs,
    render_fingerprint,
)


def test_path_is_guaranteed_prefix_plus_parent_fallback():
    """PATH = 固定前缀 + 父 PATH 兜底。

    兜底那一半是刻意的：不能"拿走"任务已经在用的东西
    （例如 `bc` 只在用户 PATH 里，而 `syn_sum_range` 的冒烟测试用过 `bc`）。
    """
    parent = os.pathsep.join([r"C:\some\user\bin", r"C:\another\bin"])
    result = deterministic_path(parent).split(os.pathsep)

    # 保证目录在前
    for item in guaranteed_dirs():
        assert item in result, f"保证目录丢了: {item}"
    if guaranteed_dirs():
        assert result[0] == guaranteed_dirs()[0]
    # 父 PATH 一项不丢，且在保证目录之后
    assert r"C:\some\user\bin" in result and r"C:\another\bin" in result
    assert result.index(r"C:\some\user\bin") >= len(guaranteed_dirs())


def test_path_does_not_depend_on_launch_context(monkeypatch):
    """核心不变量：把父 PATH 换成"从 PowerShell 启动"的样子，**保证工具依然可见**。

    这是本模块存在的唯一理由。

    ⚠️ 这条用例必须**先破坏 `os.environ["PATH"]`** 再验证，而不是先拿到
    `guaranteed_dirs()` 再断言它出现在结果里 —— 后者抓不到真正的缺陷：
    发现"保证目录"的过程本身如果依赖父 PATH（`shutil.which("git")` 就是），
    那么父 PATH 一退化，保证目录就变成空集，**修复在最需要它的场景下失效**，
    而测试全绿（因为测试进程自己拿着完好的 PATH）。这个坑真的踩过。
    """
    if os.name != "nt":
        pytest.skip("固定前缀是 Windows（Git for Windows）特有的问题")

    minimal_parent = os.pathsep.join([r"C:\Windows\System32", r"C:\Windows"])
    monkeypatch.setenv("PATH", minimal_parent)   # ← 关键：先退化父 PATH

    assert guaranteed_dirs(), (
        "父 PATH 退化后就找不到 Git 了 —— 发现过程本身依赖了要修的那个变量。"
        "第一来源必须与 PATH 无关（注册表 / 标准安装位置）。"
    )

    tools = environment_fingerprint(deterministic_path(minimal_parent))["tools"]
    for name in ("grep", "od", "tr", "sqlite3"):
        if any((Path(d) / f"{name}.exe").exists() for d in guaranteed_dirs()):
            assert tools[name], f"{name} 在保证目录里，却没被解析到 —— 前缀没生效"
    # 解释器兜底：父 PATH 里没有 python 时也要有
    assert tools["python"], "连 python 都解析不到，任务根本无法跑"


def test_interpreter_fallback_is_last_and_never_shadows():
    """宿主解释器目录必须是**最后一项**。

    它不是用来"优先"的，而是救急：父 PATH 里一个 python 都没有时至少还能跑。
    放在前面会让 `python`（宿主解释器，可能是 uv 的 3.12）与 `python3`
    （父 PATH 里的 3.13）指向不同版本 —— 比缺失更难查。
    """
    parent = os.pathsep.join([r"C:\some\user\bin", r"C:\another\bin"])
    parts = deterministic_path(parent).split(os.pathsep)
    interpreter_dir = str(Path(sys.executable).parent)
    if interpreter_dir in parts:
        assert parts[-1] == interpreter_dir, f"解释器目录不在最后: {parts[-3:]}"
        assert parts.index(r"C:\some\user\bin") < len(parts) - 1


def test_git_dirs_detected_from_both_layouts():
    """Git 的两种可执行文件布局都要能推导出安装根。

    `<root>\\cmd\\git.exe`（退 2 级）与 `<root>\\mingw64\\bin\\git.exe`（退 3 级）。
    只试一种的话，另一种布局会**安静地**什么都不返回 —— 于是 `sort`/`find`
    落到 `C:\\Windows\\System32` 下的 Windows 版（`find` 是文本搜索，
    不是目录遍历），而轨迹看起来完全正常。
    """
    if os.name != "nt":
        pytest.skip("Git for Windows 特有")
    dirs = guaranteed_dirs()
    if not dirs:
        pytest.skip("本机没有 Git for Windows")
    joined = " ".join(dirs).lower()
    assert "git" in joined
    # 至少要有一个真正的工具目录（不是只找到 cmd）
    assert any(d.lower().endswith(("usr\\bin", "mingw64\\bin")) for d in dirs), dirs


def test_fingerprint_is_stable_and_records_missing_tools():
    """指纹必须稳定（同一环境两次算出来一样），且**如实记录不可用的工具**。

    为什么强调"如实"：把缺失的工具悄悄略掉，就等于把"任务难度变了"藏起来。
    实测 `bc` 在目标机器上不可用 —— 它必须以 None 出现在指纹里。
    """
    first = environment_fingerprint()
    second = environment_fingerprint()
    assert first["digest"] == second["digest"]
    assert len(first["digest"]) == 10
    assert set(first["tools"]) == set(TOOL_PROBES)

    # 一个不存在的 PATH 应该让所有探测工具都变成 None（证明它是**真的在解析**）
    empty = environment_fingerprint(path=r"C:\definitely\not\a\real\dir")
    assert all(value is None for value in empty["tools"].values())
    assert empty["digest"] != first["digest"]


def test_fingerprint_digest_changes_with_environment():
    """环境变了，摘要必须跟着变 —— 否则它就无法用来判断"两次跑是不是同一个环境"。"""
    a = environment_fingerprint(path=r"C:\Windows\System32")
    b = environment_fingerprint(path=r"C:\Windows\System32;C:\Windows")
    assert a["digest"] != b["digest"]


def test_render_fingerprint_mentions_missing_tools():
    """人类可读输出必须把"缺了什么"说出来，而不是只给一个摘要。"""
    text = render_fingerprint(environment_fingerprint(path=r"C:\definitely\not\real"))
    assert "不可用" in text
    assert "探环境" in text or "测量缺陷" in text


def test_child_env_uses_deterministic_path(tmp_path: Path):
    """接线测试：沙箱真的用了固定前缀（函数写得再对，忘了调用就一点用没有）。"""
    from lab.infra.local_sandbox import LocalSandbox

    sandbox = LocalSandbox(tmp_path / "ws", exec_timeout=20)
    child_path = sandbox._build_child_env()["PATH"]
    for item in guaranteed_dirs():
        assert item in child_path.split(os.pathsep)

    fingerprint = sandbox.env_fingerprint()
    assert fingerprint["digest"]
    assert set(fingerprint["tools"]) == set(TOOL_PROBES)


def test_extra_env_can_still_override_path(tmp_path: Path):
    """显式传入的 PATH 仍然能覆盖（实验/对照需要），且指纹如实反映覆盖结果。

    这两件事必须同时成立：既要有默认的确定性，又要允许刻意制造差异 ——
    否则"环境差异到底影响多大"这个问题就没法做实验回答。
    """
    from lab.infra.local_sandbox import LocalSandbox

    override = r"C:\Windows\System32"
    sandbox = LocalSandbox(tmp_path / "ws", exec_timeout=20, extra_env={"PATH": override})
    assert sandbox._build_child_env()["PATH"] == override
    assert sandbox.env_fingerprint()["digest"] == environment_fingerprint(override)["digest"]
