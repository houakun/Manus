# Go 模式任务队列：把「测量台」变成「一条能讲的话」

> 这份文件是**给 go 模式（长期自动运行）用的任务队列**。
> 它必须自包含：执行者没有本次会话的上下文，所有前提都写在下面。
> 队列按**依赖顺序**排列，每个任务都有可自检的验收标准与成本闸门。
>
> **怎么用**：把本文件**全文**作为 go 模式的目标发送即可；执行过程中可随时重读本文件。
> 每个任务完成后：跑测试 → 在「进度台账」加一行 → **停下来报告**（不自行 commit）。

---

## Context（为什么要做这些）

**项目**：`D:/智能体/agent/源码/mooc-manus`（Windows / Git Bash，Python 用 `.venv/Scripts/python.exe`）。
仓库里有两层东西：

| 层 | 位置 | 状态 |
|---|---|---|
| **被测对象（SUT）** | `api/`（仿 Manus 的 Agent，课程作者的 `PlannerReActFlow`） | 可用，是**别人写的代码** |
| **测量台（本项目的主角）** | `lab/`（评测 harness + 加固中间件 + 故障注入 + trace + 报告 + 门禁） | 已完成，且是本仓库最有价值的部分 |

**已经做完并测出来的东西**（这些数字可以直接引用，不要重测）：

| 能力 | 数字 / 位置 |
|---|---|
| 20+2 个确定性任务（12 synthetic / 8 semireal / 2 ci），纯函数判定器 | `lab/bench/tasks/`，`bench validate` → **22/22** |
| `pass^k` / Wilson / bootstrap / 配对检验 | `lab/bench/stats.py`，报告第一部分并列输出 |
| 加固开关（`--guard`）+ 交错 A/B（`--ab-guard`） | `lab/cli.py`，`docs/step6-gap-closure.md` |
| 回归门禁（退出码 + 噪声地板自动读取） | `bench gate`，`.github/workflows/ci.yml` |
| LLM 录制回放（真跑 24.5s/$0.057 → 离线 0.4s/$0） | `lab/replay/`，`docs/step7-replay-noise-interleave.md` |
| **噪声地板（最新，装置修复后）** | 成本 **±16.6%**、token ±17.2%、耗时 ±13.4%（7 任务，42 次运行，`lab/runs/noise_floor.json`） |
| 装置保真修复（文件 CRLF 归一 / 虚拟盘符防逃逸 / 确定性 PATH + 环境指纹） | `docs/step8-device-fixes.md`、`lab/infra/env.py` |
| 测试 | **287 passed** |

**缺的是什么（本队列要解决的）**：
1. **没有一条"关于 Agent 本身"的因果结论** —— 唯一面向 Agent 的实验（加固 2×2）是**负结果**
   （`docs/ab-guard-experiment.md`：写路径上 Agent 会自己读回验证，后置校验冗余）。
2. **样本量太小**：`pass^k` 的样本量是**任务数**，semireal 只有 8 个，区间重叠。
3. **被测的 Agent 不是自己写的**：`lab/agent/` 不存在 → 面试官问"哪部分是你写的"会很被动。
4. **`ci` 任务组只有 2 个**（目标是 8~10），而它是唯一同时回答"评的是什么 / 有什么用"的一类。
5. 一批便宜的观测工具缺失（读轨迹入口、span 级成本归因、标签对账）。
6. **LLM-judge 校准是审计里唯一完全为零的高分项**（需要人工金标，agent 只能把 90% 的活做完）。

---

## 已锁定的决定（用户已确认，不要再问）

| # | 决定 | 对队列的影响 |
|---|---|---|
| 1 | **预算足够** | T6 / T7 / T9 / T13 全部可以跑（合计约 $51） |
| 2 | **不要自行 `git commit`** | 每个任务结束时只报告"改了什么 / 证据 / 待提交文件清单"，**由用户自己提交** |
| 3 | **可以自行批准付费任务** | 但仍必须**先打印预估成本**、跑完记录**实际花费**；不需要停下来等确认 |
| 4 | **可以清除逃逸残留** | ⚠️ 但 `AGENTS.md` 优先级高于用户指令：**agent 不得代跑递归删除**。
|   |   | 只能由**用户手动**跑 `python -m lab sandbox clean-escapes --yes`；agent 只负责报告清单 |
| 5 | **LICENSE = MIT** | T11 直接用 MIT |
| 6 | 无额外插入任务 | 队列保持不变 |

---

## 全局纪律（每个任务都必须遵守）

1. **每个任务结束必须全绿**：`.venv/Scripts/python.exe -m pytest -q` 全过 +
   `.venv/Scripts/python.exe -m lab bench validate` → 22/22（T8 后 28/28）。
   **然后停下报告**：改了什么 / 证据（命令 + 输出要点）/ 待提交文件清单 ——
   **不要自行 `git commit`**（用户自己提交）。
2. **一次只改一个变量**。若一轮改了多个，**禁止**把结果归因给其中某一个
   （这条纪律是踩过坑换来的：`docs/noise-floor-measured.md` §6.4）。
3. **任何 delta 必须对照噪声地板**（成本 ±16.6% / 耗时 ±13.4%）判断是否可读；
   小于地板的一律写成"看不出来"。
4. **付费任务可以先跑，但必须先打印预估**。流程固定为：
   打印任务数×次数×臂数×单次历史均值 → 跑 → 记录**实际花费**到台账。
   绝不在后台静默跑付费评测；**跑之前先看一遍命令里的臂数**（`--runs N` 在多臂时是**每臂各 N 次**）。
