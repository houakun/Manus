#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sandbox 协议的本地实现 —— fast mode 的"虚拟沙箱"。

== 核心思路（这是 Step 1 最省力的一个决策）==
SUT 的 FileTool / ShellTool 本身**不含任何业务逻辑**，它们只是 `self.sandbox.xxx()` 的薄封装。
因此不需要重写工具，只要实现 Sandbox 协议，SUT 的全部文件/命令能力就能在没有 Docker
的情况下工作 —— 工具代码 100% 原样复用，还避免了"lab 版工具"和"SUT 版工具"行为漂移。

== 两个必须守住的设计 ==
1. **逻辑路径映射**
   SUT 的提示词明确告诉模型"你在一台有互联网的 Linux 沙箱里"，工具描述也是 `/home/ubuntu/...`
   这种绝对路径。如果直接把真实路径（Windows 下是 `D:\\...`）暴露给模型：
     - 破坏提示词假设，模型的行为会变得不可预测；
     - 本地绝对路径会进入模型上下文，既浪费 token 又泄露环境信息。
   所以：所有绝对路径都按"沙箱根"重新映射，返回给模型的永远是逻辑路径。
       /home/ubuntu/a.txt  ->  <workspace>/home/ubuntu/a.txt
   模型始终以为自己在一台 Linux 机器上。

2. **目录穿越防护**
   映射之后必须校验结果仍在 workspace 内。否则模型（或页面里的提示词注入）一句
   `../../../Users/x/.ssh/id_rsa` 就能读写真实机器上的任意文件。
   这里用两道防线：拒绝含 `..` 的路径 + `resolve()` 后再做包含性检查（防符号链接逃逸）。

== 两个"保真"装置（都是实测噪声地板之后加的，见 noise-floor-measured.md）==

3. **虚拟盘符：把脚本内容里的绝对路径也导回工作区**
   字符串替换只作用于命令行，**管不到脚本内容** —— 于是 Agent 写的脚本里
   一句 `/home/ubuntu/sales.db` 就会落到 `<当前盘>:/home/ubuntu`（工作区外）。
   修法不是改写脚本内容（那会把 `D:\\...` 泄进模型上下文，破坏"我在 Linux 上"的假设），
   而是：**给每次运行分配一个 `subst` 虚拟盘符指向工作区**，并让子进程的 cwd 落在该盘上。
   Windows 的无盘符绝对路径是按"**当前盘**"解析的，于是 `/home/ubuntu/x` 自动变成
   `<虚拟盘>:\\home\\ubuntu\\x` —— 正是工作区里的那个位置，脚本一个字都不用改。
   并发安全：每次运行用自己的盘符，`--concurrency > 1` 不会互相抢（这也是它优于
   "在盘根建 junction"的地方 —— 那是全局状态）。

4. **工作区内文本文件的 CRLF 归一为 LF**
   目标环境是 Ubuntu：在那里 `sqlite3 ... > report.txt` 与 Python 文本模式写入
   （`write_text` / `open('w')`）都产出 **LF**。Windows 上原生程序会产出 **CRLF**，
   而模型看到 `\\r\\n` 会**正确地**（对 Linux 而言）去修一个它自己造不出来的问题。
   实测：修复命令输出行尾之后，地板 suite 里**仍有 9.5% 的 shell 调用在追行尾**
   （`od -c` / `cmp` / `tr -d '\\r'`），其中 `sem_csv_clean` 一个任务占 16 次。
   所以命令执行后把工作区内文本文件的 CRLF 归一为 LF（二进制文件按 NUL 字节跳过）。

== 已知限制（Step 1 明确接受，写进文档而不是藏着）==
- `shell_execute` 是"跑完即返回"，没有 SUT 真沙箱那种可交互的**持久 Shell 会话**；
- 命令中的沙箱绝对路径靠**字符串替换**成本地真实路径，复杂命令（heredoc、变量拼接、
  通配符展开）可能替换不到 —— 这是近似模拟，不等价于真沙箱；
  （脚本内容里的绝对路径由上面的虚拟盘符兜住，但**命令里的**复杂 shell 语法仍只是近似）
- **子进程的 PATH 继承自本进程**：从 Git Bash 启动会带上 Git 的 `usr\\bin`
  （于是 `sqlite3` / `grep` / `od` / `tr` 全在），从 PowerShell 启动则全都不在。
  也就是说"Agent 看到的世界"取决于评测是怎么被启动的 —— 这是**已知的、尚未修的可复现性缺陷**
  （证据：同一段命令在两种上下文里 `where sqlite3` 一个成功一个失败），
  修它需要先定"fast mode 保证哪些工具存在"，故未在本轮动。
- 不提供浏览器与网络能力（fast mode 的工具集里已经把浏览器/搜索工具去掉了）。
以上限制决定了 fast mode 适合**计算 / 文件 IO / 文本处理类确定性任务**，
不适合浏览器任务 —— 后者仍需真沙箱，Step 4 的任务集要按这个边界来设计。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import ctypes
import io
import locale
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import weakref
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, List, Optional

from lab.bootstrap import ensure_sut_on_path
from lab.infra.env import deterministic_path, environment_fingerprint

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402

logger = logging.getLogger(__name__)

