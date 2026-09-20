#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""后置校验：工具声称成功之后，**独立**确认结果是否真的成立。

== 为什么"工具说自己成功"不能信 ==
失败有两种：
1. **显式失败**：返回 success=False。好处理，LLM 也能看见。
2. **静默失败**：返回 success=True 但结果是错的/空的/不完整的。
   这是真正危险的一类 —— 没有任何一方会报警，Agent 会基于假事实继续往下做，
   最后交付一个看起来完整、实际错误的结论。

Step 3 的故障注入里有一半（`empty_result` / `malformed_result` /
`truncated_result` / `silent_wrong_result` / `partial_write`）都属于第 2 类，
**只有后置校验才能发现它们**。

== 这一层的定位（对应 handoff 第 8 节的失败归因分类）==
    定位 / 规划 / **验证** / 工具
"验证"是独立的一类失败原因。如果没有后置校验，
所有静默失败都会被错误地归因到"LLM 判断力不行"，
于是你会去改提示词 —— 而真正该修的是工具层。

== 设计约束：校验本身不能变成新的故障源 ==
- 只做**只读**校验（绝不为了校验而写文件）；
- 大文件跳过内容校验（避免把内存/IO 打满）；
- 校验失败**只记录不阻断**（observe 模式），因为它自己也可能误报。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.tool_result import ToolResult  # noqa: E402

# 各工具成功时 data 里**必须**存在的字段。
# 这就是 `malformed_result` 故障的检测器：字段缺失 = 结果结构损坏。
EXPECTED_DATA_KEYS: Dict[str, Tuple[str, ...]] = {
    "read_file": ("filepath", "content"),
    "write_file": ("filepath", "bytes_written"),
    "replace_in_file": ("filepath", "replaced_count"),
    "search_in_file": ("filepath", "matches", "line_numbers"),
    "find_files": ("dir_path", "files"),
    "list_files": ("dir_path", "files"),
    "check_file_exists": ("filepath", "exists"),
    "delete_file": ("filepath", "deleted"),
    "upload_file": ("filepath", "file_size"),
    "shell_execute": ("session_id", "returncode"),
    "shell_read_output": ("session_id", "output"),
    "shell_wait_process": ("returncode",),
}

# 内容校验的大小上限：超过就跳过（校验不能把评测机器拖垮）
MAX_VERIFY_CHARS = 200_000


def _looks_like_json(content: str) -> bool:
    """内容看起来是不是 JSON（用于结构合法性校验）。"""
    text = content.lstrip()
    return text.startswith("{") or text.startswith("[")


def _looks_truncated(data: dict, args: dict, content: str) -> bool:
    """判断内容是否**本来就被截断**（这种情况下不能做结构校验）。

    为什么必须判：`read_file` 默认只读 10000 字符。一个 50KB 的 JSON 文件
    读回来就是半截，直接拿去做 JSON 解析必然失败 —— 那是**工具的正常行为**，
    不是故障。不区分就会制造大量假告警（而假告警会让真告警失效）。

    两类证据：
    1. 工具自己标了 `truncated`（本地沙箱会标）；
    2. 内容长度正好等于请求的 `max_length`（强提示：是被上限截的）。
    """
    if data.get("truncated"):
        return True
    limit = args.get("max_length")
    if isinstance(limit, int) and limit > 0 and len(content) >= limit:
        return True
    return False


def check_postcondition_content(
        *,
        function_name: str,
        args: Optional[dict],
        result: ToolResult,
) -> Optional[str]:
    """**内容级**后置校验（可 enforce）：目前做 JSON 结构合法性。

    == 为什么需要它（读路径的盲区）==
    原来 `read_file` 只查 `filepath`/`content` 两个字段存在 —— 只要拿回一个
    非空字符串就算通过。于是**读路径上的静默损坏**（`truncated_result` /
    `silent_wrong_result`）完全无人发现：Agent 拿到半截 JSON 也会直接往下用。

    这是唯一一类"**Agent 自身无法发现**"的故障 —— 它没有外部真相可比。
    而结构合法性（JSON 能不能解析）是一个**与来源无关**的客观不变量，
    因此可以高置信度地自动判定，适合纳入 enforce。

    误报防范：内容**本来就被截断**（见 `_looks_truncated`）时直接跳过 ——
    工具按上限截内容是正确的，不能当成损坏。
    """
    args = args or {}
    if not result.success:
        return None
    data = _unwrap(result)
    if data is None:
        return None
    content = data.get("content")
    if not isinstance(content, str) or not content:
        return None
    if not _looks_like_json(content):
        return None
    if _looks_truncated(data, args, content):
        return None

    try:
        json.loads(content)
    except (json.JSONDecodeError, ValueError) as exc:
        return (
            f"{function_name} 返回的内容看起来是 JSON 但无法解析"
            f"（疑似被截断或损坏）：{exc}；长度 {len(content)} 字符"
        )
    return None


def _digest(content: str) -> str:
    import hashlib

    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]


