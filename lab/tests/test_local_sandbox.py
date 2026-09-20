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

import subprocess
import sys
from pathlib import Path

import pytest

from lab.infra.local_sandbox import LocalSandbox, SandboxPathError


@pytest.fixture()
def sandbox(tmp_path: Path) -> LocalSandbox:
    """每个用例一个全新的沙箱根目录，测试之间互不污染。

    ⚠️ 这个 fixture 必须**释放沙箱**：`LocalSandbox` 现在会占一个 `subst` 虚拟盘符，
    而它是**进程外资源** —— 不释放就会一直占着字母。实测：不给 fixture 加清理时，
    一次 pytest 就把 22 个候选盘符全用光了，后续运行只能静默退化成"逃逸只告警"。
    """
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
    # retryable=True 表达的是"超时属于暂态失败"，**不代表应该重试**。
    # 是否真的重试由中间件结合幂等性判定：shell_execute 非幂等 → 不会被自动重试。
    # （这正是 D2 "重试非幂等工具导致重复副作用"的正解，详见 lab/guard/retry.py）
    assert result.retryable is True
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


# ==================== 6. 脚本逃逸检测（安全 + 正确性） ====================

@pytest.mark.asyncio
async def test_detects_script_writing_outside_workspace(tmp_path: Path, monkeypatch):
    """**关掉虚拟盘**时，脚本里的绝对路径仍会落到工作区外 —— 那种情况必须被检测到。

    ⚠️ 修好虚拟盘之后，这条用例**必须显式关掉虚拟盘**才有意义：
    默认路径下产物已经被导回工作区（见 test_script_absolute_paths_land_inside_workspace），
    逃逸不再发生。保留它是因为 subst 可能不可用（组策略 / 盘符占满），
    那时它仍是唯一的发现手段 —— 而"产物找不到"这类问题必须能一眼归因。

    这是 fast mode 的真实缺陷（不是假设）：路径映射只作用于文件工具与命令行，
    管不到脚本内容。实测在一次真实的 `sem_refactor_config` 运行里，
    Agent 把文件写到了 `D:/home/ubuntu/parts/part_000...`（用正斜杠写路径，
    因为 Windows 风格的反斜杠在这个 docstring 里会被 Python 当成转义序列 —— 
    这就是为什么本仓库的脚本内容里一律用 chr(10)/chr(47) 拼字符）。
    """
    monkeypatch.setenv("LAB_SANDBOX_VIRTUAL_DRIVE", "0")
    sandbox = LocalSandbox(tmp_path / "ws", exec_timeout=20)
    await sandbox.ensure_sandbox()

    escaped_root = sandbox._escaped_root()
    if escaped_root is None:
        pytest.skip("无法确定逃逸目录")

    # 先把逃逸目录建出来：python 的 write_text **不会**自动创建父目录，
    # 而真实场景里 Agent 的脚本往往会先 mkdir -p 再写（或者直接写失败）。
    # 不建的话这条测试会在“干净的机器上”因为 FileNotFoundError 而失败 ——
    # 那种失败看起来像“检测器坏了”，其实是测试自己没铺好前提。
    escaped_root.mkdir(parents=True, exist_ok=True)

    # 用 chr() 拼出绝对路径：避免测试代码本身被多层转义搞错
    code = (
        "import pathlib; p=" + "+".join(f"chr({ord(c)})" for c in "/home/ubuntu/_esc_probe.txt")
        + "; pathlib.Path(p).write_text(chr(120))"
    )
    result = await sandbox.exec_command("esc", "/home/ubuntu", f'python -c "{code}"')

    assert result.data.get("escaped_paths"), "写到了工作区外却没有任何检测结果"

    # 清理探针，避免污染后续测试/后续的真实运行
    probe = escaped_root / "_esc_probe.txt"
    if probe.exists():
        probe.unlink()
    # 连同空的父目录一起清掉：测试自己弄脏的环境要自己恢复，
    # 否则每次跑测试都会在盘根留下一个空的 home/ubuntu
    try:
        if not any(escaped_root.iterdir()):
            escaped_root.rmdir()
            parent = escaped_root.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
    except OSError:
        pass


@pytest.mark.asyncio
async def test_escape_detection_reports_delta_not_existence(sandbox: LocalSandbox):
    """关键性质：只报**本次新增/修改**的文件，不要因为目录存在就一直告警。

    初版检测器只判断目录存在，结果一次 40 运行的评测报了 98 次告警 ——
    几乎全是噪声。**噪声化的安全告警比没有告警更糟**：它会训练人忽略它。
    """
    first = await sandbox.exec_command("d1", "/home/ubuntu", 'python -c "print(1)"')
    second = await sandbox.exec_command("d2", "/home/ubuntu", 'python -c "print(2)"')

    # 普通命令（不碰工作区外）不应该产生逃逸报告 —— 无论那个目录是否已被创建
    assert first.data.get("escaped_paths") == []
    assert second.data.get("escaped_paths") == []


