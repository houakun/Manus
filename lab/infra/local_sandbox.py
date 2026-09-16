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

== 已知限制（Step 1 明确接受，写进文档而不是藏着）==
- `shell_execute` 是"跑完即返回"，没有 SUT 真沙箱那种可交互的**持久 Shell 会话**；
- 命令中的沙箱绝对路径靠**字符串替换**成本地真实路径，复杂命令（heredoc、变量拼接、
  通配符展开）可能替换不到 —— 这是近似模拟，不等价于真沙箱；
- 不提供浏览器与网络能力（fast mode 的工具集里已经把浏览器/搜索工具去掉了）。
以上限制决定了 fast mode 适合**计算 / 文件 IO / 文本处理类确定性任务**，
不适合浏览器任务 —— 后者仍需真沙箱，Step 4 的任务集要按这个边界来设计。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import signal
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, List, Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402


class SandboxPathError(ValueError):
    """路径越界或非法时抛出。"""


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

    @property
    def vnc_url(self) -> str:
        """fast mode 没有 VNC（无 GUI），返回空串。"""
        return ""

    @property
    def cdp_url(self) -> str:
        """fast mode 没有 Chrome DevTools Protocol 端点，返回空串。"""
        return ""

    async def ensure_sandbox(self) -> None:
        """确保沙箱"已启动"。本地实现只需确认目录存在，不做任何容器操作。"""
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "home" / "ubuntu").mkdir(parents=True, exist_ok=True)
        (self._root / "tmp").mkdir(parents=True, exist_ok=True)

    async def destroy(self) -> bool:
        """销毁沙箱。**刻意不删除 workspace**：产物是评测证据，必须保留供人工检查。"""
        self._session_outputs.clear()
        self._session_returncodes.clear()
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
        """
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
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.update(self._extra_env)
        return env

    async def exec_command(self, session_id: str, exec_dir: str, command: str) -> ToolResult:
        """执行命令并等待结束（非交互式），返回 returncode 与合并后的输出。"""
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
                cwd=str(cwd),
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
            partial = (raw_output or b"").decode("utf-8", errors="replace")
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

        output = (raw_output or b"").decode("utf-8", errors="replace")
        if len(output) > self._max_output_chars:
            output = output[: self._max_output_chars] + "\n...[输出过长已截断]"

        self._session_outputs[session_id] = output
        self._session_returncodes[session_id] = process.returncode or 0

        # 注意：这里 success 表示"命令成功执行完"，与 SUT 真沙箱语义一致
        #（真沙箱只要 HTTP 200 就 success=True，returncode 放在 data 里）。
        # 非零返回码不应该被吞掉，因此 message 里显式提示，供模型判断。
        hint = "" if process.returncode == 0 else f"（返回码 {process.returncode}，命令执行失败）"
        return ToolResult(
            success=True,
            message=f"命令执行完成{hint}",
            data={
                "session_id": session_id,
                "command": command,
                "status": "completed",
                "returncode": process.returncode,
                "output": output,
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
