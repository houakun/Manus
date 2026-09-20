# Step 1 交付物：真实架构 + 可钩接缝 + 逐文件改造清单

> 对应 `agent-engineering-handoff.md` 第 11 节第 3、4 项。
> 所有行号基于当前代码（`mooc-manus/api`），改动前请用 `grep -n` 复核。
> 原则：**只列事实与接缝，不在此文件里写实现代码**（符合"先讲思路再给代码"的约定）。

---

## Part A. 实际调用链（HTTP → LLM/工具，带 file:line）

```
POST /api/sessions/{id}/chat
└─ interfaces/endpoints/session_routes.py:161        chat()            # SSE 源
   └─ application/services/agent_service.py:129      AgentService.chat()
      ├─ :147 _get_task()  /  :154 _create_task()    → RedisStreamTask（Redis Stream 承载事件）
      └─ domain/services/agent_task_runner.py:361    AgentTaskRunner.invoke(task)
         ├─ :366 await self._sandbox.ensure_sandbox()        🔴 硬依赖 Docker 沙箱
         ├─ :367 MCPTool.initialize()                        🔴 硬依赖 MCP
         ├─ :368 A2ATool.initialize()                        🔴 硬依赖 A2A
         └─ :371 while not await task.input_stream.is_empty()
            ├─ :83  _put_and_add_event()             # 事件落 Redis Stream + DB
            └─ :324 _run_flow(message)
               └─ :333 async for event in self._flow.invoke(message)   🔴 核心接缝
                  └─ flows/planner_react.py:94   PlannerReActFlow.invoke()
                     └─ :134  while True                    # ← 外层状态机
                        ├─ :142 planner.create_plan()       [PLANNING]
                        ├─ :179 react.execute_step()        [EXECUTING]  ← 每步一次
                        ├─ :184 react.compact_memory()      # 上下文压缩
                        ├─ :191 planner.update_plan()       [UPDATING]  ← 增量重规划
                        ├─ :200 react.summarize()           [SUMMARIZING]
                        └─ :213 yield DoneEvent()
                           └─ agents/base.py:209 BaseAgent.invoke()      # ← 内层 ReAct 循环
                              ├─ :77  _invoke_llm()
                              │     └─ :90 await self._llm.invoke(...)   🔴 唯一 LLM 出口
                              │        └─ infrastructure/external/llm/openai_llm.py:50 invoke()
                              │           └─ :87 return response.choices[0].message.model_dump()
                              │                 🔴 response.usage 在此被丢弃
                              └─ :221 for _ in range(max_iterations)     # ← 循环边界
                                 └─ :133 _invoke_tool()                  🔴 工具总杠杆点
                                    └─ tools/base.py:96 BaseTool.invoke()
                                       └─ file / shell / browser / search / message / mcp / a2a
                                          └─ DockerSandbox → sandbox 容器 :8080 / CDP:9222
```

**事件回流**：flow `yield` → `_put_and_add_event()` 写入 `task.output_stream`（Redis Stream）
→ `AgentService.chat()` 消费 → `EventMapper.event_to_sse_event()` → SSE 推前端。

**关键事实（决定改造难度）**：
1. 外层 `while True` 状态机 + 内层 `for ... max_iterations` **两层循环都存在**，handoff 第 6 节问的"是 while + messages 还是状态机"→ **答案是状态机**。
2. 已有**统一工具层**（`@tool` 装饰器 + `BaseTool.invoke()` 反射分发），`_invoke_tool()` 是**唯一调用点** → handoff 决策 4 的"杠杆点"天然存在，改造量小于预估。
3. **没有任何 headless 入口**：事件必须先经 Redis Stream，且 `invoke()` 一开始就 `ensure_sandbox()`。这是 Step 1 必须打通的墙。
4. `Memory` 已持久化到 DB（`session_repository.py:76/80 save_memory/get_memory`）+ `step.done` 标记 + `roll_back()` → **断点续跑有雏形**（体检第 7 项）。

---

## Part B. 可钩接缝清单（12 个，按优先级）

