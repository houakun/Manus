#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""子进程环境：**确定性 PATH** + **环境指纹**。

== 为什么需要这个模块（实测发现）==
`LocalSandbox` 原来把父进程的 `PATH` 原样传给子进程。于是"Agent 看到的世界"
取决于**评测是怎么被启动的** —— 实测同一段探测命令、同一台机器：

    从 Git Bash 启动：python python3 sqlite3 which grep od tr cmp uname sed awk 全在
    从 PowerShell 启动：**全部不在**（连 python 都不在）

原因：Git for Windows 只在 Git Bash 里把 `usr\\bin` / `mingw64\\bin` 加进 PATH；
在 cmd / PowerShell 里只加 `Git\\cmd`。而 `sqlite3` / `grep` / `od` / `tr` / `cmp` / `uname` / `which`
全都在 `usr\\bin` 里。

== 为什么这是**测量缺陷**而不是"环境差异"==
同一个任务、同一份代码，换个 shell 启动就换了一套可用工具：
- 模型会花若干次迭代去"探环境"（轨迹里 `which` / `command -v` 出现 110 次）；
- 任务难度本身变了（能用 `od -c` 和不能用的 Agent 不是一个 Agent）。

也就是说**自变量被污染了**：你以为在比"两份代码"，实际还夹着"两个启动上下文"。
这正是噪声地板宽到 ±24% 时应该先查的那类东西。

== 做法 ==
1. **固定前缀**：把"我们保证要有的工具目录"按固定顺序前置（Git 的两个 bin + 系统目录）；
2. **父 PATH 兜底**：接在后面 —— 目的是**不拿走**任务已经在用的东西
   （例如 `bc` 只在用户 PATH 里，而 `syn_sum_range` 的冒烟测试用过 `bc`）；
3. **记录残余**：只在父 PATH 里的工具仍随启动上下文变化，所以另外产出
   **环境指纹**（解析出的每个工具的绝对路径 + 可 GROUP BY 的短摘要），
   让"这次运行到底看到了什么"变成**数据**，而不是假设。

> 纪律与 `--guard` 那一套一致：**先让差异可见，再谈消除它**。
> 本模块只做"尽可能确定 + 把残余记下来"，不假装已经把环境完全钉死。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 指纹里要探测的工具。
# 选这些的理由：它们都是**任务实际用过**或"缺了会让模型去探环境"的东西 ——
# 前一组来自轨迹（`which` / `command -v` 探的就是它们），
# 后一组是 Unix 基础文本工具（缺任何一个都会让 Agent 换一条路走）。
TOOL_PROBES = (
    "python", "python3", "sqlite3", "git", "bc", "node",
    "grep", "sed", "awk", "od", "tr", "cmp", "uname", "which",
    "sort", "head", "tail", "wc", "diff", "find", "xargs",
)

# 系统目录：父 PATH 被清空时（例如从 IDE/服务启动）它们也可能不在，
# 而 `where` / `findstr` / `taskkill` 都在里面。
_WINDOWS_SYSTEM_DIRS = (
    r"C:\Windows\System32",
    r"C:\Windows",
    r"C:\Windows\System32\Wbem",
    r"C:\Windows\System32\WindowsPowerShell\v1.0",
)


def _git_install_roots() -> List[Path]:
    """Git 的安装根，按**不依赖父 PATH** 的顺序找。

    ⚠️ 第一来源必须是注册表，不能是 `shutil.which("git")` —— 后者读的正是
    **父 PATH**，而那恰好是本模块要修的那个变量。实测：把父 PATH 换成只有系统目录
    （模拟从 PowerShell/IDE 启动）后，`which("git")` 找不到 git，
    于是"保证目录"变成空集 —— **修复在最需要它的场景下失效**，
    而所有测试都绿（因为测试进程自己拿着完好的 PATH）。
    """
    roots: List[Path] = []
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                with contextlib.suppress(OSError):
                    with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as handle:
                        value, _ = winreg.QueryValueEx(handle, "InstallPath")
                        if value:
                            roots.append(Path(value))

    # 退路：父 PATH 上的 git（有就用），以及标准安装位置
    found = shutil.which("git")
    if found:
        exe = Path(found).resolve()
        for depth in (2, 3):
            root = exe
            for _ in range(depth):
                root = root.parent
            roots.append(root)
    roots.append(Path(r"C:\Program Files\Git"))
    roots.append(Path(r"C:\Program Files (x86)\Git"))
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(Path(local_appdata) / "Programs" / "Git")
    return roots


