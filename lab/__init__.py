#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""agent-reliability-lab：Agent 可靠性评测实验台（Step 1）

三层结构（详见 docs/step1-architecture-and-refactor-plan.md）：
    EvalOps 层（测量仪器，本目录）
      └── 可靠性工程层（控制与加固，后续 Step 3）
            └── SUT（被测对象 = mooc-manus/api 里的 PlannerReActFlow）

本包**不做**任何 import 副作用：需要 SUT 代码的模块会显式调用
`lab.bootstrap.ensure_sut_on_path()`，把耦合点写明白。
"""

__version__ = "0.1.0"