5. **禁止批量删除**（`AGENTS.md` 最高优先级）：不得 `rm -rf`、通配符删除、递归删除目录树；
   只能删"用户明确指定的单个文件"或"本轮自己生成的单个垃圾文件"。
6. **不许把"工具存在"写成"结论成立"**；不许把未实测的数字写成实测
   （`docs/step6-gap-audit.md` §7 专门警告过）。
7. **中文注释说明"为什么这么写"**（本仓库的既有风格）；每个任务在 `docs/` 留一份记录，
   并在 `lab/README.md` 的相关表格里登记（若适用）。
8. **进度台账**：每完成一个任务，就在本文件末尾的「进度台账」加一行（任务号 / 状态 / 关键数字）。
   commit 列由用户填（因为 agent 不自行提交）。

---

## Approach（推荐路线，按依赖排序）

```
T0 清场快照（不提交，只报告）
 └─ T1 bench run --purpose（解锁 T6 的口径）
     └─ T2 读轨迹入口 ─┐
     └─ T3 成本归因    ├─ 都是 $0，先补齐"看得见"
     └─ T4 歧义 lint   │
     └─ T5 标签对账   ─┘
         └─ T6 ★读路径静默故障 2×2（$18）← 唯一可能出"那条曲线"
             └─ T7 负结果时的备选故障/能力（$18）
     T8 ci 任务 2→8（$0 写） ── T9 ci capability 基线（$10）
     T10 自写最小 loop 作为第二 SUT（$0 代码）← 最大可信度缺口
     T13 地板复测（$5）← 所有 delta 的分母，最后确认一次
     T14 LLM-judge + κ 校准骨架（$0 代码；人工标注由用户做）
         T11 开源化收尾（$0，MIT）
             T12 一页纸最终叙事（$0）
```

**优先级判断**：T0~T5 是 $0 且直接提升后续所有实验的可读性 → 先做。
T6 是唯一可能产出"简历那句话"的实验 → 但花钱且可能又是负结果。
T8/T10 提升可信度与样本量。T11/T12 是收尾。

---

## Files to modify（关键文件）

| 任务 | 文件 |
|---|---|
| T1 | `lab/cli.py`（`bench_run` 加 `--purpose`、空集诊断复用）、`lab/bench/runner.py`（`run_suite(purpose=)` 已存在） |
| T2 | `lab/cli.py`（新子命令）、`lab/trace/view.py`（`render_trace` 复用）、`lab/trace/store.py`（`load_suite` / `load_runs_grouped_by_task`） |
| T3 | `lab/trace/store.py`（新聚合函数）、`lab/bench/report.py` |
| T4 | `lab/bench/validate.py` |
| T5 | `lab/cli.py` + `lab/bench/task.py`（`task_purposes()` 已存在）+ `lab/trace/store.py` |
| T6/T7 | 只跑命令 + 写 `docs/`，**不改代码**（若需要改，说明设计有问题，先停下来问） |
| T8 | `lab/bench/tasks/ci/*.yaml`（新增 6 个）+ `lab/tests/test_bench.py`（任务集形状断言） |
| T9 | 只跑命令 + 写 `docs/` |
| T10 | `lab/sut/minimal_react.py`（新）、`lab/api.py`（`--sut` 选择）、`lab/tests/test_minimal_react.py`（新） |
| T13 | 只跑命令 + 更新 `lab/runs/noise_floor.json`、`lab/README.md` 的限制表 |
| T14 | `lab/bench/judge.py`（新）、`lab/bench/calibrate.py`（新）、`lab/bench/tasks/**/*.yaml`（加 rubric 版本）、标注 CSV 生成 |
| T11 | `LICENSE`、`lab/requirements.txt`、`README.md`、`lab/README.md`、`.github/workflows/ci.yml`（改依赖安装来源） |
| T12 | `README.md`、`lab/README.md` |

## Reuse（不要重写的东西）

- `lab/bench/noise.py`：`load_noise_floor()` / `resolution()` —— 判断 delta 可读性
- `lab/bench/stats.py`：`wilson_interval` / `paired_bootstrap` / `pass_pow_k`（`task_level_rates`）
- `lab/bench/compare.py`：`compare_suites(..., scope="regression")` / `evaluate_gates` / `render_gates`
- `lab/bench/report.py`：`render_report`（新章节挂在这里）
- `lab/trace/view.py`：`render_trace`（T2 直接复用，不要重写渲染）
- `lab/middleware.py`：`GuardedTool`（T10 的 SUT 可以复用同一套工具代理）
- `lab/infra/local_sandbox.py` + `lab/infra/env.py`：沙箱与确定性 PATH
- `lab/replay/`：调试时用 `--replay record/replay` 省钱的路径
- `docs/step6-followup-purpose-and-readpath.md` §4：C′/D′ 的**实验设计已经写好**，照它做

---

## Steps（任务队列）

### T0 — 清场与基线快照（$0，**不提交**）

- [ ] **不要 commit**。只报告当前待提交文件清单（起始为 3 个：
      `docs/step8-device-fixes.md`、`lab/README.md`、`lab/bench/tasks/semireal/07_sqlite_report.yaml`）。
- [ ] 记录基线快照到台账：`git log -1`、`pytest` 通过数、`bench validate` 结果、地板③ 三个数字。
- [ ] 报告逃逸残留清单（**只报告，不删**）：`python -m lab sandbox clean-escapes`（默认 dry-run）。
      用户已授权清理，但按 `AGENTS.md` **递归删除必须由用户手动执行**：
      用户自己跑 `python -m lab sandbox clean-escapes --yes`。