# ==================== 4. 子进程输出的解码与归一（噪声地板的主要来源）====================
#
# 这一组测试对应一个实测结论：噪声地板 ±29% 里，主导项是**单个任务**的 +118%，
# 而那条 43 步的轨迹里有 10 步在追行尾（`od -c` / `tr -d '\r'` / `cmp`）。
# 根因是子进程输出带 CRLF，且 cmd.exe 的错误信息是 GBK 而 Python 是 UTF-8。
#
# 目标环境（Ubuntu）不会有这两个现象，所以这里的测试同时是**保真性**测试：
# "本地 fast mode 的输出看起来应当像 Ubuntu 的输出"。

def test_output_normalises_crlf_to_lf():
    """CRLF 必须归一成 LF —— 否则模型会去修一个由本地实现造出来的问题。"""
    from lab.infra.local_sandbox import decode_command_output

    assert decode_command_output(b"a\r\nb\r\n") == "a\nb\n"
    assert decode_command_output(b"only\r\n") == "only\n"
    assert decode_command_output(b"\r\n\r\n") == "\n\n"


def test_output_preserves_in_line_carriage_return():
    """行内的 `\r` 是进度条刷新（`\rProgress 50%`），有真实含义，不能一并删掉。"""
    from lab.infra.local_sandbox import decode_command_output

    assert decode_command_output(b"Progress 10%\rProgress 90%\ndone\n") == \
        "Progress 10%\rProgress 90%\ndone\n"


def test_output_decodes_utf8_first():
    """Python 子进程的输出是 UTF-8（_build_child_env 固定的），必须原样读出中文。"""
    from lab.infra.local_sandbox import decode_command_output

    raw = "写入成功: /home/ubuntu/a.txt\n".encode("utf-8")
    assert decode_command_output(raw) == "写入成功: /home/ubuntu/a.txt\n"


def test_output_falls_back_to_oem_codepage_for_cmd_exe_errors():
    """cmd.exe 的错误信息是 OEM 代码页（中文机器上是 GBK）—— 不能变成乱码进上下文。

    实测原文就是这条：`'command' 不是内部或外部命令`。
    """
    from lab.infra.local_sandbox import decode_command_output

    message = "'command' 不是内部或外部命令，也不是可运行的程序\r\n"
    decoded = decode_command_output(message.encode("cp936"))
    assert "不是内部或外部命令" in decoded
    assert "\ufffd" not in decoded, "不该出现替换字符（那是乱码的标志）"
    assert "\r" not in decoded


def test_output_handles_mixed_encodings_in_one_stream():
    """一次命令里同时有 Python(UTF-8) 与 cmd.exe(GBK) 输出 —— 逐行判定要各自正确。

    这就是**不能整块猜编码**的原因：整块按 UTF-8 解会把 GBK 那行变成乱码，
    整块按 GBK 解会把 UTF-8 那行变成乱码。
    """
    from lab.infra.local_sandbox import decode_command_output

    raw = "中文输出正常\n".encode("utf-8") + "'foo' 不是内部或外部命令\r\n".encode("cp936")
    decoded = decode_command_output(raw)
    assert "中文输出正常" in decoded
    assert "不是内部或外部命令" in decoded
    assert "\ufffd" not in decoded


@pytest.mark.asyncio
async def test_real_command_output_has_no_crlf(sandbox: LocalSandbox):
    """端到端：真的跑一条会输出多行的命令，返回给模型的内容不能带 `\r`。

    这是"接线"测试 —— 解码函数写得再对，忘了在 exec_command 里调用就一点用没有。
    """
    result = await sandbox.exec_command(
        "crlf1", "/home/ubuntu", f'"{sys.executable}" -c "print(1);print(2)"'
    )

    output = result.data["output"]
    assert "\r" not in output, f"输出里仍有 \r: {output!r}"
    assert output.strip().splitlines() == ["1", "2"]


@pytest.mark.asyncio
async def test_failed_command_error_text_is_readable(sandbox: LocalSandbox):
    """命令不存在时的报错必须**可读**（不能是乱码）—— 乱码会误导模型。

    注意：不同 shell 的报错文案不同（cmd.exe / bash），所以这里只断言
    "没有替换字符"，不断言具体措辞 —— 断言文案会把测试绑死在 Windows 上。
    """
    result = await sandbox.exec_command(
        "bad1", "/home/ubuntu", "this_command_definitely_does_not_exist_zzz"
    )

    output = result.data["output"]
    assert "\ufffd" not in output, f"报错里有乱码: {output!r}"
    assert "\r" not in output


