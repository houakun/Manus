# Step 1 完成记录（验收报告）

> 对应 `agent-engineering-handoff.md` 第 5 节 Step 1 与第 11 节第 5 项。
> 路线：**分两步走** —— 先用老项目打通测量能力（本 Step），Step 4 再写自研 loop 做 A/B。

---

## 一、Step 1 验收结论：**通过**

### 1.1 验收命令与结果

```bash
cd mooc-manus
python -m lab run "把 1 到 100 求和写入 /home/ubuntu/result.txt 并告诉我结果" --json
```

| 验收项 | 要求 | 实测 | 结果 |
|---|---|---|---|
| headless 可调用 | 命令行跑完整任务 | `python -m lab` 子命令 | ✅ |
| 结构化返回 | `TaskResult{ok, answer, steps, usage, cost, error...}` | 见下 | ✅ |
| 不启 Docker | 无沙箱容器 | `LocalSandbox` | ✅ |
| 不启 PostgreSQL | 无数据库 | `InMemoryUnitOfWork` | ✅ |
| 不启 Redis | 无消息队列 | 直连 flow 事件流 | ✅ |
| 退出码可用于 CI | 0 成功 / 1 失败 | `exit_code=0` | ✅ |
| 单元测试 | 关键不变量有回归保护 | **25 passed in 3.18s** | ✅ |

真实运行输出（节选）：

```
ok          = True
sut_name    = manus-planner-react[fast]
plan_title  = 1 到 100 求和并写入文件
steps       = {'total': 2, 'done': 2, 'succeeded': 2, 'failed': 0}
llm_usage   = {'llm_calls': 20, 'llm_errors': 0, 'prompt_tokens': 113966,
               'completion_tokens': 3420, 'total_tokens': 117386,
               'tool_calls': 14, 'llm_latency_ms': 24180}
cost_usd    = 0.070171
elapsed_ms  = 24734
attachments = ['/home/ubuntu/result.txt']
```

产物落盘验证（证明逻辑路径映射生效）：

```
lab/runs/<task_id>/workspace/
└── home/ubuntu/
    ├── result.txt      # 内容: 5050  ← 目标达成
    ├── sum_task.py     # Agent 自己写的脚本
    └── summary.md      # Agent 自己写的总结
```

模型始终认为自己工作在 `/home/ubuntu/`，实际全部落在 workspace 内。

### 1.2 对比 handoff 第 7 节体检清单

| # | 检查项 | Step 1 前 | Step 1 后 |
|---|---|---|---|
| 1 | 能否被程序调用 | 🔴 不合格 | ✅ **`run_task(goal) -> TaskResult` + CLI** |
| 2 | 工具调用是否集中 | 🟠 基本合格 | ✅ 已定位为唯一杠杆点（Step 3 落地中间件） |
| 3 | LLM 调用可复现 | 🟠 半合格 | 🟠 温度已固定 0.0；**但实测仍非确定（见三）** |
| 4 | 记录 token/成本 | 🔴 不合格 | ✅ `CountingLLM` 代理采集 |
| 5 | 错误结构化 | 🟠 半合格 | ✅ `ToolResult.error_type/retryable/attempts` |
| 6 | 终止保证 | 🟠 不合格 | 🟠 已加"单命令超时 + 任务级超时"，预算中间件属 Step 3 |
| 7 | 断点续跑 | 🟡 有雏形 | 🟡 未动（Step 2 做事件日志后再评估） |
| 8 | 工具幂等 | 🔴 不合格 | 🔴 未动（Step 3；已有测试锚点） |
| 9 | prompt 版本标识 | 🔴 不合格 | 🔴 未动（Step 2 做 trace 时顺带记录 hash） |
| 10 | 轻量运行模式 | 🔴 不合格 | ✅ **fast mode 打通** |

**handoff 第 7 节要求的"先做 1、2 两项"已完成。**

---

## 二、交付物清单

### 2.1 新增（14 个文件）