def _git_tool_dirs() -> List[str]:
    """找出 Git for Windows 的 `usr\\bin` / `mingw64\\bin` / `cmd`（按固定顺序）。

    ⚠️ Git 有两种可执行文件布局，安装根分别是**往上两级**和**往上三级**：
        `<root>\\cmd\\git.exe`           → 退 2 级
        `<root>\\mingw64\\bin\\git.exe`  → 退 3 级
    只试一种的话，另一种布局会安静地什么都不返回 —— 于是 `sort` / `find`
    会落到 `C:\\Windows\\System32` 下的 **Windows 版**（那里的 `find` 是**文本搜索**，
    不是目录遍历；`sort` 的选项也不同），而轨迹看起来完全正常。
    """
    if os.name != "nt":
        return []

    for root in _git_install_roots():
        dirs = [str(root / sub) for sub in ("usr\\bin", "mingw64\\bin", "cmd")]
        dirs = [d for d in dirs if os.path.isdir(d)]
        if dirs:
            return dirs  # 用第一个有效的安装根，别把多个安装混起来
    return []


def guaranteed_dirs() -> List[str]:
    """**保证要有的**工具目录（固定顺序）。父 PATH 缺失时也由它兜住。"""
    dirs: List[str] = []
    if os.name == "nt":
        dirs.extend(_git_tool_dirs())
        dirs.extend(_WINDOWS_SYSTEM_DIRS)
    return [d for d in dirs if d and os.path.isdir(d)]


def deterministic_path(parent_path: Optional[str] = None) -> str:
    """构造子进程的 PATH：**固定前缀 + 父 PATH + 解释器兜底**。

    :param parent_path: 父进程 PATH；None 表示取 `os.environ["PATH"]`
                        （显式传参是为了可测：测试可以模拟"从 PowerShell 启动"）

    三段的顺序都是有理由的：
    1. **固定前缀**（Git 的两个 bin + 系统目录）在最前 —— 它才是"保证"的部分；
    2. **父 PATH** 接在后面 —— 不能"拿走"任务已经在用的东西
       （例如 `bc` 只在用户 PATH 里，而 `syn_sum_range` 的冒烟测试用过 `bc`）；
    3. **宿主解释器目录放最后** —— 它不是用来"优先"的，而是**救急**：
       父 PATH 里一个 python 都没有时（从服务/IDE 启动）至少还有 python 可用。
       放最后是为了**绝不遮蔽**：正常情况下 `python` 仍来自父 PATH，
       与 `python3` 同一个安装（否则两个名字会指向不同版本，比缺失更难查）。
    """
    if parent_path is None:
        parent_path = os.environ.get("PATH", "")

    interpreter_dir = str(Path(sys.executable).parent) if sys.executable else ""
    parts: List[str] = []
    for item in guaranteed_dirs() + parent_path.split(os.pathsep) + [interpreter_dir]:
        item = item.strip()
        if item and item not in parts:
            parts.append(item)
    return os.pathsep.join(parts)


def environment_fingerprint(path: Optional[str] = None) -> Dict[str, Any]:
    """解析"Agent 会看到什么"，产出可落库的环境指纹。

    :return: `{"digest": 短摘要, "tools": {工具名: 绝对路径或 None}, "path": 生效 PATH}`
    """
    effective = path if path is not None else deterministic_path()
    tools: Dict[str, Optional[str]] = {}
    for name in TOOL_PROBES:
        # 显式传 path：`shutil.which` 默认只看 os.environ，那正是我们要替换掉的东西
        resolved = shutil.which(name, path=effective)
        tools[name] = resolved

    payload = json.dumps({"tools": tools, "path": effective}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
    return {"digest": digest, "tools": tools, "path": effective}


def render_fingerprint(fingerprint: Dict[str, Any]) -> str:
    """人类可读的指纹（`lab sandbox env` 用）。文本优先，可 grep、可进 CI。"""
    tools = fingerprint.get("tools") or {}
    missing = [name for name, resolved in tools.items() if not resolved]
    lines = [
        f"环境指纹：{fingerprint.get('digest', '?')}",
        f"生效 PATH（前 3 项）：{os.pathsep.join((fingerprint.get('path') or '').split(os.pathsep)[:3])}",
        "",
        "工具解析结果：",
    ]
    for name in TOOL_PROBES:
        resolved = tools.get(name)
        lines.append(f"  {'✓' if resolved else '·'} {name:8} {resolved or '（不可用）'}")
    if missing:
        lines += [
            "",
            f"⚠️ 有 {len(missing)} 个探测工具不可用：{'、'.join(missing)}",
            "   这些工具缺失时，模型会花迭代去'探环境'（轨迹里 which/command -v 共出现 110 次），",
            "   而且任务难度会变 —— 这是**测量缺陷**，不是环境差异。",
        ]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    print(render_fingerprint(environment_fingerprint()))
    print(f"\npython 解释器（宿主）：{sys.executable}")