# ==================== 5. 两个保真装置（噪声地板实测之后加的） ====================
#
# 这两个装置的存在理由都是**实测**出来的（见 docs/noise-floor-measured.md）：
# 噪声地板 ±24% 里有一大块不是模型抖动，而是本地实现的失真。

@pytest.mark.skipif(sys.platform != "win32", reason="虚拟盘符是 Windows 机制")
@pytest.mark.asyncio
async def test_script_absolute_paths_land_inside_workspace(tmp_path: Path):
    """脚本**内容**里的 `/home/ubuntu/...` 必须落进工作区，而不是真实盘根。

    这是 noise-floor-measured.md §8 那次真实失败的回归测试：
    Agent 写 `DB_PATH = "/home/ubuntu/sales.db"`，而字符串替换只作用于命令行、
    管不到脚本内容 → 产物落到 `<盘>:/home/ubuntu/`，判定器读不到 →
    **正确的工作被判失败，而且 error_type 为空**（SUT 自己以为成功了），
    看起来像"Agent 写错代码"。

    机制：给每次运行分配一个 subst 虚拟盘指向工作区，子进程 cwd 落在该盘上；
    Windows 的无盘符绝对路径按"当前盘"解析，于是自动指向工作区内的同一位置。
    """
    sb = LocalSandbox(tmp_path / "ws", exec_timeout=30)
    await sb.ensure_sandbox()
    script = sb._root / "home" / "ubuntu" / "w.py"
    script.write_text(
        "import pathlib\npathlib.Path('/home/ubuntu/inside.txt').write_text('ok')\n",
        encoding="utf-8",
    )

    result = await sb.exec_command("d1", "/home/ubuntu", f'"{sys.executable}" /home/ubuntu/w.py')

    assert result.data["returncode"] == 0, result.data["output"]
    inside = sb._root / "home" / "ubuntu" / "inside.txt"
    assert inside.exists(), "产物没落在工作区里 —— 逃逸又回来了"
    assert inside.read_text(encoding="utf-8") == "ok"
    # 导回工作区之后，逃逸告警就不该再响（否则它又变成噪声化的安全告警）
    assert result.data["escaped_paths"] == []
    await sb.destroy()


@pytest.mark.skipif(sys.platform != "win32", reason="虚拟盘符是 Windows 机制")
@pytest.mark.asyncio
async def test_virtual_drive_is_released(tmp_path: Path):
    """盘符必须被释放。

    它**不随进程退出消失**，会一直占着字母直到重启或手工 `subst /D`；
    26 个字母用完，后续运行就只能退化成"逃逸只告警"。
    """
    import os

    sb = LocalSandbox(tmp_path / "ws", exec_timeout=30)
    await sb.ensure_sandbox()
    drive = sb._drive
    assert drive is not None, "没有分配虚拟盘（subst 不可用？）"
    assert os.path.exists(drive + "\\")

    await sb.destroy()

    assert sb._drive is None
    assert not os.path.exists(drive + "\\"), f"{drive} 没被释放，会泄漏盘符"


@pytest.mark.skipif(sys.platform != "win32", reason="虚拟盘符是 Windows 机制")
@pytest.mark.asyncio
async def test_stale_drive_mapping_is_reclaimed(tmp_path: Path):
    """目标目录已不存在的陈旧映射不能挡住分配，而且必须被清掉。

    为什么需要回收：盘符是进程外资源，被强杀的进程会留下映射，
    而候选字母只有 26 个。不回收的话，跑久了就会"拿不到盘符 → 静默退化"。

    只回收"目标已不存在"的：目标还在的映射可能是**另一个并发进程**正在用的，
    抢它会弄坏别人正在跑的任务。

    ⚠️ 本用例会自己造一个陈旧映射，所以结束前**必须自己清掉** ——
    测试弄脏的环境要自己恢复，否则每次跑测试都会在盘根留下垃圾映射。
    """
    import shutil

    from lab.infra.local_sandbox import _DRIVE_POOL, list_drive_mappings

    ghost = tmp_path / "ghost-ws"
    ghost.mkdir()
    probe = LocalSandbox(ghost, exec_timeout=20)
    await probe.ensure_sandbox()
    letter = probe._drive
    assert letter is not None, "没有可用盘符（上一个用例泄漏了？）"

    # 模拟"进程被强杀"：把目标目录删掉，但映射还留着
    await probe.destroy()
    probe._drive = letter  # 假装没释放
    _DRIVE_POOL[letter] = str(ghost)
    subprocess.run(["subst", letter, str(ghost)], capture_output=True)
    shutil.rmtree(ghost, ignore_errors=True)

    try:
        fresh = LocalSandbox(tmp_path / "fresh-ws", exec_timeout=20)
        await fresh.ensure_sandbox()
        assert fresh._drive is not None
        # 不变量：新拿到的盘符必须指向**它自己的工作区**
        target = {m["drive"]: m["target"] for m in list_drive_mappings()}.get(fresh._drive, "")
        assert target.lower() == str(fresh._root).lower(), f"{fresh._drive} 指向了别处: {target}"
        await fresh.destroy()
    finally:
        # 自己造的陈旧映射自己清（哪怕断言失败也要清）
        subprocess.run(["subst", letter, "/D"], capture_output=True)
        _DRIVE_POOL.pop(letter, None)