- **验收**：台账里基线快照已填；待提交文件清单已报告；逃逸清单已报告（含文件数/字节数）。

### T1 — `bench run` 加 `--purpose`（$0）

**为什么**：`bench noise-floor` 有 `--purpose`（默认 `regression`），但 `bench run` **没有** ——
于是"只在 regression 任务上做对照实验"（T6 的设计）只能手写 7 个 `--task`，容易漏/错。

- [ ] `lab/cli.py`：`bench_run` 加 `--purpose {capability,regression,all}`（默认 `all`，
      与现状行为一致，**不要**改成默认 regression —— 那会静默改变所有人的取样）。
- [ ] 过滤与传参走 `run_suite(purpose=...)`（`lab/bench/runner.py:775` 已实现）。
- [ ] 空集时复用 `noise-floor` 里刚加的诊断（说出"是 purpose 过滤筛掉的"）。
- [ ] 新增用例：`--purpose capability --group ci` 能列出 2 个任务；`--purpose regression --group ci` 给出可操作报错。
- **验收**：pytest 全绿；`python -m lab bench run --group ci --purpose regression --limit 1` 打印可操作诊断且退出码 2。

### T2 — 读轨迹入口：`bench show` / `bench sample-failures`（$0）

**为什么**：审计 §4.8 —— 项目要求"读 transcript"，但没有入口（只有 `lab trace` 渲染单次运行）。

- [ ] `bench show <suite> <task_key> [--run N] [--arm NAME]`：打印该次运行的
      工具序列（来自 `tasks.tool_sequence`）+ 失败的检查项 + 关键产物内容（从 workspace 读）。
- [ ] `bench sample-failures [--suite X] [--n 5]`：从 `traces.db` 挑失败运行并逐个展示。
- [ ] 复用 `lab/trace/view.py:render_trace`，不要新写渲染。
- [ ] 用例：用库里已有的失败运行（如 `sem_markdown_toc` 的失败）做断言，**不花钱**。
- **验收**：两条命令都能在现有 442 条历史数据上跑出内容；pytest 全绿。

### T3 — span/step 级成本归因（$0）

**为什么**：现在只有 task 级成本，"钱花在哪一步/哪个工具"答不出来（审计 §4.12）。

- [ ] `lab/trace/store.py`：加聚合函数（如 `cost_by(kind="tool"|"step")`），从 `spans` 表按 span 归属聚合。
- [ ] `lab/bench/report.py`：加一节"成本归因"（per-tool / per-step 表，按成本降序，标出占比）。
- [ ] 用例：构造假 span 断言聚合正确；再用历史数据人工核对一次。
- **验收**：`bench report <已有 suite>` 能打印 per-tool 成本表；pytest 全绿。

### T4 — 歧义 lint（$0）

**为什么**：`sem_markdown_toc` 曾因"判定器要求的路径在 goal 里没写清"污染过两次实验（审计 §4.10）。

- [ ] `lab/bench/validate.py`：检查每个 `VerifyCheck.path` 是否在 `goal` 中被提及；未提及 → **warning**（不改变通过/失败）。
- [ ] 输出一份告警清单（当前 22 个任务）。
- **验收**：validate 仍 22/22；告警清单写进 `docs/`。

### T5 — 标签 vs 实测的对账（$0）

**为什么**：`purpose` 标签是某一时刻数据的快照，任务集会变（`ci` 组就是例子）；
没有对账机制时，一个 `regression` 任务后来变得不稳定**没人会收到通知**。

- [ ] 新命令 `bench purpose-audit`（或并入 `bench report`）：读 `traces.db`，
      对每个任务算**干净运行成功率**（`faults_injected=0`）与"同配置翻转次数"，
      与 `task.purpose` 对比，不一致就告警（例如 regression 任务出现干净失败）。
- [ ] 输出表：任务 / 标签 / 干净 n-ok / 判定（一致 / 建议改标）。
- **验收**：在现有数据上跑，能重现"`syn_date_diff` 应该改标 capability"这类结论；pytest 全绿。

### T6 — ★ 读路径静默故障 2×2（C′/D′）（**花钱 ≈ $18；已授权，可自行批准**）

**为什么**：这是**唯一**可能产出"加固把成功率从 X% 提到 Y%"的实验。
写路径的负结果已经证明"Agent 会读回自证"→ 后置校验冗余；而**读路径**（`truncated_result`）
是 Agent 无法自证的（检测面已实现：`lab/guard/postcondition.py:check_postcondition_content`）。

- [ ] 先跑一次 `--replay record` 的小样（1 任务 1 次）验证链路（≈$0.4）。
- [ ] 打印预估成本（7 任务 × 3 次 × **2 臂** = 42 次 × ≈$0.43 ≈ **$18**），然后开跑。
- [ ] 正式跑（交错，regression 口径）：
      ```bash
      .venv/Scripts/python.exe -m lab bench run --runs 3 --group semireal --purpose regression \
        --fault truncated_result --fault-tool read_file --fault-rate 0.5 \
        --ab-guard none --ab-guard "retry,postcondition,enforce" \
        --label "读路径静默故障 2×2（C′/D′）" --yes
      ```
- [ ] 分析：`bench compare <suite> --arm "guard=none" --arm "guard=retry+postconditions+enforce"`，
      并对照噪声地板（±16.6%）判断可读性。
