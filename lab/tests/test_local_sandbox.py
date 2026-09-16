#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalSandbox 的回归测试。

这些测试是 Step 1 的"验收证据"：它们把 LocalSandbox 的关键不变量固定下来，
以后每次改沙箱实现都能立刻知道有没有把评测环境弄坏。

重点覆盖三件容易出错的事：
1. 逻辑路径映射（模型以为自己在 Linux 沙箱里，实际落在 workspace 下）；
2. 目录穿越防护（提示词注入的第一道闸门）；
3. 子进程环境的隔离（不继承宿主 PYTHON*，不泄露宿主环境变量）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lab.infra.local_sandbox import LocalSandbox, SandboxPathError


@pytest.fixture()
def sandbox(tmp_path: Path) -> LocalSandbox:
    """每个用例一个全新的沙箱根目录，测试之间互不污染。"""
    return LocalSandbox(tmp_path / "workspace", exec_timeout=20)


# ==================== 1. 路径映射 ====================

@pytest.mark.asyncio
async def test_absolute_path_maps_into_workspace_and_returns_logical_path(sandbox: LocalSandbox):
    """绝对路径要落到 workspace 内，且返回给模型的是逻辑路径。"""
    result = await sandbox.write_file("/home/ubuntu/result.txt", "hello")

    assert result.success is True
    # 1.返回值里的路径是逻辑路径（模型世界观一致）
    assert result.data["filepath"] == "/home/ubuntu/result.txt"
    # 2.真实文件确实落在沙箱根目录内部
    real_file = Path(sandbox._root) / "home" / "ubuntu" / "result.txt"
    assert real_file.read_text(encoding="utf-8") == "hello"

    read_back = await sandbox.read_file("/home/ubuntu/result.txt")
    assert read_back.data["content"] == "hello"
    assert read_back.data["filepath"] == "/home/ubuntu/result.txt"


@pytest.mark.asyncio
async def test_relative_path_is_relative_to_workspace_root(sandbox: LocalSandbox):
    """相对路径同样落在 workspace 内（沙箱根即当前目录的语义）。"""
    result = await sandbox.write_file("notes/a.md", "content")
    assert result.success is True
    assert (Path(sandbox._root) / "notes" / "a.md").exists()


@pytest.mark.asyncio
async def test_windows_style_path_is_normalised(sandbox: LocalSandbox):
    """Windows 风格反斜杠路径要被归一化处理，不能逃出沙箱。"""
    result = await sandbox.write_file("\\home\\ubuntu\\b.txt", "x")
    assert result.success is True
    assert (Path(sandbox._root) / "home" / "ubuntu" / "b.txt").exists()


# ==================== 2. 目录穿越防护 ====================

@pytest.mark.parametrize(
    "evil_path",
    [
        "/../secret.txt",
        "/home/../../secret.txt",
        "../../secret.txt",
        "..\\..\\secret.txt",  # 反斜杠写法同样要拦
    ],
)
@pytest.mark.asyncio
async def test_path_traversal_is_blocked(sandbox: LocalSandbox, evil_path: str):
    """目录穿越必须被拦下，并且以"结构化失败"的形式返回。

    为什么是返回失败而不是抛异常：模型需要读到失败原因并自己换路径；
    异常会中断整条 flow（这正是我们在 SUT 里修的 D4 类问题）。
    """
    result = await sandbox.read_file(evil_path)
    assert result.success is False
    assert result.error_type == "path_escape"


@pytest.mark.asyncio
async def test_to_local_raises_directly_for_internal_callers(sandbox: LocalSandbox):
    """内部调用方（需要明确知道路径非法时）可以直接拿到异常。"""
    with pytest.raises(SandboxPathError):
        sandbox._to_local("/../x")


# ==================== 3. 文件能力 ====================

@pytest.mark.asyncio
async def test_append_is_non_idempotent(sandbox: LocalSandbox):
    """追加写入是**非幂等**操作 —— D2 类问题（重试造成重复副作用）的测试锚点。

    这个用例的价值不在"验证追加能用"，而在于把"重试会重复追加"这个事实固定下来：
    Step 3 引入幂等键保护时，必须让这个用例的语义可被显式控制。
    """
    await sandbox.write_file("/home/ubuntu/log.txt", "line1\n")
    await sandbox.write_file("/home/ubuntu/log.txt", "line1\n", append=True)

    content = (await sandbox.read_file("/home/ubuntu/log.txt")).data["content"]
    assert content == "line1\nline1\n"  # 重试一次就会多一行，这就是副作用


@pytest.mark.asyncio
async def test_replace_and_search(sandbox: LocalSandbox):
    await sandbox.write_file("/home/ubuntu/a.txt", "foo\nbar\nfoo\n")

    replaced = await sandbox.replace_in_file("/home/ubuntu/a.txt", "foo", "baz")
    assert replaced.data["replaced_count"] == 2

    found = await sandbox.search_in_file("/home/ubuntu/a.txt", r"ba[rz]")
    assert found.success is True
    assert found.data["matches"] == ["baz", "bar", "baz"]
    assert found.data["line_numbers"] == [0, 1, 2]

    missing = await sandbox.search_in_file("/home/ubuntu/a.txt", r"never")
    assert missing.success is False
    assert missing.error_type == "not_found"


@pytest.mark.asyncio
async def test_replace_missing_target_is_deterministic_failure(sandbox: LocalSandbox):
    """找不到目标内容属于确定性失败：重试没有意义，必须如实上报。"""
    await sandbox.write_file("/home/ubuntu/a.txt", "hello")
    result = await sandbox.replace_in_file("/home/ubuntu/a.txt", "absent", "x")
    assert result.success is False
    assert result.error_type == "not_found"


