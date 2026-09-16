#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""lab 与被测系统（SUT）之间的**唯一**耦合点。

为什么需要这个文件：
- lab 是"测量仪器"，SUT（mooc-manus/api）是"被测对象"。按理两者应该能独立演进，
  所以 lab 不复制 SUT 的任何代码，只通过 import 复用；
- 但 Step 1 要复用 SUT 已经写好的 PlannerReActFlow，就必须能 `import app.*`；
- 于是把 sys.path 操作集中在这一个文件里：将来把 lab 拆成独立仓库时，
  只需要改这里的 SUT_API_DIR 一行（或改成 pip 安装 SUT 包）。

刻意的约束：
- 不修改 SUT 代码、不做 monkey patch、不改环境变量；
- 只做一件事：把 SUT 的 api 目录加到 sys.path 最前面。
"""

from __future__ import annotations

import sys
from pathlib import Path

# lab/bootstrap.py -> lab/ -> mooc-manus/
LAB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = LAB_DIR.parent
# SUT 的 Python 包根目录：里面有 app/（业务代码）和 core/（配置）
SUT_API_DIR = PROJECT_ROOT / "api"

# lab 自己的产物目录（轨迹、任务工作区），Step 2 会把 trace 落在这里
RUNS_DIR = LAB_DIR / "runs"


def ensure_sut_on_path() -> Path:
    """把 SUT 的 api 目录加到 sys.path，返回该目录。

    幂等：重复调用不会重复插入。
    """
    sut_path = str(SUT_API_DIR)
    if sut_path not in sys.path:
        sys.path.insert(0, sut_path)
    return SUT_API_DIR


def ensure_runs_dir() -> Path:
    """确保 lab 产物目录存在，返回该目录。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNS_DIR