# ==================== 子进程输出的解码与归一 ====================
#
# == 为什么需要这一层（实测发现，而且是噪声地板的主要来源）==
# 实测噪声地板时，同一份代码两臂的成本差了 ±29%，而主导项是**单个任务**的 +118%。
# 读那条 43 步的轨迹发现，其中 10 步在追行尾：
#
#     od -c report.txt → tr -d '\r' → cmp → od -c → mv → cmp
#
# 根因有两个，**都与模型能力无关，都是本地实现的失真**：
#
# 1. **CRLF**：fast mode 在 Windows 上跑，子进程输出一律是 `\r\n`。
#    SUT 的目标环境是 Ubuntu（提示词里就是这么写的）——**真沙箱不会给 CRLF**。
#    模型在上下文里看到 `\r\n`，合理地以为文件行尾有问题，于是花掉 10 次迭代。
#
# 2. **编码不一致**：这一点最阴。子进程的输出里可能**同时混着两种编码**：
#      - cmd.exe 内建命令的错误信息用 **OEM 代码页**（中文机器上是 GBK）；
#      - 而 Python 子进程被 `_build_child_env` 固定成 UTF-8。
#    一刀切用 UTF-8 解 → 前者的中文变成 `'command' �����ڲ����ⲿ���`，
#    而这堆乱码会**直接进 LLM 上下文**，让它对自己刚才干了什么判断失准。
#
# == 归一为什么是**保真**而不是"掩盖问题" ==
# 目标环境（Ubuntu）本来就不输出 `\r\n`，也不输出 GBK 乱码。
# 把本地实现的产物归一到目标环境的形状，是在**降低测量误差**，而不是在作弊。
# 反过来，把 CRLF 留在上下文里才是真正的失真：它让模型去修一个它自己造出来的问题。
#
# == 算法：逐行解码 ==
# 不整块猜编码，而是按 `\n` 切分后**逐行**试：
#   UTF-8 严格 → 失败则 OEM/本地代码页 → 再失败则 replace。
# 两个理由：
#   a. UTF-8 的多字节序列里**不可能出现 0x0A**，所以按 `\n` 切分是安全的；
#   b. 混合编码流里每行通常只来自一个来源，逐行判定比整块判定准确得多。
# 只去掉行尾的 `\r`，**不动行内的 `\r`` —— 后者是进度条刷新（`\rProgress 50%`），有真实含义。


def _oem_encoding() -> str:
    """本机控制台的 OEM 代码页编码名（Windows 上 cmd.exe 的错误信息用的就是它）。"""
    if sys.platform != "win32":
        return locale.getpreferredencoding(False) or "utf-8"
    with contextlib.suppress(Exception):
        # GetOEMCP 才是控制台的代码页；GetACP 是 ANSI（两者在中文机器上同为 936，但不保证）
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    return "utf-8"


def decode_command_output(raw: Optional[bytes]) -> str:
    """把子进程的原始字节解成**目标环境形状**的文本（UTF-8 + LF）。

    这是模块里唯一该做子进程输出解码的地方 —— 两处输出路径（正常结束 / 超时残留）
    都调它。否则两条路径会长出两种行为，而超时那条本来就很少有人看。
    """
    if not raw:
        return ""
    fallback = _oem_encoding()
    decoded: List[str] = []
    for raw_line in raw.split(b"\n"):
        # 行尾的 \r 是 Windows 行尾的一部分，去掉；行内的 \r（进度条）保留。
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        try:
            decoded.append(raw_line.decode("utf-8"))
            continue
        except UnicodeDecodeError:
            pass
        try:
            decoded.append(raw_line.decode(fallback))
        except (UnicodeDecodeError, LookupError):
            decoded.append(raw_line.decode("utf-8", errors="replace"))
    return "\n".join(decoded)


class SandboxPathError(ValueError):
    """路径越界或非法时抛出。"""


# ==================== 虚拟盘符池（进程级） ====================
#
# 为什么需要"池"而不是"随用随取"：`subst` 映射是**进程外资源** ——
# 它不随 Python 对象消失，也不随进程退出自动消失。于是任何一条不调 `destroy()`
# 的路径（测试、验证器、临时脚本）都会永久占着一个字母。
# 实测：一次 pytest 就把 22 个候选盘符全用光了 —— 而后果是**静默退化**：
# 后续运行拿不到盘符，脚本逃逸又回来了，但没有任何人会发现。
# 所以：池 + 显式释放 + 进程退出兑底 + 回收（仅当映射的目标目录已不存在时）。
# 池里存的是**弱引用**，而不是字符串。
#
# 为什么必须是弱引用："盘符在池里"不等于"它正在被使用" ——
# 一个被丢弃的沙箱对象（测试里最常见：`sandbox = LocalSandbox(...)` 然后函数返回）
# 仍然占着池条目。只看池的话，这些被丢弃的盘符永远回收不了。
# 弱引用能精确区分：`ref() is None` = 对象已被回收 = 这个盘符可以抢。
# （实测：本仓库自己的 5 个测试文件共 18 处构造 LocalSandbox、0 处 destroy，
#  所以"调用方会释放"这个假设在本仓库里就不成立。）
_DRIVE_POOL: Dict[str, "weakref.ReferenceType"] = {}


def _release_all_drives() -> None:
    """进程退出时释放本进程占用的所有虚拟盘（atexit 兑底）。"""
    for letter in list(_DRIVE_POOL):
        with contextlib.suppress(Exception):
            subprocess.run(["subst", letter, "/D"], capture_output=True, timeout=10)
        with contextlib.suppress(Exception):
            (_drive_registry_dir() / f"{letter.rstrip(':')}.pid").unlink(missing_ok=True)
        _DRIVE_POOL.pop(letter, None)


atexit.register(_release_all_drives)