> "缝"= 一个字面上可以插入代码的位置。每个缝只做一件事，避免改动扩散。

| # | 位置 | 插入什么 | 服务层 | 为什么是这里 |
|---|---|---|---|---|
| **S1** | `agents/base.py:90` `await self._llm.invoke(` | `span("llm")` 包裹 + 记录 model/temperature/messages_hash/latency | EvalOps | **唯一 LLM 出口**，一处埋点覆盖 planner/react 全部调用 |
| **S2** | `openai_llm.py:87` `return ...message.model_dump()` | 追加 `_usage` / `_latency_ms` 键（不破坏 dict 契约，最小侵入） | EvalOps | `response.usage` 的唯一获取点 |
| **S3** | `agents/base.py:133` `_invoke_tool()` | **总杠杆点**：budget 检查 → loop_guard → 故障注入 → 重试 → 后置校验 → `span("tool")` | 可靠性 + EvalOps | 所有工具调用的必经之路 |
| **S4** | `agents/base.py:221` `for _ in range(max_iterations)` | 任务级预算（跨 invoke 累计步数 / token / 成本） | 可靠性 | 现有 `max_iterations` 是**单次 invoke** 的上限，不是任务级 |
| **S5** | `agents/base.py:131` `raise RuntimeError(...)` | 改为 `yield ErrorEvent(error_type="llm_exhausted")` | 可靠性 | 现在异常会穿透 flow 导致 **SSE 断流**，前端看不到错误 |
| **S6** | `agents/base.py:147` `ToolResult(success=False, message=err)` | 补 `error_type` / `retryable` / `attempts` 字段 | 可靠性 | 结构化错误是重试策略与归因的前提 |
| **S7** | `tools/base.py:116` `return ValueError(...)` | **修 bug**：改为 `raise` | 可靠性 | 见 Part D / D1 |
| **S8** | `flows/planner_react.py:134` `while True` | 熔断：状态迁移计数上限（防 `PLANNING↔EXECUTING` 抖动死循环） | 可靠性 | 唯一无上限的循环 |
| **S9** | `flows/planner_react.py:94` `invoke()` 入口/出口 | `span("task")` 根 span：task_id / goal / 终态 / 总步数 | EvalOps | 整条 trace 的根节点 |
| **S10** | `agent_task_runner.py:366-368` | **fast mode 开关**：跳过 sandbox/MCP/A2A 初始化 | 工程吞吐 | 决定评测能否跑量（体检第 10 项） |
| **S11** | `agent_task_runner.py:333` `self._flow.invoke()` | 直接消费 flow 事件（绕过 Redis Stream） | EvalOps | headless 入口的基础 |
| **S12** | `interfaces/service_dependencies.py:81` `get_agent_service` | 依赖注入点，注入 FakeLLM/FakeSandbox | 测试 | 不启 Docker 也能跑确定性单测 |

---

## Part C. 逐文件改造清单（Step 1 → Step 3）

### Step 1 — 标准化接口（🔴 先做，不做完无法测量）

| 动作 | 文件 | 内容 | 理由 |
|---|---|---|---|
| 新增 | `lab/sut/base.py` | `SUT` 协议：`async def run(goal) -> TaskResult` | 把"被测对象"抽象成可替换接口 |
| 新增 | `lab/sut/manus_adapter.py` | 适配现有 `PlannerReActFlow`，直连 flow 事件 | 复用课程代码，零侵入 |
| 新增 | `lab/api.py` | `run_task(goal, *, fast_mode, max_steps, max_cost) -> TaskResult` | handoff 决策 6 |
| 新增 | `lab/cli.py` | `python -m lab run "目标"`（argparse） | 可演示、可 CI |
| 新增 | `lab/infra/memory_uow.py` | 内存版 UoW / 内存 SessionRepository | **摆脱 PG + Redis** |
| 改 | `agents/base.py:131` | `raise` → `yield ErrorEvent` | S5 |
| 改 | `agents/base.py:147` | `ToolResult` 补 `error_type` | S6 |
| 改 | `tools/base.py:116` | `return` → `raise` | S7 |
| 改 | `openai_llm.py:87` | 透出 `usage` / latency | S2 |