def check_read_consistency(
        *,
        filepath: Optional[str],
        content: Optional[str],
        known_writes: Dict[str, tuple],
) -> Optional[str]:
    """**审计级**校验：读回的内容与最近一次成功写入是否一致。

    == 为什么这条只做审计、不纳入 enforce ==
    它能抓到"读路径静默损坏"，但**有真实的误报风险**：
    文件可能被 `shell_execute` 里的命令改过（sed/重定向），
    或被另一个步骤改过 —— 这时读写不一致是**正常现象**。
    拿它去 enforce 会造成假失败（把正确的工作判错）。

    所以它只作为**审计信号**（进 trace 与报告），提醒人看一眼；
    高置信度的那部分（JSON 结构）已经在 `check_postcondition_content` 里 enforce 了。
    这正是"检测强度"的取舍点：要抓住真问题，但不能把正常行为当问题。
    """
    if not filepath or content is None:
        return None
    known = known_writes.get(filepath)
    if not known:
        return None
    known_length, known_digest = known
    if len(content) == known_length and _digest(content) == known_digest:
        return None
    return (
        f"读回内容与最近一次成功写入不一致：写入 {known_length} 字符（sha1 {known_digest}）"
        f"→ 读出 {len(content)} 字符（sha1 {_digest(content)}）。"
        f"可能是读路径静默损坏，**也可能是文件被 shell 命令改过**（故只作审计信号）"
    )


def _unwrap(result: ToolResult) -> Optional[dict]:
    data = result.data
    if data is None:
        return None
    if isinstance(data, dict):
        return data
    # pydantic 模型形式的 data（SUT 部分工具会这么返回）
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return None


def _expected_write_content(args: dict) -> Optional[str]:
    """按 write_file 的参数复算出"本该写入的内容"。

    为什么能算出来：写入内容是调用方给的（就在 args 里），换行选项也是确定的。
    所以"声称写了 X"和"文件里真的是 X"可以直接比对 —— 这是最强的后置校验，
    能一次性抓住 partial_write / truncated_result / silent_wrong_result 三类静默失败。
    """
    content = args.get("content")
    if not isinstance(content, str):
        return None
    text = content
    if args.get("leading_newline"):
        text = "\n" + text
    if args.get("trailing_newline"):
        text = text + "\n"
    return text


async def check_postcondition(
        *,
        function_name: str,
        args: Optional[dict],
        result: ToolResult,
        sandbox: Any = None,
) -> Optional[str]:
    """校验一次工具调用的结果。返回告警文本，None 表示通过。

    异步是因为 write_file 的校验需要回读文件（走沙箱接口）。
    """
    args = args or {}

    # 1.失败结果不校验（失败原因由工具自己负责说清楚）
    if not result.success:
        return None

    data = _unwrap(result)

    # 2.通用检查：声称成功却没有数据
    if result.data is None:
        return f"{function_name} 声称成功但返回空数据(data=None)"

    # 3.字段完整性检查（抓 malformed_result）
    expected_keys = EXPECTED_DATA_KEYS.get(function_name)
    if expected_keys and data is not None:
        missing = [key for key in expected_keys if key not in data]
        if missing:
            return f"{function_name} 结果缺少字段 {missing}（疑似结构损坏）"

    # 4.write_file 的强校验：回读文件比对内容（抓 partial_write 等）
    if function_name == "write_file" and sandbox is not None and data is not None:
        warning = await _verify_written_file(args, data, sandbox)
        if warning:
            return warning

    return None


async def _verify_written_file(args: dict, data: dict, sandbox: Any) -> Optional[str]:
    """回读刚写入的文件，确认内容与预期一致。"""
    filepath = data.get("filepath") or args.get("filepath")
    if not filepath:
        return "write_file 结果里没有 filepath，无法校验"

    # 追加模式下无法用"内容相等"判断（需要知道追加前的原文），
    # 只确认文件存在 —— 明确降级而不是假装校验过了。
    if args.get("append"):
        checked = await sandbox.check_file_exists(filepath)
        exists = (_unwrap(checked) or {}).get("exists")
        if not exists:
            return f"write_file(append) 声称成功但文件不存在: {filepath}"
        return None

    expected = _expected_write_content(args)
    if expected is None:
        return None
    if len(expected) > MAX_VERIFY_CHARS:
        return None  # 太大，跳过内容校验（避免校验本身成为负担）

    read_back = await sandbox.read_file(filepath, max_length=MAX_VERIFY_CHARS + 1)
    if not read_back.success:
        return f"write_file 后回读文件失败: {read_back.message}"

    actual = (_unwrap(read_back) or {}).get("content")
    if actual is None:
        return f"write_file 后回读文件没有内容: {filepath}"

    if actual != expected:
        return (
            f"写入内容与预期不一致: 预期 {len(expected)} 字符，实际 {len(actual)} 字符"
            f"（疑似部分写入/截断）: {filepath}"
        )

    # 字节数申报值也要对得上（工具自己报的数字不能自相矛盾）
    claimed = data.get("bytes_written")
    if isinstance(claimed, int) and claimed != len(expected.encode("utf-8")) and expected.isascii():
        return f"bytes_written 申报 {claimed}，实际应为 {len(expected.encode('utf-8'))}"

    return None