def _drive_registry_dir() -> Path:
    """盘符归属登记目录。

    ⚠️ 刻意放在**系统临时目录**，而不是工作区：工作区里每一个文件模型都可能看到，
    一个莫名的 `.lab-drive` 文件就是新的失真源（而我们的目标恰恰是减少失真）。
    """
    path = Path(tempfile.gettempdir()) / "lab-drive-registry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_alive(pid: int) -> bool:
    """该进程是否还活着（用于判断别的进程留下的映射能不能回收）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _drive_owner(drive: str) -> Optional[int]:
    """读该盘符登记的属主 pid；没有登记返回 None。"""
    with contextlib.suppress(Exception):
        return int((_drive_registry_dir() / f"{drive.rstrip(':')}.pid").read_text().strip())
    return None


def _drive_is_reclaimable(drive: str, target: str) -> bool:
    """该映射能不能被回收（**绝不能抢活着的别的进程的盘符**）。

    三种可回收情形：
    1. 目标目录不存在 → 工作区已被删，一定是陈旧的；
    2. 没有归属登记 → 不是活着的 lab 进程建的；
    3. 属主是本进程：池里没有记录、或弱引用已死（沙箱对象被丢弃）→ 可回收；
       属主是**别的**进程且它已经死了 → 被强杀留下的。
    """
    if not target or not os.path.exists(target):
        return True
    owner = _drive_owner(drive)
    if owner is None:
        return True
    if owner == os.getpid():
        ref = _DRIVE_POOL.get(drive)
        # 池里没记录 → 不是本进程现在占着的；弱引用已死 → 沙箱对象被丢弃了
        return ref is None or ref() is None
    return not _pid_alive(owner)


def list_drive_mappings() -> List[Dict[str, str]]:
    """列出当前所有 `subst` 映射（`letter -> target`）。供 CLI 与回收使用。"""
    if os.name != "nt":
        return []
    with contextlib.suppress(Exception):
        done = subprocess.run(["subst"], capture_output=True, text=True, timeout=10, errors="replace")
        mappings: List[Dict[str, str]] = []
        for line in (done.stdout or "").splitlines():
            # 形如 `Z:\: => C:\path\to\ws`
            if "=>" not in line:
                continue
            left, _, right = line.partition("=>")
            # subst 的输出形如 `Z:\: => C:\path`（盘符后面带一个 `\:`），
            # 所以只能取冒号**前**的那一段当盘符 —— 直接 rstrip 会得到 `Z:\:`。
            letter = left.strip().split(":")[0].strip() + ":"
            mappings.append({"drive": letter, "target": right.strip()})
        return mappings
    return []


def escape_roots() -> List[Path]:
    """列出"脚本逃逸"可能写入的根目录（即脚本里写 `/home/ubuntu/x` 会落到的位置）。

    为什么需要它：沙箱的路径映射只作用于文件工具与命令行，**管不到脚本内容**。
    于是 Agent 写的脚本一旦用绝对路径，产物就会落到 `<当前盘>:/home/ubuntu`。
    实测这是**常态而不是边缘情况**：一次真实的 semirefactor 运行里
    Agent 往 D:/home/ubuntu/parts/ 写了 24 个分片文件，
    另一次往同一个地方下了两个 5MB 的 zip。

    这些文件在工作区之外，不会随任务清理，会一直累积（实测一天就积了 15MB）。
    所以清理必须是**可重复的命令**，而不是一次性手工删除。
    """
    if os.name != "nt":
        root = Path("/home/ubuntu")
        return [root] if root.exists() else []

    drives: List[str] = []
    lister = getattr(os, "listdrives", None)  # Python 3.12+
    if callable(lister):
        try:
            drives = [str(item) for item in lister()]
        except OSError:
            drives = []
    if not drives:
        drives = [f"{letter}:\\" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"]

    found: List[Path] = []
    for drive in drives:
        candidate = Path(drive) / "home" / "ubuntu"
        try:
            if candidate.exists():
                found.append(candidate)
        except OSError:
            continue
    return found


def describe_escape_roots() -> List[Dict[str, Any]]:
    """清点逃逸产物（文件数 + 总字节数），供 CLI 与报告使用。"""
    inventory: List[Dict[str, Any]] = []
    for root in escape_roots():
        total = 0
        count = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    count += 1
                    total += path.stat().st_size
            except OSError:
                continue
        inventory.append({"path": str(root), "files": count, "bytes": total})
    return inventory


class LocalSandbox:
    """Sandbox 协议的本地实现（结构化类型，无需显式继承）。"""
    # 与 SUT 提示词保持一致：模型认为自己在家目录 /home/ubuntu 下工作
    SANDBOX_HOME = "/home/ubuntu"
    SANDBOX_TMP = "/tmp"

    # 传给子进程的环境变量白名单（见 _build_child_env 的注释说明为什么不能直接用 os.environ）
    _ENV_ALLOWLIST = {
        # 进程执行必需
        "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
        # 临时目录
        "TEMP", "TMP", "TMPDIR",
        # 家目录（部分工具依赖）
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        # 区域设置
        "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE",
    }

    def __init__(
            self,
            workspace: Path,
            *,
            exec_timeout: int = 60,
            max_output_chars: int = 32_000,
            extra_env: Optional[Dict[str, str]] = None,
    ) -> None:
        """构造函数。

        :param workspace: 本次任务专属的本地目录，充当"沙箱文件系统根"
        :param exec_timeout: 单条命令的超时秒数（硬限制，防止模型跑死循环命令卡住评测）
        :param max_output_chars: 单次命令输出上限，超出截断（防止超长输出撑爆上下文）
        :param extra_env: 额外放行给子进程的环境变量（如评测确实需要 HTTP_PROXY 时显式传入）
        """
        self._root = Path(workspace).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._exec_timeout = exec_timeout
        self._max_output_chars = max_output_chars
        self._extra_env = dict(extra_env or {})
        # 虚拟盘符（见模块文档第 3 条）：None 表示没有/不可用，此时退化为旧行为。
        self._drive: Optional[str] = None
        # CRLF 归一（见模块文档第 4 条）。可用环境变量关掉，以便做 A/B 与排查。
        self._normalize_crlf = os.environ.get("LAB_SANDBOX_NORMALIZE_CRLF", "1") not in ("0", "false", "")
        self._use_virtual_drive = os.environ.get("LAB_SANDBOX_VIRTUAL_DRIVE", "1") not in ("0", "false", "")
        # shell 是"跑完即返回"，这里按 session_id 记住最后一次的输出/返回码，
        # 以便 shell_read_output / shell_wait_process 这两个工具仍能给出合理结果。
        self._session_outputs: Dict[str, str] = {}
        self._session_returncodes: Dict[str, int] = {}

    # ==================== 路径映射与防护 ====================
    def _to_local(self, logical_path: str) -> Path:
        """把逻辑路径（/home/ubuntu/a.txt）映射为本地真实路径。"""
        text = str(logical_path).replace("\\", "/")
        raw = PurePosixPath(text)

        # 1.明确拒绝上跳：宁可报错，也不要"悄悄改写"成一个别的路径（更难排查）
        if ".." in raw.parts:
            raise SandboxPathError(f"路径包含上跳符，已拒绝: {logical_path}")

        # 2.剥掉根标记与 Windows 盘符（C: / D:），只保留相对部分
        parts = [
            p for p in raw.parts
            if p not in ("/", ".", "") and not p.endswith(":")
        ]
        candidate = self._root.joinpath(*parts).resolve() if parts else self._root

        # 3.第二道防线：resolve() 之后必须仍在 workspace 内（防符号链接逃逸）
        if candidate != self._root and self._root not in candidate.parents:
            raise SandboxPathError(f"路径越界，已拒绝: {logical_path}")
        return candidate

    def _to_logical(self, local_path: Path) -> str:
        """把本地真实路径还原成逻辑路径，保证返回给模型的世界观一致。"""
        relative = Path(local_path).resolve().relative_to(self._root)
        return "/" + relative.as_posix()

    # ==================== 虚拟盘符（把脚本里的绝对路径导回工作区） ====================
    # 分配用的候选盘符：从后往前找，尽量不占用用户常用的靠前盘符。
    _DRIVE_CANDIDATES = "ZYXWVUTSRQPONMLKJIHGFED"

    def _ensure_virtual_drive(self) -> Optional[str]:
        """为本次运行分配一个 `subst` 虚拟盘符，指向工作区根。

        为什么需要它（见模块文档第 3 条）：命令行的字符串替换管不到**脚本内容**，
        而 Windows 的无盘符绝对路径按"当前盘"解析 —— 只要子进程的当前盘是这个
        虚拟盘，`/home/ubuntu/x` 就自动落进工作区，且脚本一个字都不用改。

        失败时返回 None 并退化为旧行为（不做任何事，也不报错）：
        `subst` 可能被组策略禁用，或候选盘符被占满 —— 那时只是逃逸检测继续告警，
        而不是让整个评测跑不起来。
        """
        if os.name != "nt" or not self._use_virtual_drive:
            return None
        if self._drive:
            return self._drive

        # 1. 先找空闲字母。
        for letter in self._DRIVE_CANDIDATES:
            drive = f"{letter}:"
            try:
                if os.path.exists(drive + "\\"):
                    continue  # 已被真实分区或别人的 subst 占用
                if self._try_claim(drive):
                    return self._drive
            except Exception:
                continue

        # 2. 没有空闲字母：回收陈旧映射。
        #    ⚠️ 判据必须是"属主已死/无属主"，不能只看"目标目录还在不在" ——
        #    目标还在的映射可能是**另一个并发进程**正在用的，
        #    抢它会把它正在跑的任务写到我们的工作区里（而且极难归因）。
        for item in list_drive_mappings():
            drive, target = item["drive"], item["target"]
            if not _drive_is_reclaimable(drive, target):
                continue
            with contextlib.suppress(Exception):
                subprocess.run(["subst", drive, "/D"], capture_output=True, timeout=10)
            _DRIVE_POOL.pop(drive, None)
            if self._try_claim(drive):
                logger.info(f"回收了虚拟盘 {drive}（原目标：{target or '未知'}）")
                return self._drive

        logger.warning(
            "未能分配 subst 虚拟盘（候选盘符都被活着的进程占用）：脚本内容里的 "
            "/home/ubuntu 绝对路径仍会落到工作区外（逃逸检测会告警，但产物不在工作区）。"
            "用 `subst` 查看映射，`subst <盘符>: /D` 手工清理。"
        )
        return None

    def _try_claim(self, drive: str) -> bool:
        """尝试把一个盘符映射到工作区；成功则记入池与实例。"""
        with contextlib.suppress(Exception):
            done = subprocess.run(
                ["subst", drive, str(self._root)],
                capture_output=True, text=True, timeout=10, errors="replace",
            )
            if done.returncode == 0 and os.path.exists(drive + "\\"):
                self._drive = drive
                _DRIVE_POOL[drive] = weakref.ref(self)
                # 登记属主：别的进程靠它判断"这个映射还能不能抢"
                with contextlib.suppress(Exception):
                    (_drive_registry_dir() / f"{drive.rstrip(':')}.pid").write_text(str(os.getpid()))
                logger.debug(f"已为本次运行分配虚拟盘 {drive} -> {self._root}")
                return True
        return False

    def _release_virtual_drive(self) -> None:
        """释放虚拟盘符。不释放会一直占着字母，直到重启或手工 `subst /D`。"""
        if not self._drive:
            return
        with contextlib.suppress(Exception):
            subprocess.run(["subst", self._drive, "/D"], capture_output=True, timeout=10)
        with contextlib.suppress(Exception):
            (_drive_registry_dir() / f"{self._drive.rstrip(':')}.pid").unlink(missing_ok=True)
        _DRIVE_POOL.pop(self._drive, None)
        self._drive = None

    def _to_virtual(self, local_path: Path) -> str:
        """把工作区内的真实路径换成"虚拟盘视图"，用于给子进程设 cwd。

        必须走虚拟盘（而不是真实路径）的原因：**当前盘**由 cwd 决定，
        而当前盘正是无盘符绝对路径的解析基准。用真实路径当 cwd 的话，
        `/home/ubuntu/x` 又会落回真实盘根。
        """
        if not self._drive:
            return str(local_path)
        try:
            relative = Path(local_path).resolve().relative_to(self._root)
        except ValueError:
            return str(local_path)
        return f"{self._drive}\\" + str(relative).replace("/", "\\")

    # ==================== CRLF 归一（保真，见模块文档第 4 条） ====================
    # 单文件大小上限：超过就不动（避免为了归一去读一个几百 MB 的产物）
    _MAX_NORMALIZE_BYTES = 2_000_000
    # 单次命令最多归一多少个文件（防 Agent 造出成千上万个文件时把时间花在这里）
    _MAX_NORMALIZE_FILES = 200

    def _normalize_workspace_line_endings(self) -> List[str]:
        """把工作区内**文本**文件的 CRLF 归一为 LF，返回被改动的逻辑路径。

        判据与取舍：
        - 只看工作区内部（绝不碰工作区外 —— 那里的东西本来就该被检测为逃逸）；
        - 含 NUL 字节的按二进制跳过（数据库、zip、图片都不能动）；
        - 只处理 `\r\n`，不动单独的 `\r`（后者可能是进度条等有含义的内容）；
        - 超过大小上限的跳过。

        为什么这不是"篡改产物"：目标环境 Ubuntu 本来就不产出 CRLF，
        归一是在让本地产物与目标环境一致（与 `decode_command_output` 同一理由）。
        """
        changed: List[str] = []
        for path in self._root.rglob("*"):
            if len(changed) >= self._MAX_NORMALIZE_FILES:
                break
            try:
                if not path.is_file():
                    continue
                if path.stat().st_size > self._MAX_NORMALIZE_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\r\n" not in data or b"\x00" in data:
                continue
            try:
                path.write_bytes(data.replace(b"\r\n", b"\n"))
            except OSError:
                continue
            changed.append(self._to_logical(path))
        return changed

    # ==================== 结果构造小工具 ====================

    @staticmethod
    def _fail(
            message: str,
            error_type: str = "sandbox_error",
            **data: Any,
    ) -> ToolResult:
        """构造失败结果。统一带上 error_type，便于上层做重试与归因。"""
        return ToolResult(
            success=False,
            message=message,
            data=data or None,
            error_type=error_type,
            retryable=False,
        )

    # ==================== Sandbox 协议：属性与生命周期 ====================

    @property
    def id(self) -> str:
        return f"local-{abs(hash(str(self._root))) % 10 ** 8:08d}"

    async def ensure_sandbox(self) -> None:
        """确保沙箱"已启动"。本地实现只需确认目录存在，不做任何容器操作。"""
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "home" / "ubuntu").mkdir(parents=True, exist_ok=True)
        (self._root / "tmp").mkdir(parents=True, exist_ok=True)
        # 虚拟盘要在建好目录之后再挂：它映射的是工作区根，目录不存在时 subst 仍会成功，
        # 但后续 `X:\home\ubuntu` 这种 cwd 会因为目录缺失而失败。
        self._ensure_virtual_drive()

    async def destroy(self) -> bool:
        """销毁沙箱。**刻意不删除 workspace**：产物是评测证据，必须保留供人工检查。"""
        self._session_outputs.clear()
        self._session_returncodes.clear()
        # 虚拟盘必须显式释放：它不随进程退出消失，会一直占着字母直到重启。
        self._release_virtual_drive()
        return True

    async def get_browser(self):
        """fast mode 不提供浏览器。"""
        raise NotImplementedError(
            "LocalSandbox 不提供浏览器：fast mode 的工具集里没有 BrowserTool。"
            "需要浏览器能力请使用 DockerSandbox（真沙箱）。"
        )

    # ==================== 文件操作 ====================

    async def read_file(
            self,
            filepath: str,
            start_line: Optional[int] = None,
            end_line: Optional[int] = None,
            sudo: bool = False,
            max_length: int = 10000,
    ) -> ToolResult:
        """读取文件内容，支持按行截取与长度截断。"""
        try:
            local = self._to_local(filepath)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", filepath=filepath)

        if not local.exists():
            return self._fail(f"文件不存在: {filepath}", error_type="file_not_found", filepath=filepath)
        if local.is_dir():
            return self._fail(f"路径是目录而不是文件: {filepath}", error_type="invalid_path", filepath=filepath)

        try:
            content = local.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return self._fail(f"读取文件失败: {e}", error_type="io_error", filepath=filepath)

        # 1.按行截取（start_line 含、end_line 不含，与 SUT 沙箱语义一致）
        if start_line is not None or end_line is not None:
            lines = content.split("\n")
            start = start_line or 0
            end = end_line if end_line is not None else len(lines)
            content = "\n".join(lines[start:end])

        # 2.长度截断：防止一个超大文件把上下文撑爆
        truncated = False
        if max_length and len(content) > max_length:
            content = content[:max_length]
            truncated = True

        return ToolResult(
            success=True,
            message=f"读取成功: {filepath}" + ("（内容已截断）" if truncated else ""),
            data={"filepath": filepath, "content": content, "truncated": truncated},
        )

    async def write_file(
            self,
            filepath: str,
            content: str,
            append: bool = False,
            leading_newline: bool = False,
            trailing_newline: bool = False,
            sudo: bool = False,
    ) -> ToolResult:
        """写入文件（支持追加模式，append=True 时是**非幂等**操作）。"""
        try:
            local = self._to_local(filepath)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", filepath=filepath)

        text = content
        if leading_newline:
            text = "\n" + text
        if trailing_newline:
            text = text + "\n"

        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            # newline="" 保持内容原样，避免平台相关的换行转换影响评测断言
            with open(local, "a" if append else "w", encoding="utf-8", newline="") as fp:
                fp.write(text)
        except Exception as e:
            return self._fail(f"写入文件失败: {e}", error_type="io_error", filepath=filepath)

        return ToolResult(
            success=True,
            message=f"{'追加' if append else '写入'}成功: {filepath}",
            data={"filepath": filepath, "bytes_written": len(text.encode("utf-8"))},
        )

    async def replace_in_file(
            self,
            filepath: str,
            old_str: str,
            new_str: str,
            sudo: bool = False,
    ) -> ToolResult:
        """按字符串替换文件内容。"""
        try:
            local = self._to_local(filepath)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", filepath=filepath)

        if not local.exists():
            return self._fail(f"文件不存在: {filepath}", error_type="file_not_found", filepath=filepath)

        try:
            content = local.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return self._fail(f"读取文件失败: {e}", error_type="io_error", filepath=filepath)

        replaced_count = content.count(old_str)
        if replaced_count == 0:
            # 找不到目标内容属于"确定性失败"，重试没有意义
            return self._fail(
                f"未在文件中找到要替换的内容: {old_str[:80]}",
                error_type="not_found",
                filepath=filepath,
            )

        try:
            local.write_text(content.replace(old_str, new_str), encoding="utf-8", newline="")
        except Exception as e:
            return self._fail(f"写入文件失败: {e}", error_type="io_error", filepath=filepath)

        return ToolResult(
            success=True,
            message=f"替换成功，共替换 {replaced_count} 处: {filepath}",
            data={"filepath": filepath, "replaced_count": replaced_count},
        )

    async def search_in_file(self, filepath: str, regex: str, sudo: bool = False) -> ToolResult:
        """按正则搜索文件内容，返回匹配行内容与行号。"""
        try:
            local = self._to_local(filepath)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", filepath=filepath)

        if not local.exists():
            return self._fail(f"文件不存在: {filepath}", error_type="file_not_found", filepath=filepath)

        try:
            pattern = re.compile(regex)
        except re.error as e:
            return self._fail(f"正则表达式非法: {e}", error_type="invalid_argument", filepath=filepath)

        try:
            content = local.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return self._fail(f"读取文件失败: {e}", error_type="io_error", filepath=filepath)

        matches: List[str] = []
        line_numbers: List[int] = []
        for index, line in enumerate(content.split("\n")):
            if pattern.search(line):
                matches.append(line)
                line_numbers.append(index)

        if not matches:
            return self._fail(
                f"未匹配到任何内容: {regex}",
                error_type="not_found",
                filepath=filepath,
                matches=[],
                line_numbers=[],
            )

        return ToolResult(
            success=True,
            message=f"匹配到 {len(matches)} 行",
            data={"filepath": filepath, "matches": matches, "line_numbers": line_numbers},
        )

    async def find_files(self, dir_path: str, glob_pattern: str) -> ToolResult:
        """按 glob 模式查找目录下的文件，返回逻辑路径列表。"""
        try:
            local_dir = self._to_local(dir_path)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", dir_path=dir_path)

        if not local_dir.exists() or not local_dir.is_dir():
            return self._fail(f"目录不存在: {dir_path}", error_type="file_not_found", dir_path=dir_path)

        pattern = glob_pattern or "*"
        try:
            found = sorted(
                self._to_logical(item)
                for item in local_dir.glob(pattern)
                if item.is_file()
            )
        except Exception as e:
            return self._fail(f"查找文件失败: {e}", error_type="io_error", dir_path=dir_path)

        return ToolResult(
            success=True,
            message=f"找到 {len(found)} 个文件",
            data={"dir_path": dir_path, "files": found},
        )

    async def list_files(self, dir_path: str) -> ToolResult:
        """列出目录下的文件（与 SUT 沙箱一致，等价于 find_files('*')）。"""
        return await self.find_files(dir_path, "*")

    async def check_file_exists(self, filepath: str) -> ToolResult:
        try:
            local = self._to_local(filepath)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", filepath=filepath)
        return ToolResult(
            success=True,
            message=f"文件{'存在' if local.exists() else '不存在'}: {filepath}",
            data={"filepath": filepath, "exists": local.exists()},
        )

    async def delete_file(self, filepath: str) -> ToolResult:
        try:
            local = self._to_local(filepath)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", filepath=filepath)
        if not local.exists():
            return self._fail(f"文件不存在: {filepath}", error_type="file_not_found", filepath=filepath)
        try:
            if local.is_dir():
                import shutil

                shutil.rmtree(local)
            else:
                local.unlink()
        except Exception as e:
            return self._fail(f"删除失败: {e}", error_type="io_error", filepath=filepath)
        return ToolResult(success=True, message=f"删除成功: {filepath}", data={"filepath": filepath, "deleted": True})

    async def upload_file(
            self,
            file_data: BinaryIO,
            filepath: str,
            filename: str = None,
    ) -> ToolResult:
        """把二进制流写入沙箱路径（用于把用户附件同步进沙箱）。"""
        try:
            local = self._to_local(filepath)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", filepath=filepath)
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            payload = file_data.read()
            local.write_bytes(payload)
        except Exception as e:
            return self._fail(f"上传文件失败: {e}", error_type="io_error", filepath=filepath)
        return ToolResult(
            success=True,
            message=f"上传成功: {filepath}",
            data={"filepath": filepath, "file_size": len(payload), "success": True},
        )

    async def download_file(self, filepath: str) -> BinaryIO:
        """读取文件为二进制流。"""
        local = self._to_local(filepath)
        return io.BytesIO(local.read_bytes())

    # ==================== Shell 操作 ====================

    def _rewrite_paths(self, command: str) -> str:
        """把命令里出现的沙箱逻辑路径替换成本地真实路径。

        例（Windows）:
            "python /home/ubuntu/sum.py"  ->  "python D:\\...\\home\\ubuntu\\sum.py"

        为什么必须做：模型会理所当然地使用提示词里教的绝对路径，
        而本地机器上并不存在 /home/ubuntu。
        限制：纯字符串替换，不解析 shell 语法 —— 见模块文档的"已知限制"。

        ⚠️ 有虚拟盘时必须换成**虚拟盘上的绝对路径**（`Z:\\home\\ubuntu`），
        不能继续用工作区的真实路径（`D:\\...\\home\\ubuntu`）：
        子进程的当前盘是虚拟盘，而 cmd 的 `cd D:\\x` **不会切换盘符**（要 `cd /d`），
        于是 `cd D:\\...\\docs && mv a.txt a.md` 里的 mv 仍跑在原目录 ——
        实测：`bench validate` 里 syn_rename_files 的参考解就是这么挂的
        （报错 `mv: cannot stat 'a.txt'`，看起来像任务坏了，其实是盘符不一致）。
        """
        if self._drive:
            home_real = f"{self._drive}\\home\\ubuntu"
            tmp_real = f"{self._drive}\\tmp"
        else:
            home_real = str(self._root / "home" / "ubuntu")
            tmp_real = str(self._root / "tmp")
        return command.replace(self.SANDBOX_HOME, home_real).replace(self.SANDBOX_TMP, tmp_real)

    def _build_child_env(self) -> Dict[str, str]:
        """构造子进程的环境变量（白名单 + 显式覆盖）。

        为什么不能直接 `os.environ` 传给子进程 —— 两个真实问题：

        1) **安全（更严重）**：真沙箱里模型是看不到宿主机环境的。
           本地实现如果直接继承，模型一条 `env` / `echo $LAB_LLM_API_KEY` 就能读到
           宿主机凭据（LLM Key、云厂商 Key、代理密码……）——
           这等于把"页面里的提示词注入"直接升级成"凭据泄露"。所以只放行白名单。

        2) **正确性**：宿主机的 PYTHONHOME / PYTHONPATH 会污染子进程解释器。
           实测本机 PYTHONHOME 指向 uv 的 3.12 而 PATH 里的 python 是 3.13，
           结果 `python -c "..."` 直接抛 `AssertionError: SRE module mismatch`。
           危险的地方在于：**模型会以为是自己的命令写错了**，
           于是花好几个迭代去"修复"一个根本不属于它的问题。

        另外固定 PYTHONIOENCODING/PYTHONUTF8，保证子进程 Python 输出 UTF-8，
        与这里的 decode("utf-8") 对齐（Windows 下默认编码是 GBK，会出现乱码）。
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in self._ENV_ALLOWLIST
        }
        # PATH **不能原样继承**：实测同一段命令从 Git Bash 启动能看到
        # sqlite3/grep/od/tr，从 PowerShell 启动则全部看不到（连 python 都没有）——
        # 也就是说"Agent 看到的世界"取决于评测是怎么被启动的。
        # 改成"固定前缀 + 父 PATH 兜底"，详见 lab/infra/env.py 的模块文档。
        env["PATH"] = deterministic_path(env.get("PATH"))
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # extra_env 仍然放在最后：调用方显式传 PATH 时必须能覆盖（实验/对照用），
        # 而指纹会如实反映覆盖后的结果。
        env.update(self._extra_env)
        return env

    def env_fingerprint(self) -> Dict[str, Any]:
        """本次运行 Agent 实际会看到的环境（工具面 + 可 GROUP BY 的短摘要）。

        为什么要落库：噪声地板宽到 ±24% 时，"这次跑的时候 Agent 看到了什么"
        必须能查 —— 否则换个 shell 启动就能让两次跑不可比，而报告里看不出来。
        """
        return environment_fingerprint(self._build_child_env().get("PATH", ""))

    # 工作区外快照的条目上限（防止 Agent 下载了一个大目录树时把内存吃光）
    _MAX_OUTSIDE_ENTRIES = 500

    def _snapshot_outside(self) -> Dict[str, float]:
        """记录工作区外那个位置的 (relative path → mtime) 快照。

        为什么要快照而不是"看目录存不存在"：
        初版检测器只判断 `Path("D:/home/ubuntu").exists()`，结果一旦某个脚本
        创建过它，**之后每条命令都会告警** —— 实测一次 40 运行的评测里报了 98 次，
        几乎全是噪声。噪声化的安全告警比没有告警更糟：它会训练人忽略它。
        改成比对前后快照，只报"本次命令**新增或改写了**什么"。
        """
        root = self._escaped_root()
        if root is None or not root.exists():
            return {}
        snapshot: Dict[str, float] = {}
        try:
            for path in root.rglob("*"):
                if len(snapshot) >= self._MAX_OUTSIDE_ENTRIES:
                    break
                try:
                    if path.is_file():
                        snapshot[str(path.relative_to(root))] = path.stat().st_mtime
                except OSError:
                    continue
        except OSError:
            return snapshot
        return snapshot

    def _detect_escaped_writes(self, before: Dict[str, float]) -> List[str]:
        """本次命令在工作区外新增/改写了哪些文件（见 _snapshot_outside 的说明）。"""
        after = self._snapshot_outside()
        return sorted(name for name, mtime in after.items() if before.get(name) != mtime)[:20]

    def _escaped_root(self) -> Optional[Path]:
        """返回一个脚本里写 `/home/ubuntu/...` 时会落到的**真实**位置。

        == 为什么需要这个（实测发生的事故）==
        沙箱的路径映射只作用于两处：
          1. 文件工具（read_file/write_file ...）
          2. 命令行字符串（_rewrite_paths 把 /home/ubuntu 换成真实路径）
        **脚本内容它管不到**。于是当 Agent（或参考解）写一个 Python 脚本、
        脚本里用 "/home/ubuntu/x" 时，Python 会把它解析成
        `<当前盘>:/home/ubuntu/x` —— 直接写到工作区**外面**。

        实测：`D:\\home\\ubuntu` 里堆了十几个文件（config.json / days.txt /
        fizzbuzz.txt / parts/part_000...），它们都是逃跑的产物。

        == 双重危害 ==
        1. **正确性**：产物没落在工作区 → 判定器读不到 → 正确的工作被判失败；
        2. **安全**：这意味着 fast mode 的"沙箱"可以被任意脚本逃出 ——
           它只能用于**可信的任务与内容**，绝不能跑外部输入。

        == 现在的处置（不再是"只告警"）==
        脚本内容里的绝对路径由**虚拟盘符**导回工作区（见模块文档第 3 条）：
        子进程的当前盘指向工作区，于是 `/home/ubuntu/x` 自动解析成
        `<虚拟盘>:\\home\\ubuntu\\x`。所以正常情况下这里**不应该再告警**；
        保留它是因为 subst 可能不可用（组策略/盘符占满），那时它仍是唯一的发现手段 ——
        而"产物找不到"这类问题必须能一眼归因，而不是让人去怀疑判定器或模型能力。
        """
        if os.name == "nt":
            # Windows：无盘符的绝对路径按"当前盘"解析
            drive = self._root.drive or "C:"
            return Path(f"{drive}/home/ubuntu")
        return Path("/home/ubuntu")

    async def exec_command(self, session_id: str, exec_dir: str, command: str) -> ToolResult:
        """执行命令并等待结束（非交互式），返回 returncode 与合并后的输出。"""
        # 懒加载虚拟盘：正常生命周期里 ensure_sandbox 已经挂好了，但验证器等其他
        # 入口也可能直接跑命令 —— 挂盘是幂等的，这里再确认一次，代价只是一次属性判断。
        self._ensure_virtual_drive()
        # 0.执行前先给"工作区外"拍个快照，用于检测脚本逃逸（见 _snapshot_outside）
        outside_before = self._snapshot_outside()
        # 1.解析工作目录（不存在就创建，避免因为目录问题浪费一次迭代）
        try:
            cwd = self._to_local(exec_dir or self.SANDBOX_HOME)
        except SandboxPathError as e:
            return self._fail(str(e), error_type="path_escape", session_id=session_id, command=command)
        with contextlib.suppress(Exception):
            cwd.mkdir(parents=True, exist_ok=True)
        if not cwd.exists():
            cwd = self._root / "home" / "ubuntu"
            cwd.mkdir(parents=True, exist_ok=True)

        real_command = self._rewrite_paths(command)
        # cwd 走"虚拟盘视图"：当前盘由 cwd 决定，而无盘符绝对路径按当前盘解析。
        # 用真实路径当 cwd 的话，脚本里的 `/home/ubuntu/x` 会落回真实盘根。
        cwd_virtual = self._to_virtual(cwd)

        # 2.启动子进程（shell=True：模型给的是 shell 命令，不是 argv 列表）
        #   POSIX 用 start_new_session / Windows 用 CREATE_NEW_PROCESS_GROUP，
        #   目的是把整条命令放进独立进程组，超时时才能"按组"连根杀掉（见 _kill_process_tree）。
        spawn_kwargs = {}
        if sys.platform == "win32":
            spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            spawn_kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_shell(
                real_command,
                cwd=cwd_virtual,
                env=self._build_child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **spawn_kwargs,
            )
        except Exception as e:
            return self._fail(f"命令启动失败: {e}", error_type="sandbox_error", session_id=session_id, command=command)

        # 3.硬超时：这是"终止保证"的最小实现（Step 3 会把它升级成预算中间件）
        try:
            raw_output, _ = await asyncio.wait_for(process.communicate(), timeout=self._exec_timeout)
        except asyncio.TimeoutError:
            # 关键：必须连根杀掉**整棵进程树**（见下面的注释）
            await self._kill_process_tree(process)
            # 进程树死后管道会 EOF，再取一次残余输出（拿不到也不影响结论）
            with contextlib.suppress(Exception):
                raw_output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            partial = decode_command_output(raw_output)
            if partial:
                self._session_outputs[session_id] = partial
            return ToolResult(
                success=False,
                message=f"命令执行超时({self._exec_timeout}s)，已强制终止: {command}",
                error_type="timeout",
                # retryable 只表示"失败类型是暂态"，**不表示"应该重试"**。
                # 是否真的重试由中间件结合「工具幂等性」判定：shell_execute 非幂等，
                # 所以它不会被自动重试（否则命令可能已经产生一半副作用）。
                retryable=True,
                data={
                    "session_id": session_id,
                    "command": command,
                    "status": "timeout",
                    "returncode": None,
                    "output": partial,
                },
            )

        output = decode_command_output(raw_output)
        if len(output) > self._max_output_chars:
            output = output[: self._max_output_chars] + "\n...[输出过长已截断]"

        self._session_outputs[session_id] = output
        self._session_returncodes[session_id] = process.returncode or 0

        # 注意：这里 success 表示"命令成功执行完"，与 SUT 真沙箱语义一致
        #（真沙箱只要 HTTP 200 就 success=True，returncode 放在 data 里）。
        # 非零返回码不应该被吞掉，因此 message 里显式提示，供模型判断。
        hint = "" if process.returncode == 0 else f"（返回码 {process.returncode}，命令执行失败）"

        # 逃逸检测：如果脚本用了 /home/ubuntu 绝对路径，产物会落在工作区外面。
        # 把它写进返回结果与日志，让"产物找不到"这类问题一眼能归因，
        # 而不是让人去怀疑判定器或模型能力。
        escaped = self._detect_escaped_writes(outside_before)
        if escaped:
            logger.warning(
                f"检测到工作区外写入（脚本里可能用了 /home/ubuntu 绝对路径）："
                f"本次新增/修改 {len(escaped)} 个文件，例如 {escaped[0]}。"
                f"沙箱路径映射只作用于文件工具与命令行，管不到脚本内容。"
            )

        # 归一工作区内文本文件的行尾（见模块文档第 4 条）。
        # ⚠️ 只写进 data，**不能写进 message** —— message 是给模型看的，
        # 一句"已归一 CRLF"就等于告诉它"你其实在 Windows 上"，反而制造新的失真。
        normalized = self._normalize_workspace_line_endings() if self._normalize_crlf else []
        if normalized:
            logger.debug(f"已把 {len(normalized)} 个工作区文件的 CRLF 归一为 LF（目标环境 Ubuntu 不产出 CRLF）")

        return ToolResult(
            success=True,
            message=f"命令执行完成{hint}" + ("（检测到工作区外写入）" if escaped else ""),
            data={
                "session_id": session_id,
                "command": command,
                "status": "completed",
                "returncode": process.returncode,
                "output": output,
                "escaped_paths": escaped,
                "normalized_line_endings": normalized,
            },
        )

    @staticmethod
    async def _kill_process_tree(process) -> None:
        """杀掉整棵进程树（不是只杀 shell 本身）。

        == 为什么必须杀树（真实踩过的坑）==
        命令是交给 shell 执行的，shell 会再 fork 出真正的程序。
        只 `process.kill()` 只能杀死 shell，真正的子进程会变成孤儿继续跑满整个超时时长。
        后果比"难看"严重得多：
          - 评测以为任务已经超时终止，机器 CPU 却还被占着；
          - 并行跑任务集时，这些孤儿进程会互相抢资源，
            **污染后面任务的耗时指标**（这是最阴险的地方：数据错了但看不出来）。
        典型症状：一个 2 秒超时的用例，整个测试套件却跑了 60 秒。
        """
        if process.returncode is not None:
            return

        if sys.platform == "win32":
            # Windows 没有进程组信号语义，用 taskkill /T 递归结束整棵树
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(process.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=10)
        else:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        # 兜底：无论如何再直接杀一次本进程，并等它退出
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=10)

    async def read_shell_output(self, session_id: str, console: bool = False) -> ToolResult:
        """读取某次命令的输出（fast mode 下命令是同步跑完的，这里回放最后一次输出）。"""
        if session_id not in self._session_outputs:
            return self._fail(
                f"未找到 shell 会话: {session_id}",
                error_type="session_not_found",
                session_id=session_id,
            )
        return ToolResult(
            success=True,
            message="读取 shell 输出成功",
            data={
                "session_id": session_id,
                "output": self._session_outputs[session_id],
                "console_records": [],
            },
        )

    async def wait_process(self, session_id: str, seconds: Optional[int] = None) -> ToolResult:
        """命令已同步执行完毕，直接返回最后一次的返回码。"""
        if session_id not in self._session_returncodes:
            return self._fail(
                f"未找到 shell 会话: {session_id}",
                error_type="session_not_found",
                session_id=session_id,
            )
        return ToolResult(
            success=True,
            message="进程已结束",
            data={"returncode": self._session_returncodes[session_id]},
        )

    async def write_shell_input(
            self,
            session_id: str,
            input_text: str,
            press_enter: bool = True,
    ) -> ToolResult:
        """fast mode 不支持交互式输入 —— 明确报错，而不是静默假装成功。"""
        return self._fail(
            "fast mode 不支持交互式 Shell 输入（命令为同步执行，无持久会话）",
            error_type="unsupported",
            session_id=session_id,
        )

    async def kill_process(self, session_id: str) -> ToolResult:
        """fast mode 下没有常驻进程可杀。"""
        return self._fail(
            "fast mode 下没有常驻 Shell 进程",
            error_type="unsupported",
            session_id=session_id,
        )