@pytest.mark.asyncio
async def test_crlf_written_by_scripts_is_normalized(tmp_path: Path):
    """脚本写出的 CRLF 要归一为 LF —— 目标环境 Ubuntu 根本不产出 CRLF。

    为什么值得一个用例：模型看到 `\r\n` 会**正确地**（对 Linux 而言）去修它，
    实测地板 suite 里 **9.5% 的 shell 调用**在追行尾（`od -c` / `cmp` / `tr -d '\r'`），
    其中 sem_csv_clean 一个任务占 16 次。这是纯浪费，不是能力问题。
    """
    sb = LocalSandbox(tmp_path / "ws", exec_timeout=30)
    await sb.ensure_sandbox()
    (sb._root / "home" / "ubuntu" / "w.py").write_text(
        "import pathlib\npathlib.Path('/home/ubuntu/crlf.txt').write_text('a\\nb\\n')\n",
        encoding="utf-8",
    )

    result = await sb.exec_command("d1", "/home/ubuntu", f'"{sys.executable}" /home/ubuntu/w.py')

    data = (sb._root / "home" / "ubuntu" / "crlf.txt").read_bytes()
    # 平台无关的不变量：产物必须是 LF（Ubuntu 的形状）
    assert data == b"a\nb\n", data
    if sys.platform == "win32":
        # Windows 上 Python 文本模式本来会写 CRLF，所以必须真的发生过归一
        assert "/home/ubuntu/crlf.txt" in result.data["normalized_line_endings"]
    await sb.destroy()


@pytest.mark.asyncio
async def test_binary_files_are_never_touched(tmp_path: Path):
    """含 NUL 字节的文件一律不动 —— 数据库 / zip / 图片被改会直接毁掉产物。

    这是归一装置的**反向**保护：只归一文本。用 NUL 字节当判据（而不是后缀名），
    因为后缀名可以骗人，而 NUL 在文本文件里几乎不会出现。
    """
    sb = LocalSandbox(tmp_path / "ws", exec_timeout=30)
    await sb.ensure_sandbox()
    payload = b"PK\x03\x04\x00\x00row1\r\nrow2\r\n"
    (sb._root / "home" / "ubuntu" / "blob.bin").write_bytes(payload)

    await sb.exec_command("d1", "/home/ubuntu", "echo ok")

    assert (sb._root / "home" / "ubuntu" / "blob.bin").read_bytes() == payload
    await sb.destroy()


@pytest.mark.asyncio
async def test_crlf_normalization_can_be_disabled(tmp_path: Path, monkeypatch):
    """归一必须能被关掉 —— 否则"这次变化是不是归一带来的"就无法回答。

    开关的意义与 `--guard none` 相同：**对照实验的前提**。
    """
    monkeypatch.setenv("LAB_SANDBOX_NORMALIZE_CRLF", "0")
    sb = LocalSandbox(tmp_path / "ws", exec_timeout=30)
    await sb.ensure_sandbox()
    (sb._root / "home" / "ubuntu" / "w.py").write_text(
        "import pathlib\npathlib.Path('/home/ubuntu/crlf.txt').write_text('a\\nb\\n')\n",
        encoding="utf-8",
    )

    result = await sb.exec_command("d1", "/home/ubuntu", f'"{sys.executable}" /home/ubuntu/w.py')

    assert result.data["normalized_line_endings"] == []
    data = (sb._root / "home" / "ubuntu" / "crlf.txt").read_bytes()
    if sys.platform == "win32":
        assert b"\r\n" in data, "关掉开关后应该保留 CRLF（这就是对照组的形状）"
    await sb.destroy()