@pytest.mark.asyncio
async def test_read_missing_file_and_truncation(sandbox: LocalSandbox):
    missing = await sandbox.read_file("/home/ubuntu/nope.txt")
    assert missing.error_type == "file_not_found"

    await sandbox.write_file("/home/ubuntu/big.txt", "x" * 500)
    truncated = await sandbox.read_file("/home/ubuntu/big.txt", max_length=100)
    assert truncated.data["truncated"] is True
    assert len(truncated.data["content"]) == 100


# ==================== 4. Shell 能力 ====================

@pytest.mark.asyncio
async def test_exec_command_returns_output_and_returncode(sandbox: LocalSandbox):
    result = await sandbox.exec_command("s1", "/home/ubuntu", f'"{sys.executable}" -c "print(6*7)"')

    assert result.data["returncode"] == 0
    assert "42" in result.data["output"]


@pytest.mark.asyncio
async def test_nonzero_returncode_is_surfaced_not_swallowed(sandbox: LocalSandbox):
    """非零返回码必须如实出现在返回值里 —— 不能吞掉（体检第 5 项）。"""
    result = await sandbox.exec_command("s2", "/home/ubuntu", f'"{sys.executable}" -c "import sys;sys.exit(3)"')

    assert result.data["returncode"] == 3
    assert "返回码 3" in result.message


@pytest.mark.asyncio
async def test_command_timeout_is_enforced(sandbox: LocalSandbox):
    """超时必须被硬性掐断 —— 这是"终止保证"的最小实现（体检第 6 项）。

    同时断言**耗时**：命令 sleep 60，超时 2 秒。
    如果只杀 shell 不杀进程树，孤儿进程会继续跑满 60 秒，
    整个测试套件也会被拖到 60 秒（真实踩过的坑，见 _kill_process_tree 注释）。
    """
    import time

    sandbox._exec_timeout = 2
    started = time.monotonic()
    result = await sandbox.exec_command(
        "s3", "/home/ubuntu", f'"{sys.executable}" -c "import time;time.sleep(60)"'
    )
    elapsed = time.monotonic() - started

    assert result.success is False
    assert result.error_type == "timeout"
    assert result.retryable is False
    # 2 秒超时 + 杀树开销，给足余量也不能接近 60 秒
    assert elapsed < 20, f"超时未真正生效，耗时 {elapsed:.1f}s（进程树可能没杀干净）"


@pytest.mark.asyncio
async def test_shell_path_rewriting(sandbox: LocalSandbox):
    """命令里的沙箱逻辑路径要被改写成真实路径，否则脚本路径根本找不到。"""
    await sandbox.write_file("/home/ubuntu/who.py", "print('rewritten-ok')")
    result = await sandbox.exec_command(
        "s4", "/home/ubuntu", f'"{sys.executable}" /home/ubuntu/who.py'
    )

    assert result.data["returncode"] == 0
    assert "rewritten-ok" in result.data["output"]


@pytest.mark.asyncio
async def test_unsupported_interactive_shell_reports_failure(sandbox: LocalSandbox):
    """不支持的能力要明确失败，不能静默假装成功。"""
    result = await sandbox.write_shell_input("s5", "y")
    assert result.success is False
    assert result.error_type == "unsupported"


# ==================== 5. 子进程环境隔离（安全 + 正确性） ====================

@pytest.mark.asyncio
async def test_child_env_does_not_leak_host_variables(sandbox: LocalSandbox, monkeypatch):
    """子进程不能继承宿主的敏感环境变量。

    这是"提示词注入 → 凭据泄露"链路的闸门：如果模型能读到 LAB_LLM_API_KEY，
    那么任何一句注入指令都能把宿主机凭据带走。
    """
    monkeypatch.setenv("LAB_LLM_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-never-leak")

    result = await sandbox.exec_command("s6", "/home/ubuntu", "set" if sys.platform == "win32" else "env")
    output = result.data["output"]

    assert "sk-should-never-leak" not in output
    assert "should-never-leak" not in output


@pytest.mark.asyncio
async def test_child_env_strips_python_home_pollution(sandbox: LocalSandbox, monkeypatch):
    """宿主 PYTHONHOME 不能污染子进程解释器。

    真实踩过的坑：宿主 PYTHONHOME 指向 3.12 而 PATH 里的 python 是 3.13，
    子进程直接 `AssertionError: SRE module mismatch`。
    最危险的是模型会以为是自己命令写错了，白白烧掉好几个迭代。
    """
    monkeypatch.setenv("PYTHONHOME", "/nonexistent/python/home")

    result = await sandbox.exec_command(
        "s7", "/home/ubuntu", f'"{sys.executable}" -c "print(\'clean\')"'
    )

    assert result.data["returncode"] == 0
    assert "clean" in result.data["output"]

    env_dump = (await sandbox.exec_command("s8", "/home/ubuntu", "set" if sys.platform == "win32" else "env")).data["output"]
    assert "PYTHONHOME" not in env_dump


@pytest.mark.asyncio
async def test_extra_env_is_opt_in(sandbox: LocalSandbox):
    """需要额外放行环境变量时必须显式声明（如评测需要走代理）。"""
    sandbox._extra_env = {"MY_EXPLICIT_VAR": "visible"}
    result = await sandbox.exec_command("s9", "/home/ubuntu", "set" if sys.platform == "win32" else "env")
    assert "visible" in result.data["output"]