- [ ] 写 `docs/step9-readpath-2x2.md`：**必须**分成"能说 / 不能说"两张表；
      若为负结果，写清机制（为什么没救回来），并给出下一步假设。
- [ ] **靠近地板的效应要加功率再判**：若观测到成功率差异但配对区间跨 0（或成本 delta 在地板 ±16.6% 内），
      不要写成结论 —— 先用 `--from-suite <id>` 免费重算不同口径，再决定要不要把 `--runs` 加到 5 重跑（+≈$12）。
- **验收**：文档 + 报告；无论正负结果都算完成（负结果必须可解释）。

### T7 — 若 T6 是负结果：换故障类或补 guard 能力（**花钱 ≈ $18；已授权**）

- [ ] 先做 **$0 的前提检查**：读 T6 的轨迹，回答"Agent 到底看到了什么、为什么没救回来"。
- [ ] 二选一（写清选择理由再跑）：
      (a) 换 `silent_wrong_result`（只在只读工具上生效，`lab/faults/injector.py:181`）；
      (b) 给 guard 加一个**Agent 自己没有**的恢复手段（例如"同一信息两处来源交叉校验"），
          并说明为什么 Agent 的提示词里没有这个动作。
- [ ] 同 T6 的验收要求。
- ⚠️ 若 T6 已经是正结果，**跳过 T7**，直接进 T8。

### T8 — `ci` 任务集 2 → 8（$0 写任务；每加一个跑 validate）

**为什么**：`ci` 组是唯一同时回答"评的是什么 / 有什么用"的一类；而且加任务能**收窄
`pass^k` 区间**（样本量是任务数，加重复次数没用 —— 已自证）。

- [ ] 照 `lab/bench/tasks/ci/01_window_boundary.yaml` 的结构，再加 6 个"CI 失败归因"任务：
      fixtures（CI 日志 + 仓库代码 + 测试）→ `verify`（`exec_script` 跑测试退出码 0 +
      `file_content` 检查 diagnosis.json 的 root_causes）→ `solution`（真修一遍）→ `wrong`。
- [ ] **必须真跑一遍测试生成日志**：`validate` 有 `fixture_authenticity` 规则，
      手写日志会被抓（`lab/bench/validate.py` 的"fixture 真实性"检查）。
- [ ] 更新 `lab/tests/test_bench.py` 的任务集形状断言（现在是 `{semireal:8, synthetic:12, ci:2}`）。
- [ ] 每个新任务都标 `purpose: capability`（方差未知，`test_ci_tasks_are_capability_not_regression` 钉住了）。
- **验收**：`bench validate` 22+6 = 28/28；pytest 全绿。

### T9 — `ci` 组 capability 基线（**花钱 ≈ $10；已授权**）

- [ ] 预估：8 任务 × 3 次 ≈ 24 次 × $0.40 ≈ **$9.6**（ci 任务比 semireal 贵 4 倍、慢 3 倍：
      37.7 次工具调用 / 106s）。
- [ ] 跑：`bench run --group ci --runs 3 --label "ci capability 基线" --report --yes`
- [ ] 产出：capability 口径第一份 `pass^k` + Wilson 区间 + 成本/步数；写 `docs/step10-ci-baseline.md`。
- **验收**：报告里有 ci 组的成功率与 `pass^k`，并写明样本量与独立性假设的 caveat。

### T10 — 自写最小 loop 作为第二个 SUT（$0 代码）

**为什么**：这是**最大的可信度缺口** —— `lab/agent/` 不存在，被测 Agent 100% 是课程作者的代码。
`docs/step1-architecture-and-refactor-plan.md:167` 自己写了这个风险。
同时它给出 `handoff §8` 里那句至今**只是计划数字**的基线（"单次 prompt 41.7% / ReAct 62.5%"）。

- [ ] 新文件 `lab/sut/minimal_react.py`：实现 `lab/sut/base.py` 的 `SUT` Protocol
      （`name` / `async run(goal) -> TaskResult` / `aclose`），**不用任何框架**：
      while 循环 + messages 数组 + 工具 schema + JSON 动作解析 + max_steps。
- [ ] 工具层复用：`lab/infra/local_sandbox.py` 的沙箱 + `lab/middleware.py` 的 `GuardedTool`
      （这样"有加固 / 无加固"就是同一份 loop 上的一个开关）。
- [ ] 接到 `lab/api.py` 的 SUT 选择上（`stats --sut` 已经按名字过滤，说明这个接缝存在）。
- [ ] 用例：用 `ScriptedLLM`（`lab/tests/fakes.py`）跑一个 synthetic 任务，断言产出正确。
- [ ] **不花钱**地验收：`bench validate` 不受影响；`--sut minimal-react --task syn_sum_range --limit 1`
      在**用户批准后**跑 1 次真模型（≈$0.06）作为冒烟。
- **验收**：pytest 全绿；冒烟跑通 1 个任务；文档写清"这个 loop 是我写的，每个决策可解释"。

### T13 — 地板复测（**花钱 ≈ $5；已授权**）

**为什么最后还要测一次**：地板是**所有 delta 的分母**。T1~T10 之后装置/任务集都变了
（新增 ci 任务、新增 SUT），若还引用旧的 ±16.6% 而不重测，后面的数字就没法判读。