| 文件 | 作用 |
|---|---|
| `lab/bootstrap.py` | lab ↔ SUT 的**唯一**耦合点（sys.path 注入），未来拆仓库只改这里 |
| `lab/usage.py` | token/成本计量模型 + 价格表（A/B 对比必须同一把尺子） |
| `lab/config.py` | 以 SUT `config.yaml` 为真源，环境变量可覆盖；**温度强制 0.0** |
| `lab/sut/base.py` | `TaskResult` / `StepStats` / `SUT` 协议（handoff 决策 6 的落地形态） |
| `lab/sut/manus_adapter.py` | 把 `PlannerReActFlow` 适配成 SUT；事件流 → TaskResult |
| `lab/infra/memory_uow.py` | 内存版 UoW/仓储，摆脱 PG + Redis |
| `lab/infra/local_sandbox.py` | **Sandbox 协议的本地实现**（fast mode 核心，最大的一个文件） |
| `lab/infra/nulls.py` | NullBrowser / NullSearchEngine（结构化失败，非抛异常） |
| `lab/infra/counting_llm.py` | LLM 代理：采集用量后**摘掉私有键**，保证零行为变更 |
| `lab/api.py` | `run_task()` 唯一对外入口 |
| `lab/cli.py` / `lab/__main__.py` | `python -m lab run ...`，退出码可被 CI 消费 |
| `lab/tests/test_local_sandbox.py` | 沙箱 18 个用例（映射/穿越/超时/环境隔离） |
| `lab/tests/test_memory_uow.py` | UoW 7 个用例（共享存储/隔离/隐式提交） |
| `pytest.ini` | lab 测试配置（`asyncio_mode=auto`，避免假绿） |

### 2.2 对 SUT 的改动（5 个文件，共 8 处）

**原计划 4 处，实际 5 处**（多出 D5，见第三节；tools 注入是为 fast mode 服务）：
`tool_result.py` · `agents/base.py` · `tools/base.py` · `openai_llm.py` · `flows/planner_react.py`

| 编号 | 位置 | 改动 | 性质 |
|---|---|---|---|
| S2 | `openai_llm.py:87` | 透出 `_usage/_latency_ms/_model`（私有键，不改原有键） | 能力补齐 |
| D1 | `tools/base.py:116` | `return ValueError(...)` → `raise` | **bug 修复** |
| D3 | `openai_llm.py:90` | `raise ... from e` 补异常链 | **bug 修复** |
| S5 | `agents/base.py` | 新增 `LLMInvocationError`；`_invoke_llm` 重试耗尽抛它；`invoke()` 捕获后 `yield ErrorEvent` | **bug 修复**（SSE 断流） |
| S6 | `agents/base.py:133` | `_invoke_tool` 区分 `error_type`，记录 `attempts` | 能力补齐（**未改重试策略**） |
| S-tools | `planner_react.py` | 新增可选 `tools` 注入参数（默认 `None` = 原行为） | 可测性接缝 |
| D5 | `planner_react.py:208` | `plan` 为 `None` 时不再崩，改为 `yield ErrorEvent` | **bug 修复**（新增发现） |

所有改动都保持向后兼容：`tools=None`、ToolResult 新字段有默认值、
`LLMInvocationError` 继承 `RuntimeError`。

---

## 三、🔴 关键发现：同一个任务，两次运行差 52%

固定 `temperature=0.0` 跑**同一个任务**两次：

| 指标 | 第 1 次 | 第 2 次 | 差异 |
|---|---|---|---|
| 工具调用 | 19 次 | 14 次 | **-26%** |
| LLM 调用 | 25 次 | 20 次 | -20% |
| 总 token | 178,151 | 117,386 | **-34%** |
| 成本 | $0.1084 | $0.0702 | **-35%** |
| 耗时 | 43.6 s | 24.7 s | **-43%** |

**结论（这是本项目最重要的方法论前提）**：

1. `temperature=0` **不等于**可复现。原因至少有三层：
   - 模型服务端本身不保证确定性（MoE 路由、批处理、版本灰度）；
   - SUT 的 Agent loop 是**路径依赖**的：第 3 步多调一次 `shell_execute`，
     后面所有决策的上下文就都不一样了 → 微小差异被循环放大；
   - 工具的返回内容（文件大小、时间戳、进程号）本身带噪声。
2. 因此 **n=1 的对比毫无意义**。handoff 第 8 节写的 `78.3% ± 2.1%（n=5, 95% CI）`
   不是形式主义 —— 从这里算出的方差看，单次对比的信噪比极低，
   很可能把"新方案更好"误判成噪声。
3. **Step 4 的 harness 必须内建**：同一任务重复 n 次、报告均值 ± 置信区间、
   并且区分"成功/失败"（离散）与"token/耗时/成本"（连续）两类指标的不同统计方式。

### 顺带暴露的 SUT 效率问题（正好是评测要量化的东西）