**验收**：`python -m lab run "把 1..10 求和并写入 result.txt"` 输出结构化 JSON，
**不启动 Docker / PG / Redis**，耗时 < 30s，`ok=true`。

> ⚠️ 关于 fast_mode 的现实：现有 flow 的所有工具都打到沙箱（`FileTool(sandbox)` / `ShellTool(sandbox)`）。
> 因此 fast_mode 需要 `lab/tools/local_fs.py`、`lab/tools/local_shell.py` 两个**本地实现**替换沙箱版。
> 这是 Step 1 里工作量最大的一块，但它是评测吞吐的**前提**——先做 2~3 个本地工具（fs / sqlite / http），
> 覆盖 Step 4 任务集的需求即可，不必全量对齐。

### Step 2 — trace 埋点（🎯 项目地基）

| 动作 | 文件 | 内容 |
|---|---|---|
| 新增 | `lab/trace/span.py` | `Span` 模型 + `contextvars` 传递 + `with span(...)` |
| 新增 | `lab/trace/store.py` | SQLite 落盘：`spans(task_id, span_id, parent_id, name, kind, attrs_json, start, end, tokens, cost, error)` |
| 新增 | `lab/trace/view.py` | 终端树形打印（无视觉通道也能看轨迹） |
| 挂钩 | S1 / S3 / S9 | 埋 3 类 span：task / llm / tool |

**验收**：`runs/<task_id>.sqlite` 里有完整 span 树；`python -m lab trace <task_id>` 打印
`task → llm(×N) / tool(×M)`，并能汇总 tokens / 步数 / 墙钟时间。

### Step 3 — 工具层中间件（🔧 一处改动全局生效）

| 动作 | 文件 | 内容 |
|---|---|---|
| 新增 | `lab/middleware.py` | `async def call_tool(name, args, ctx) -> ToolResult`（handoff 第 4 节示意形态） |
| 新增 | `lab/guard/budget.py` | token / 成本 / 步数 / 墙钟硬上限 |
| 新增 | `lab/guard/loop_guard.py` | 重复动作检测（动作指纹）+ 熔断 |
| 新增 | `lab/guard/retry.py` | 指数退避 + 抖动；**幂等键**保护非幂等工具 |
| 新增 | `lab/faults/kinds.py` | 10 类故障定义 |
| 新增 | `lab/faults/injector.py` | 装饰器/中间件式注入（按 `name` + 概率 + 第 N 次） |
| 改 | `agents/base.py:133 _invoke_tool` | 改为调用 `call_tool()`，自身退化为薄封装 |

**验收**：`FAULT_RATE=0.3 python -m lab run ...` 一处开关生效于所有工具；
故障环境下成功率提升有数字（对应 handoff 第 8 节模板）。

### Step 4+ 预告（不在本次范围）
`lab/harness/{tasks/*.yaml, runner.py, verifier.py, metrics.py, analyze.py}` + `lab/report/`。

---

## Part D. 必修 Bug 清单（🎁 postmortem 素材，每个都要"故意改坏→修好"走一遍）

| ID | 位置 | 症状 | 根因 | 修法 |
|---|---|---|---|---|
| **D1** | `tools/base.py:116` | 调用不存在的工具 → 抛 `AttributeError: 'ValueError' object has no attribute 'model_dump_json'`，而不是清晰的 "工具未找到" | `return ValueError(...)` 应为 `raise`，异常对象被当返回值传下去 | 改 `raise`，并在 `_invoke_tool` 捕获转 `ToolResult(error_type="tool_not_found")` |
| **D2** | `agents/base.py:133-147` | 沙箱超时/网络抖动时，`write_file(append=True)` 或 `shell_execute` **被重复执行**，产生重复内容 / 重复副作用 | 对所有工具一律 `for _ in range(max_retries)` 重试，无幂等判断 | 工具声明 `idempotent: bool`；非幂等工具重试前先做状态探测（如 `check_file_exists` / 幂等键） |
| **D3** | `openai_llm.py:88-90` | 排障时日志只有"调用OpenAI客户端向LLM发起请求出错"，看不到真实原因（超时？限流？429？） | `raise ServerRequestsError(...)` **缺 `from e`**，异常链被截断 | `raise ServerRequestsError(...) from e`，并把 SDK 异常类型写进 `error_type` |
| **D4** | `agents/base.py:131` | 重试耗尽后异常穿透 flow → **SSE 中途断流**，前端只看到"连接中断"没有任何 error 事件 | 用 `raise` 而非 `yield ErrorEvent` 表达可预期的失败 | `yield ErrorEvent(...)`，flow 捕获后置 `FlowStatus.COMPLETED` 并 `yield DoneEvent()` |