- [ ] 用与上次**同一个 7 任务集**跑（保持可比）：
      ```bash
      .venv/Scripts/python.exe -m lab bench noise-floor --runs 3 --group semireal --purpose all \
        --task sem_csv_clean --task sem_log_analyze --task sem_json_to_csv --task sem_bug_fix \
        --task sem_config_merge --task sem_refactor_config --task sem_sqlite_report \
        --label "地板④（T1~T10 之后）" --file lab/runs/noise_floor.json --yes
      ```
- [ ] 若分辨率明显变差 → **先查机制再归因**（数轨迹模式：环境探测/看字节/重试次数），
      不要直接说"模型不稳定"（那是没证据的方向）。
- [ ] 把新数字写进台账与 `lab/README.md` 的限制表（替换 ±16.6%）。
- **验收**：`lab/runs/noise_floor.json` 已更新；`bench gate` 自动读到新地板；
      台账与 README 的数字一致（**不许两处写不同的地板**）。

### T14 — LLM-judge + Cohen's κ 的**骨架**（$0 代码；人工标注由用户做）

**为什么**：审计里唯一**完全为零**的高分项（`docs/step6-gap-audit.md` §1④）。
它需要人工金标 —— 所以 agent 只能把 **90% 的工程** 做完，把"标 100 条"留给用户。

- [ ] `lab/bench/judge.py`：按**维度独立调用** LLM judge，允许返回 `"Unknown"`
      （且 `Unknown` **不计入 κ** —— 否则一致性会被"不确定"稀释）。
- [ ] `lab/bench/calibrate.py`：读人工金标 → 算 Cohen's κ + 每维混淆矩阵；
      **低于阈值就拒绝启用**（默认 κ < 0.6 拒绝，阈值写成常量并注明来源）。
- [ ] 生成**标注清单**（agent 可做）：从 `traces.db` 抽 100 条（含正负样本、含失败运行），
      导出成 CSV（列：`task_key` / 产物路径 / 模型自述 / 人工标签（待填））。
- [ ] rubric 版本号写进 task YAML（审计 §4.7 的要求）。
- [ ] ⚠️ **不得**在没有人工金标的情况下写"已做 κ 校准"（`docs/step6-gap-audit.md` §1④ 专门警告过）。
- **验收**：`pytest` 全绿；标注 CSV 已生成（100 条）；`calibrate` 在**假的**金标上能算出 κ 并被测试钉住；
      文档写清"等用户标注后才能报真实 κ"。

### T11 — 开源化收尾（$0，**LICENSE = MIT**）

- [ ] `LICENSE`：**MIT**（用户已选定），版权行用仓库作者名（向用户确认署名写法）。
- [ ] `lab/requirements.txt`：从 `.github/workflows/ci.yml` 里硬编码的 pip 列表抽出来，CI 改成读它。
- [ ] 英文 `README.en.md`（标题 + 一句话 + 架构图 + 三条命令 + 关键数字）。
- [ ] 根 `README.md` 定位改成"**测量台为主角，api/ 是被测对象**"。
- **验收**：CI 三步仍全绿；`pip install -r lab/requirements.txt` 在干净 venv 里能跑 `pytest` + `validate`。

### T12 — 一页纸的最终叙事（$0）

- [ ] 把已实测的数字串成 README 顶部一段（每条都要带**限制**与**环境指纹**）：
      任务集与判定器 / `pass^k` / 噪声地板（±16.6%）/ 回放（24.5s→0.4s）/
      加固 2×2 的负结果 / 逃逸修复 0/42 / 门禁与 CI。
- [ ] 「我错在哪」一节保持更新（现在有 10 条自纠，这是面试官最看重的部分）。
- **验收**：一个不懂项目的人读完能复述"它回答了什么、怎么保证结论可信、错在哪"。

---

## Verification（如何验证整条队列）

```bash
cd D:/智能体/agent/源码/mooc-manus

# 1. 每个任务结束都跑这两条（$0）
.venv/Scripts/python.exe -m pytest -q                     # 期望：全过（起始 279 passed）
.venv/Scripts/python.exe -m lab bench validate            # 期望：22/22（T8 后 28/28）

# 2. 环境自检（$0）：Agent 会看到什么
.venv/Scripts/python.exe -m lab sandbox env

# 3. 地板与门禁（$0）
.venv/Scripts/python.exe -c "from lab.bench.noise import load_noise_floor; f=load_noise_floor(); print('成本分辨率 ±%.2f%%' % f.resolution('cost_pct',0.0))"
# 期望：±16.60%（若 T6/T9 之后重测了地板，这里应更新）

# 4. 付费实验之后（$0，从库重出报告）
.venv/Scripts/python.exe -m lab bench report <suite_id>
.venv/Scripts/python.exe -m lab bench compare <A> --arm "..." --arm "..."
```

**整条队列完成的标志**：
1. 至少一条**关于 Agent 本身**的结论（T6 或 T7），且写清能说/不能说；
2. `ci` 组 ≥8 个任务并有一份 capability 基线（T8/T9）；
3. 存在一个**自己写的** SUT 并跑通过（T10）；
4. 地板数字已用 T13 重测，且 README/台账一致；
5. LLM-judge 骨架就位、标注清单已交给用户（T14）；
6. 仓库可开源（MIT / 依赖 / 英文 README）（T11）；
7. README 顶部能一句话讲清项目价值（T12）。

**交付给用户的东西（每个任务结束时）**：
- 一段话：改了什么 / 证据（命令 + 输出要点）/ 结论的能说与不能说；
- **待提交文件清单**（agent 不自行 commit）；
- 台账新增一行。

---

## 需要用户动手的两件事（agent 做不了）