"把 1 到 100 求和"这种一行代码的事：
- 走了 **2 个规划步骤、14~19 次工具调用、20~25 次 LLM 调用**；
- 出现 **连续多次 `shell_execute`**（同一动作反复重试的迹象）；
- 消耗 **11.7 万 token / $0.07** 才得出 `5050`。

这就是 handoff 第 8 节里"上下文压缩 45k → 6.2k（-86%）"要优化的对象，
现在有了**真实基线数字**。

---

## 四、实现过程中额外发现并修复的 4 个问题

| 编号 | 问题 | 症状 | 严重度 | 处理 |
|---|---|---|---|---|
| **D5** | `planner_react.py:208` `self.plan.status` | PlannerAgent 未产出有效 Plan 时 `self.plan` 为 `None` → `AttributeError` 崩溃；`PlanEvent(plan=None)` 还会触发 pydantic 校验错误。弱模型/网络抖动极易触发 | 高 | 已修：改为 `yield ErrorEvent` |
| **D6** | `LocalSandbox` 子进程继承宿主机 `PYTHONHOME` | 本机 `PYTHONHOME` 指向 3.12 而 PATH 里 `python` 是 3.13 → 子进程 `AssertionError: SRE module mismatch`。**最危险的是模型会以为是自己的命令写错了**，浪费多个迭代去"修复"环境问题 | 高 | 已修：子进程环境白名单 + 剥离 `PYTHON*` |
| **D7** | 子进程继承宿主机全部环境变量 | 模型一条 `env` 命令就能读到 `LAB_LLM_API_KEY` 等凭据 → **把提示词注入升级成凭据泄露** | **严重** | 已修：白名单 + `extra_env` 显式放行 |
| **D8** | 超时只杀 shell，不杀进程树 | 2 秒超时的用例让整个测试套件跑了 **60 秒**。孤儿进程继续占用 CPU，**并行跑任务集时会污染其他任务的耗时指标**（数据错了却看不出来） | 高 | 已修：`CREATE_NEW_PROCESS_GROUP` + `taskkill /T` / `os.killpg`。测试套件 **60.93s → 3.18s** |

> D6/D7/D8 都不是 SUT 的 bug，而是"把真沙箱换成本地实现"时**新引入的风险面**。
> 这正是这个项目要证明的价值：**评测环境本身也必须被验证**，
> 否则你会得到"指标在动"的假象。

---

## 五、已知限制（诚实边界）

| 限制 | 影响 | 何时处理 |
|---|---|---|
| fast mode 的 shell 是"跑完即返回"，无持久会话 | 交互式任务、需要维持进程的任务跑不了 | 明确不支持，任务集避开 |
| 命令中沙箱路径靠字符串替换 | heredoc/变量拼接里的路径替换不到 | Step 4 任务集按此边界设计 |
| 无浏览器 / 无网络搜索 | 只覆盖计算/文件 IO/文本处理类任务 | 需要浏览器的任务走真沙箱（接缝已留好） |
| 单次运行非确定（见第三节） | n=1 对比无意义 | **Step 4 harness 必须内建重复与统计** |
| `api/config.yaml` 里有明文真实 API Key | 建议尽快作废并改用 `LAB_LLM_API_KEY` | 建议立即处理 |
| `docs/` 与 handoff 在 `.gitignore` 中 | 本报告不会被提交 | 若想纳管，需调整 `.gitignore` |

---

## 六、下一步（Step 2：trace 埋点）

动手前需要确认的一件事：**Step 2 的 span 采集点放在哪一层？**

| 方案 | 优点 | 缺点 |
|---|---|---|
| **A. 在 SUT 里埋点**（改 `BaseAgent`，通过 contextvar 上报） | 数据最细（能拿到 messages hash、tool args） | 污染 SUT；SUT 要感知 lab 的存在 |
| **B. 在 lab 侧靠事件流重建**（消费 `ToolEvent`/`StepEvent`/`PlanEvent`） | SUT 零侵入；自研 loop 也能接同一套 | 拿不到"第几次 LLM 调用对应哪个工具"的精确配对；token 只能到任务级，到不了 span 级 |

**我的建议：B 为主 + A 补一个最小上报点（复用 CountingLLM 已经建立的模式，不改 SUT）。**
即：span 树由 lab 侧从事件流重建（task → step → tool），LLM span 由 `CountingLLM` 代理从旁记录，
两边用时间戳配对。这样 SUT 保持干净，Step 4 的自研 loop 也能直接接入。

确认后我就开始写 `lab/trace/{span,store,view}.py`。