**安全项（不在本计划范围，但建议动手前先处理）**
- `api/config.yaml` 明文真实 DeepSeek Key（`sk-724f...`）→ 立即作废，改走环境变量
- 全站零鉴权（无登录 / Authorization）→ 部署前必须加
- `docker-compose.yml` 把 `5432` 映射到宿主；API 容器挂载 `docker.sock`

---

## Part E. 与 handoff 的对齐检查（⚠️ 有一个必须你决策的分叉）

### 已对齐
| handoff 决策 | 现状 | 结论 |
|---|---|---|
| 2 确定性验证优先 | 现有代码无任何评测 | 从 0 开始，无历史包袱 ✅ |
| 3 故障注入做装饰器 | 无 | Step 3 新建 ✅ |
| 4 工具调用集中到 `call_tool` | `_invoke_tool` 已是唯一调用点 | **杠杆点现成**，改造量小 ✅ |
| 5 过程分 + 结果分 | 有 `PlanEvent/StepEvent/ToolEvent` 完整事件流 | 过程分数据源现成 ✅ |
| 6 headless 可调用 | 只有 SSE | 🔴 Step 1 必须新建 |

### 🔴 需要决策：`lab/agent/loop.py` 怎么写？

handoff 第 3 节的目标结构里，`lab/agent/loop.py` 标注为"**自研 agent loop（核心资产，不用框架）**"；
但第 3 节同时又规定 SUT = 你的仿 Manus 项目。两者矛盾，必须二选一：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 改造现有 loop** | 在 `PlannerReActFlow` 上改造，`lab` 只做壳 | 快（省 2~3 周）、代码量大看着唬人 | ⚠️ 面试官问"这个状态机为什么这么设计"时，讲的是**课程作者的代码**；一旦被追问"哪部分是你写的"会很被动 |
| **B. 新写精简 loop，现有项目降级为 baseline SUT** | `lab/agent/loop.py` 自写 ~200 行单循环 loop（配 `lab/sut/manus_adapter.py` 让老项目也接入） | ① 核心资产 100% 自己写，讲得清每一个决策；② 直接产出 handoff 第 8 节要的 **A/B 对照数字**（"课程版 ReAct 62.5% vs 我的 loop + 加固 79%"）——这个对比本身就是简历亮点 | 多 2~3 周；需要自己写工具层（可直接复用 `tools/` 的 schema 声明方式） |

**我的建议：方案 B，但分两步走。**
1. Step 1~3 先在**现有 flow** 上打洞（S1~S12），把 harness 和指标跑通——毕竟"测量仪器"先行才能证明新 loop 真的更好；
2. Step 4 有了 baseline 数字后，再写 `lab/agent/loop.py`，用**同一套任务集**做 A/B 对比。

这样既不浪费课程代码的价值（它是天然的 baseline 对照组），又保证简历上的核心资产是自己写的，
并且天然回答了面试必问的"**你的方案好在哪里，有数据吗**"。

---

## Part F. 下一步动作

1. ✅ 你选定 Part E 的方案（A / B / 分两步）
2. ⬜ 我实现 Step 1 的 5 个新文件 + 4 处改动（每处配中文注释，先讲思路再给代码）
3. ⬜ 跑通 `python -m lab run "..."`，拿到第一个结构化 `TaskResult`
4. ⬜ 你做一次"合上代码用自己的话讲一遍"验收（handoff 第 10 节第 4 条）