| #   | 事                                                         | 命令 / 说明                                                                                  |
| --- | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 1   | 清理逃逸残留（已授权，但按 `AGENTS.md` 必须用户手动跑递归删除） | `python -m lab sandbox clean-escapes`（先看）→ `python -m lab sandbox clean-escapes --yes`（真删） |
| 2   | T14 的人工标注（约 100 条，填 `lab/runs/judge_gold.csv`）    | agent 会把 CSV 生成好；填完后 agent 才能算真实 κ                                              |

---

## 进度台账（每完成一个任务就加一行）

| 任务 | 状态      | commit | 关键数字 / 备注    |
| ---- | --------- | ------ | ------------------ |
| T0   | ✅ 完成   |        | 基线快照已填（见文末）；逃逸残留 6 文件/6.14MB 已报告 |
| T1   | ✅ 完成   |        | pytest **281 passed**/0 failed；validate 22/22；`--purpose` 默认 all 并**传到 run_suite**（用例钉住）；空集诊断与 noise-floor 共用 |
| T2   | ✅ 完成   |        | pytest **284 passed**；validate 22/22；`bench show` + `bench sample-failures` 已在真实库验证（工具序列/失败检查/关键产物/自述不一致告警）；新增 3 用例（自带数据，CI 可跑） |
| T3   | ✅ 完成   |        | pytest **286 passed**；validate 22/22；`store.cost_by()` + 报告"一·六"节；归因总额 **$3.7661 == 库里成本 $3.7661（0.00%）**；**核对时发现并修了一个真 bug**（工具归因不能用 span 树 → 改按时间归给紧随其后的工具调用） |
| T4   | ✅ 完成   |        | pytest **286 passed**；validate **22/22**（新增歧义 lint 告警行）；清单+假阳性分析写入 `docs/task-ambiguity-lint.md`（1 个任务被标：`syn_rename_files`，规则型任务不应写死文件名） |
| T5   | ✅ 完成   |        | `bench purpose-audit` 输出对账表（真实库 22 任务，标出 5 个不一致并给出 `bench show` 深挖命令）；口径=只算 `faults_injected=0`（有测试）；规则有单测；工具自述「基于全部历史」的局限；pytest 288 passed；validate 22/22 |
| T6   | ✅ 完成（**负结果**） |        | 42/42 成功、实际 **$3.71**；两臂均 21/21=100%（配对 +0.0pt）；机制=**Agent 重读**（注入次数与 `read_file` 次数一一对应）；成本/工具 +6.7%/+6.2% **区间跨 0 且在地板内**；`docs/step9-readpath-2x2.md` 已写（能说/不能说两表）；⚠️ 跑完后用户把模型换成了 `deepseek-v4.1-flash` → **新模型的地板必须先重测**（T13 提前），否则后续付费 delta 不可判读 |
| T7   | ✅ 完成（**负结果**） |        | 42/42、两臂各 **21/21 = 100%**；注入↔重读一一对应（inj=1/2/3/5 → read=2/3/4/9）；加固臂 6 条内容级告警；成本/工具 +3.9%/+5.2% **在地板（±46.8%）内且区间跨 0**；实际 **$1.317**（预估 $11.91）；`docs/step9b-silent-wrong-2x2.md` 已写（含三次实验的统一机制 + 下一个可证伪方向） |
| T8   | ✅ 完成   |        | ci 组 **2→8**；`bench validate` → **28/28**；6 个新任务各有**真跑捕获**的日志（`_capture_logs.py`）+ 症状级错误解 + 日志里没有的边界用例；pytest **288 passed**；过程中修了 3 个生成器 bug（import 拼装 / 模块名推导 / naive-UTC 用例）与一个写入路径的动态 SQL（改成静态 INSERT + 列集对账看守） |
| T9   | ✅ 完成   |        | ci 组 8 任务 × 3 = 24 次；**22/24 = 91.7% [74.2%, 97.7%]**；**pass^3 = 7/8 = 87.5%**、pass@3 = 100%；唯一不稳定 = `ci_shared_cache_state`(1/3)（一次 task_timeout 312s>300s、一次 LLM 重试耗尽）；实际花费 **$1.756**（预估 $9.54）；`docs/step10-ci-baseline.md` 已写（含样本量/独立性/地板三条 caveat） |
| T10  | ✅ 完成   |        | `lab/sut/minimal_react.py`（无框架：while + messages + 原生 tool_calls + 步数上限）；工具层**复用 SUT 的**并过同一个 guard 代理 → 对照只差 loop；3 个 hermetic 用例（含"未知工具回灌而非抛出"与"步数上限"）；真模型冒烟 1/1（`sut_name=minimal-react[fast]`、tools=2、$0.0009）；pytest **291 passed**；validate 28/28 |
| T13  | ✅ 完成   |        | 同一 7 任务集重测（42/42 成功、实际 **$0.886**）；**新模型地板：成本 ±46.83% / token ±49.08% / 耗时 ±29.32% / 工具 ±36.04%**（比旧模型 ±16.6% 宽得多）；`lab/runs/noise_floor.json` 已更新（suite `62bde6db`）；`bench gate` 已读到；README 与台账数字已对齐 |
| T14  | ✅ 完成   |        | `judge.py`（按维度**独立调用**、`Unknown` 不计入 κ）+ `calibrate.py`（Cohen's κ + 每维混淆矩阵 + **κ<0.6 或样本<20 就拒绝启用**）；**100 条标注 CSV 已生成**（`lab/runs/judge_gold.csv`，失败优先再补成功，不含 judge 列以防锚定）；`bench judge-sheet` / `bench calibrate` 两个 $0 命令；pytest **299 passed**；validate 28/28；⚠️ **未声称已校准**（真实 κ 等用户标注） |
| T11  | ✅ 完成   |        | `LICENSE`（MIT）；`lab/requirements.txt`（CI 改读它，本地/CI 同一份）；`README.en.md`；根 README 定位改为**测量台为主角**；`pyrightconfig.json`（修正工具链路径 → 消除 app.*/lab.* 的假阳告警）；**干净 venv 实测：只装 requirements.txt → pytest 全绿 + validate 28/28** |
| T12  | ✅ 完成   |        | 根 README 顶部「已实测的数字」表（**每条带限制/口径**，含新模型地板 ±46.8%）；「我错在哪」补到 **11 条**（含 `ruff --fix` 误伤全仓那次）；**逐条核验 7 项完成标志 → 7/7**（脚本化核验，$0）；⚠️ 目标约束里写的“地板 ±16.6%”已在换模型后失效（现为 ±46.83%），需 /goal-tweak 更新 |

