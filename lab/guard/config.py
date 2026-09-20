#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""加固能力开关 —— 做「无加固 vs 加固」对照实验的前提。

== 为什么必须有这个开关（来自 step6-gap-audit §2.1 的诊断）==
`ToolGuard` 的 `verify_postconditions` / `max_attempts` 是构造参数，
而 **CLI / config / bench 都不可配** → 加固**永远是开的** →
物理上无法构造"无加固"对照组 → 拿不出"加固前后成功率曲线"。

这是「推荐 4（可靠性工程）」没闭环的**唯一硬原因**：
故障注入、中间件、后置校验全都写好了，但没有对照条件。

== 哪些能力真的影响成败 ==
| 能力 | 作用 | 关掉它会怎样 |
|---|---|---|
| `retry` | 幂等性感知重试（暂态失败自愈） | 一次超时/抖动就直接失败 |
| `postconditions` | 后置校验（**发现**静默失败） | malformed/partial/truncated 这类会**冒充成功** |
| `enforce` | 把后置校验的失败**变成对 LLM 可见的失败** | 校验发现了但模型不知道 → **自愈链断开** |
| `loop_guard` | 重复动作检测 | 只影响**观测**（少一个过程 flag），不影响成败 |
| `budget` | 预算检查（Step 3 是 observe 模式） | 同上，只影响观测 |

**关于 `enforce`（必须理解清楚）**：后置校验默认只**记录**告警，
那个 `ToolResult` 仍然以 `success=True` 返回给模型 ——
于是模型在错误的事实上继续推理，**检测了却没有回灌**。
`enforce=True` 会把这种结果改成 `success=False` + 告警文本，
让模型有机会重试/纠正：这才是"检测 → 回灌 → 自愈"的闭环。

== spec 语法 ==
    all                      全部开启（默认；**不含 enforce**）
    none                     全部关闭
    retry,postcondition      只开启列出的（其余关闭）
    retry,postcondition,enforce   开启前两项且让校验失败对模型可见
别名：`postconditions` / `postcondition` / `check` 都接受；`enforce` 同义。

> 为何 `enforce` 默认关闭：它会**改变 SUT 的成功率**（原本记录下来的静默失败
> 现在会真的失败）。默认开启会静默地换掉基线语义，所以它是一个**显式选项**。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from pydantic import BaseModel

# spec 里的别名 → 规范化的能力名
_ALIASES = {
    "retry": "retry",
    "postcondition": "postconditions",
    "postconditions": "postconditions",
    "check": "postconditions",
    "verify": "postconditions",
    "enforce": "postcondition_enforce",
    "postcondition_enforce": "postcondition_enforce",
    "loop": "loop_guard",
    "loop_guard": "loop_guard",
    "budget": "budget",
}
# 参与 label 的四个开关（enforce 是 postconditions 的模式，单独拼到标签后面）
_FEATURES = ("retry", "postconditions", "loop_guard", "budget")


class GuardConfig(BaseModel):
    """加固能力开关。"""

    retry: bool = True
    postconditions: bool = True
    loop_guard: bool = True
    budget: bool = True
    # 后置校验失败时，把结果改成对 LLM 可见的失败（而不是只记一条告警）。
    # 默认 False = 只观测，保证不静默改变基线语义。
    postcondition_enforce: bool = False

    @property
    def label(self) -> str:
        """紧凑标签，用于落库与报告分组（**没有它两组数据无法区分是谁的**）。"""
        enabled = [name for name in _FEATURES if getattr(self, name)]
        if len(enabled) == len(_FEATURES):
            label = "all"
        elif not enabled:
            label = "none"
        else:
            label = "+".join(enabled)
        if self.postcondition_enforce:
            label += "+enforce"
        return label

    def describe(self) -> str:
        parts = [f"{name}={'on' if getattr(self, name) else 'off'}" for name in _FEATURES]
        parts.append(f"enforce={'on' if self.postcondition_enforce else 'off'}")
        return f"guard[{self.label}]: " + ", ".join(parts)

    @classmethod
    def from_spec(cls, spec: Optional[str]) -> "GuardConfig":
        """从命令行/环境变量的 spec 构造。

        `all` / `none` 是快捷词；列出的能力名表示**只开这些**（其余关闭）——
        "只开这些"而不是"在这些之上叠加"，是为了让对照实验可预测：
        见到 `--guard retry` 就知道后置校验一定是关的。
        """
        if spec is None or str(spec).strip() == "":
            return cls()

        text = str(spec).strip().lower()
        if text == "all":
            return cls()
        if text == "none":
            return cls(**{name: False for name in _FEATURES})

        enabled = set()
        unknown = []
        for raw in text.replace(";", ",").split(","):
            item = raw.strip()
            if not item:
                continue
            key = _ALIASES.get(item)
            if key is None:
                unknown.append(item)
            else:
                enabled.add(key)

        if unknown:
            raise ValueError(
                f"未知的加固能力 {unknown}；可选：all / none / "
                f"{'/'.join(sorted(set(_ALIASES)))}"
            )
        if not enabled:
            raise ValueError("guard spec 解析后为空，请用 all / none 或明确的能力名")

        config = cls(**{name: (name in enabled) for name in _FEATURES})
        if "postcondition_enforce" in enabled:
            # enforce 以 postconditions 为前提：不开校验就没有东西可 enforce
            config = config.model_copy(update={"postconditions": True, "postcondition_enforce": True})
        return config

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "GuardConfig":
        source = env if env is not None else os.environ
        return cls.from_spec(source.get("LAB_GUARD"))