### 队列之后的追加工作（2026-10-09，**队列外**）

> 队列的 7 项完成标志已在 T12 逐条核验 → **7/7**。以下是同一天追加的 **LLM-judge 校准系列**，
> **不属于本队列**；记在这里只为台账不断档，细节见 `docs/step11~16-*.md` 与 `docs/judge-labeling-guide.md`。
> T12 那条尾巴（“地板 ±16.6% 待更新”）已在 T13 处置：地板现为 **±46.83%**，根 README / `lab/README.md` / 台账三处一致。

| 工作 | 状态 | 关键数字 / 备注 |
|---|---|---|
| Step 11 judge 首次校准 | ⛔ 已撤回 | κ = 1.000 / 0.419 / 0.693，但三个 κ 因**装置缺陷**不可信（工作区覆盖），顶部已加撤回警告 |
| Step 12 工作区覆盖 | ✅ 已修 | 地基缺陷：100 条校准样本里 **39 条**产物被后来的运行覆盖；按 suite 隔离 + 新鲜度守门 + `artifact_digest` 落库 |
| Step 13 校准重做（v2 表） | ✅ 完成 | 分级抽样 + **111 条**，κ = 0.978 / **0.215** / 0.958 → **拒绝启用**（IF < 0.6）；并写清“金标是 AI 标注”这条致命 caveat |
| Step 14 rubric v1 → v2 | ✅ 完成 | 把维度边界写进提示词：IF **0.208 → 0.556**（OQ 0.978 → 0.842），仍拒绝启用 |
| Step 15 rubric v2 → v3 | ✅ 完成 | 收窄“命名”：**第一次跨过门禁**（0.978 / **1.000** / 0.912）；三臂对照分歧 **33 → 4**；顺带修“报告读当前默认版本而非产生该批判定的版本”的真 bug。另：采样不再抽 `empty`（不进 κ），v4 表 **95 条**；TC 口径定案为 ①「交付物完整性」 |
| Step 16 盲标复测 | ✅ 完成（**$0**） | 95 条逐条复现（**0/760 不一致**），三种引出方式 κ 完全一致（**0.889 / 1.000 / 0.869**，门禁 ✅）；⚠️ 仍是**同一位标注者**的重测（intra-rater），**不是**独立人类 → judge 仍只算**辅助信号** |
| 文档同步 | ✅ 完成 | 根 README（中/英）、`lab/README.md`、`docs/judge-labeling-guide.md` 已从“尚未校准 / 两轮拒绝”改为 v3 达标 + 盲标复测；pytest **355 passed**、`bench validate` **28/28** |
| Step 17 分辨率预算（**$0 再分析 → 已执行**） | ✅ 完成 | 地板 ±46.8% 的 **78% 不随每臂次数下降**（方差分解：运行噪声 0.077 = 22%、任务/臂级系统差 0.276 = 78%）→ 预测 k 3→12 只降 **~3pt**，而 n 7→18 才是有量级的杠杆；median / 截尾口径**反而更宽**（±54% / ±53%）；另查出 `regression` 组实有 **18** 个任务而地板只在 **7** 个上测（口径不匹配，补测约 **$2.3** 预计 → ~31%）；半宽自身的 90% 区间是 **[12%, 58%]**。⚠️ **§十二 更正**：上面那套“加次数饱和、加任务才有量级”的结论是 **7 个 semireal 任务**的属性；换成 **18 个 regression 任务**后系统差从 78% 掉到 ≈0，**加次数反而变成最划算的手段**（$1.75 → ~12.3%）—— “加次数有没有用”取决于任务集的重尾程度，换任务集必须重算。见 `docs/step17-resolution-budget.md` |
| **地板重测：7 → 18 个 regression 任务** | ✅ 完成 | 108 次 / 92 分钟 / **实际 $1.732**（预估 $2.29）；成本地板 **±46.83% → ±16.10%**（token ±49.08% → ±16.85%、工具 ±36.04% → ±14.51%）；⚠️ **耗时反而变差**（±29.32% → ±42.28%，被个别 synthetic 任务主导）；**成功率地板 ±0.00pt → ±9.26pt**（108 次里 **6 次真失败**：4 次产物没产出 / 1 次 timeout / 1 次重试耗尽）→ regression 组实为 **94.4%** 稳定，默认 5pt 阈值比地板还紧；`bench gate` 已会当场报口径不一致；旧地板备份在 `lab/runs/noise_floor_7tasks_semireal.json` |
| **预估成本按模型筛**（真缺陷修复） | ✅ 完成 | 跑重测时预估 **$9.58**，而 step17 §七 写的是 ≈$2.3。根因：`SpanStore.avg_cost_for_group` **不筛模型**，而 semireal 组 `deepseek-reasoner` $0.1086/次 vs `deepseek-v4.1-flash` **$0.0212/次** → 对当前模型**高估 4.5 倍**（与该方法 docstring 里记的“曾经低估 3 倍”是同一类错误的两个方向）。已修：新增 `model_name` 过滤 + 调用点传当前模型 + 用例 `test_avg_cost_for_group_filters_by_model`；修后预估 **$2.29** |
| **加次数 3 → 6**（③） | ✅ 完成 | 216 次 / **实际 $3.656**（预估 $4.52）；成本地板 **±16.10% → ±15.15%**（**只降 0.95pt**，而预测是降 3.8pt → **预测失败**）；成功率地板 **±9.26pt → ±5.56pt**；耗时继续变差（±42.28% → ±47.51%）。⚠️ **两次预测失败已自纠**：step17 §十二 曾据 3 次数据宣布“加次数有效”，根因是**把负的方差估计截断成 0**（见 §十三） |
| ~~装置系统偏差~~（§十三，**已撤回**） | ❌ 撤回 | 曾据“6.48pt > 分辨率 ±5.56pt”判定装置有系统偏差。**撤回原因**：分辨率**不是显著性检验** —— 按汇总样本量算，两臂差异 **Fisher p = 0.064（不显著）**；逐任务配对 7:1（p ≈ 0.07）。见 step17 §十四 |
| **失败机制查明：planner 空转**（**重要发现**） | ✅ 完成（**$0**） | 216 次里 **16 次（7.4%）`tool_calls=0`** 且 `answer` 是**计划文本**（而成功组 **0/199** 有这种形态）；机制：planner 返回 list 而非 object → 执行器不启动。**16 次里 15 次自述成功**（假阳性率 **94%**），`error_type=None`（SUT 不认为是错误）→ 判定器抓住全部 16 次。这是一条**关于 Agent 本身**的可证伪结论（详见 `docs/step17-resolution-budget.md` §十四） |
| **空转识别进测量台** | ✅ 完成（**$0**） | 新增 `bench idle-failures`（按模型报空转率 + “空转占失败”，全库：v4.1-flash **4.9%/84%** vs reasoner **0.5%/10%**）；`SuiteResult.error_attribution()` 把空转从 `verification_failed` 拆成 **`idle_no_tool_call`**；`bench show` / `sample-failures` 摘要直接标“⚠️ 空转”；pytest **360 passed**（+3 用例）。**未动 `api/`**（修 SUT 会作废历史基线，另案讨论） |
| **SUT 缺陷修复 `[lab/D11]`**（动 `api/`） | ✅ 代码完成，⚠️ **未真模型验证** | 机制（轨迹级证据）：模型在 `Step` 里诚实写 `success=false`，而 `react.execute_step` **无条件**把 `step.status` 置成 COMPLETED（**两处重复赋值**）→ “失败的步骤”被记成“完成” → 流程以“任务成功”结束（`self_reported_ok=True`），而产物不存在。修两处：① `agents/react.py` 让 status 反映 `success`；② `flows/planner_react.py` 收尾时若**无任何步骤成功** → 上报 ErrorEvent（否则 adapter 的 `ok` 仍是 True）。验收用例 **D11** 通过；pytest **361 passed**、validate 28/28。⚠️ ScriptedLLM 只证明“代码逻辑对了”，**不证明真模型空转率降了**（需重跑 ≈$1.7） |
| **D11 修复的真模型验证** | ✅ 完成（**$0.893**） | suite `194c8daf`（18 任务 × 3 次 = 54 次，预估 $1.13）；**四条预注册指标全部符合**：失败的 `error_type` **`None`×16 → `agent_error`×2**；**假阳性（自述成功∧判定失败）16（94%）→ 0**；`steps_failed` 正确统计（12）；**空转次数 7.4% → 3.7%**（预期不降 —— 修的是“报告”不是“模型”）。两次失败：一个走 D11（12 步全没做成）、一个走 D9（计划为空）。⚠️ 成功率 92.1% → 96.3% **不能**读成“修复提升了成功率”（样本量不同 + api/ 行为已变，不可直接比）。见 `docs/step17` §十七 |

**基线快照（T0 填，2026-09-20）**：

| 项 | 值 |
|---|---|
| `git log -1` | `d710d78`（① 装置保真：CRLF 归一 + 虚拟盘符 / ② 环境指纹 + 确定性 PATH） |
| pytest | **279 passed / 0 failed / 0 skipped**（`--junitxml` 实测；⚠️ 之前口头说的 287 是未核实的算术，已纠正） |
| `bench validate` | **22/22** |
| 地板③ | 成本 **±16.60%** / token ±17.21% / 耗时 ±13.37% / 工具调用 ±16.37%（`lab/runs/noise_floor.json`，suite `b9ec82bc`，7 任务） |
| 待提交文件 | `docs/step8-device-fixes.md`、`lab/README.md`、`lab/bench/tasks/semireal/07_sqlite_report.yaml`、`plans/`（未跟踪） |
| 逃逸残留 | `D:\home\ubuntu`：**6 个文件 / 6.14 MB**（dry-run 报告，待用户手动 `clean-escapes --yes`） |
