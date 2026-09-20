# Step 2 完成记录（trace 埋点）

> 对应 `agent-engineering-handoff.md` 第 5 节 Step 2（"加 trace 埋点，验收：`runs/` 里有完整轨迹"）。
> 已选方案：**B + 代理**（lab 侧从事件流重建 span 树 + CountingLLM 代理上报 llm span）。

---

## 一、验收结论：**通过**

| 验收项 | 要求 | 实测 | 结果 |
|---|---|---|---|
| 轨迹落盘 | `runs/` 里有完整轨迹 | `lab/runs/traces.db`（SQLite） | ✅ |
| 树形可读 | 无视觉通道下能看懂时序与因果 | `python -m lab trace` 输出树 + 汇总 | ✅ |
| span 级指标 | 能拿到 task / step / tool / llm 四级 | 见下 | ✅ |
| 跨运行统计 | 成功率 / tokens / 成本 / P95 / 归因 | `python -m lab stats` | ✅ |
| 回归测试 | 结构受保护 | **52 passed** | ✅ |
| 零 SUT 侵入 | 不新增 SUT 埋点代码 | 仅 4 处 bug 修复（Step 1）+ 0 处新增 | ✅ |

### 1.1 真实运行的轨迹（`python -m lab trace a1a96cc8`）

```
[OK ] manus-planner-react[fast]  task=a1a96cc8-4c09-4f78-b453-80bccbd49ce5
目标   : 计算 7 乘以 6，把结果写入 /home/ubuntu/m.txt
结果   : steps=1/1 tools=7 llm=11 tokens=48901 cost=$0.0314 elapsed=18828ms
└── [task] ✓ @0ms +18.9s  steps=1/1 plan_revisions=3
    ├── [llm] ✓ @46ms +2.1s    in=1751 out=315 msgs=2  prompt#2ca250f1f061   ← planner
    ├── [step] ✓ 使用 bc 或 Python 精确计算 7×6 @2188ms +10.9s  success=True
    │   ├── [llm]  ✓ @2188ms +1.2s  in=3569 out=144 msgs=2  prompt#41a59dfe8467
    │   ├── [tool] ✓ shell_execute        @3390ms +48ms   out=313c
    │   ├── [llm]  ✓ @3438ms +1.3s  in=3821 out=213 msgs=4  prompt#41a59dfe8467
    │   ├── [tool] ✓ message_notify_user  @4765ms +0ms
    │   ├── [llm]  ✓ @4765ms +1.0s  in=3981 out=112 msgs=6
    │   ├── [tool] ✓ shell_execute        @5813ms +155ms
    │   ├── [llm]  ✓ @5968ms +1.5s  in=4195 out=264 msgs=8
    │   ├── [tool] ✓ shell_execute        @7452ms +141ms
    │   ├── [llm]  ✓ @7593ms +2.1s  in=4643 out=406 msgs=10
    │   ├── [tool] ✓ shell_execute        @9718ms +63ms
    │   ├── [llm]  ✓ @9781ms +1.9s  in=5167 out=283 msgs=12
    │   ├── [tool] ✓ message_notify_user  @11718ms +0ms
    │   └── [llm]  ✓ @11718ms +1.3s in=5489 out=160 msgs=14
    ├── [llm] ✓ @13063ms +843ms  in=3005 out=73 msgs=4  prompt#2ca250f1f061  ← 更新计划
    ├── [llm] ✓ @13906ms +1.0s   in=5170 out=120 msgs=16 prompt#41a59dfe8467  ← 汇总
    ├── [tool] ✓ shell_execute   @14921ms +31ms
    └── [llm] ✓ @14952ms +3.9s   in=5339 out=681 msgs=18

--- 汇总 ---
类型      次数    累计耗时   错误
task       1     18.9s      0
llm       11     18.4s      0
step       1     10.9s      0
tool       7      0.4s      0
```

---

## 二、交付物

### 2.1 新增（4 个源文件 + 3 个测试文件）

| 文件 | 作用 |
|---|---|
| `lab/trace/span.py` | Span 模型 + `TraceRecorder`（span 栈、父子归位、收口） |
| `lab/trace/store.py` | SQLite 落盘 + 跨运行聚合统计（`stats()`） |
| `lab/trace/view.py` | **文本**树形渲染 + 汇总（无视觉通道下的第一等展示方式） |
| `lab/trace/__init__.py` | 包导出 |
| `lab/tests/fakes.py` | **脚本化 LLM**（确定性、零成本、可注入异常） |
| `lab/tests/test_trace.py` | 22 个单测（栈语义 / 指纹 / 落盘 / 渲染） |
| `lab/tests/test_trace_integration.py` | 7 个集成测试（真跑 flow，断言 span 树结构） |

### 2.2 修改

| 文件 | 改动 |
|---|---|
| `lab/infra/counting_llm.py` | 新增 `sink` 参数，产出 llm span（含双指纹、上下文规模） |
| `lab/sut/manus_adapter.py` | 接 `trace` 参数；`StepEvent/ToolEvent` → span；错误链收集 |
| `lab/sut/base.py` | `TaskResult` 新增 `error_chain` |
| `lab/api.py` | 组合根接线；**修正落盘顺序**（见四.2） |
| `lab/cli.py` | 新增 `lab trace` / `lab stats`；支持短 id |

---

## 三、关键设计决策

### 3.1 为什么用"栈"而不是 contextvars

`PlannerReActFlow` 是**协作式 async generator**：消费者在调用 `__anext__` 之前设置状态，
生成器恢复执行时就能读到（PEP 567 下协程不隔离 Context）。所以 recorder 自己维护一个
span 栈、在**事件消费循环**里 push/pop，就得到了正确的 `task → step → {llm, tool}` 嵌套。

- `step` / `tool` → **压栈**（它们包含子动作）
- `llm` → **不压栈**（最内层叶子）

不压栈这点很关键：否则"决定调用工具的 LLM 调用"会变成"工具执行"的父节点，**因果关系就反了**。

> 什么时候必须换成 contextvars：SUT 用 `asyncio.gather` 并发跑多个步骤时栈会串味。现在不引入。

### 3.2 为什么 llm span 由代理上报而不是从事件流重建

事件流里没有 LLM 调用的直接信息（只有工具和步骤）。代理是唯一能在**不碰 SUT** 的前提下
拿到 token 级数据的位置，而且它已经在 Step 1 存在了 —— 复用同一个接缝，不新增侵入点。

### 3.3 prompt 指纹分两个（踩坑后修正）

| 指纹 | 构成 | 语义 |
|---|---|---|
| `prompt_digest` | system prompt + 工具描述 | **稳定** → 标识"提示词/工具集版本"（体检第 9 项） |
| `context_digest` | 全量 messages | **每次变** → 标识"这一次的确切上下文" |

一开始只算了一个（把全量 messages 也算进去），结果每次调用指纹都变，**根本当不了版本标识**。
修正后在真实运行里验证有效：一次任务里只有 2 个 `prompt_digest`
（`#41a59dfe8467` 出现 9 次 = react，`#2ca250f1f061` 出现 2 次 = planner），
正好对应两套 Agent 提示词。

### 3.4 渲染层是文本优先，不是图片

handoff 第 1 节写明当前模型**不能读图**。所以轨迹的第一等展示方式是文本树 + 毫秒偏移，
而不是火焰图。副产品：文本 trace 能 grep、能进 CI 日志、在任何终端都能看。

---

## 四、实测发现（Step 3/4 的直接输入）

### 4.1 🔴 时间几乎全花在等模型：LLM 98%，工具 2%

| 类型 | 次数 | 累计耗时 | 占比 |
|---|---|---|---|
| llm | 11 | 18.4s | **98%** |
| tool | 7 | 0.4s | 2% |

**这条数据直接否掉了一个直觉**："优化工具、让沙箱更快"是没有意义的。
要降耗时只有一个方向：**减少 LLM 调用次数**（11 次算一个乘法）或**降低单次延迟**。

### 4.2 上下文线性膨胀（"上下文爆炸"的实测曲线）

同一次任务里，`prompt_tokens` 与消息数逐轮增长：

```
in=1751 msgs=2  →  3569 → 3821 → 3981 → 4195 → 4643 → 5167 → 5489 → 5339 ...
```

每轮约 +200~300 token，消息数从 2 涨到 18。这是 handoff 第 8 节
"上下文压缩 45k → 6.2k（-86%）"要优化的对象，现在有了**逐轮的测量能力**。

### 4.3 SUT 的效率问题被量化

"7 乘以 6 = 42" 这件事：
- 1 个规划步骤、**7 次工具调用**（其中 4 次 `shell_execute`）、**11 次 LLM 调用**；
- 48,901 token、$0.0314、18.8 秒。

### 4.4 ⚠️ 又一个真 bug（本次引入并修掉）：落盘顺序错误

`run_task` 里原本是「先落盘 → 再补 `cost_usd` / `elapsed_ms`」，
结果**数据库里成本永远是 0**。危险之处在于 0 是个"看起来合理"的数字，不会报错，
要到做基线报告时才会发现成本列全是 0。

修法：把计量字段补齐提到落盘之前；并加了一条回归断言
（`stored["cost_usd"] == result.cost_usd`）把这个顺序锁死。

> 这条发现本身也验证了 Step 2 的价值：**是 trace 输出让我一眼看到 `cost=$0.0000`**。

### 4.5 错误级联：首个错误才是根因

LLM 调用失败时，SUT 会产生**级联错误**：

```
[0] [planner] 调用语言模型失败: 已达到最大重试次数(2): mock 401   ← 根因
[1] Agent未能生成有效的任务计划(Plan 为空)，任务终止              ← 后果
```

原实现里后一个会覆盖前一个，`result.error` 拿到的是"任务终止"这种**零信息量**的末端描述，
失败归因会完全错。已改为：`error` 保留首个（根因），`error_chain` 保留完整链。

---

## 五、已知限制

| 限制 | 影响 | 何时处理 |
|---|---|---|
| 拿不到 SUT 内部私有数据（如迭代序号） | 无法区分"第几轮迭代" | 接受；换取 A/B 双方口径一致 |
| 不建独立的"规划阶段" span | 规划 LLM 调用直接挂在 task 下 | 接受；从顺序仍可看出 |
| span 栈不支持并发步骤 | 若 SUT 改用 `gather` 需换 contextvars | 触发时再改 |
| `traces.db` 中第 1 次运行是修复前写入的（cost=0） | 会拉低 `stats` 的成本均值 | **Step 4 做基线前删掉 `lab/runs/traces.db` 重跑** |
| 统计尚未计置信区间 | n<5 的数字不能下结论 | Step 4 |

---

## 六、下一步（Step 3：工具层中间件）

Step 3 的钩接缝在 Step 1 就已经定位好了：`BaseAgent._invoke_tool`
→ 抽成 `lab/middleware.py: call_tool()`，一处改动让所有工具同时获得：

| 组件 | 作用 | Step 2 已经准备好的观测手段 |
|---|---|---|
| `guard/budget.py` | token / 成本 / 步数 / 墙钟硬上限 | `Usage` + trace 逐轮数据 |
| `guard/loop_guard.py` | 重复动作检测 + 熔断 | `tool_sequence` + `args_digest` |
| `guard/retry.py` | 指数退避 + 抖动 + **幂等键** | `attempts` 字段（D2 归因） |
| `faults/injector.py` | 10 类故障注入（装饰器/中间件式） | `ScriptedLLM` 已支持注入异常 |

**Step 3 需要你确认一件事**：预算超限时的行为是什么？
- **A. 立即中止任务**（`ok=False, error_type="budget_exceeded"`）—— 数据干净，但会丢结果；
- **B. 降级收尾**（强制进入 summarize 阶段，用已有结果交付）—— 更接近生产行为，但引入新路径；
- **C. 只记录不干预**（先把超限事件记进 trace，观察 baseline 用多少预算，再决定阈值）—— 我推荐先 C，
  因为"多大预算算超"必须先有数据才能定，否则阈值就是拍脑袋。

回 **A / B / C** 我就开始写 Step 3。
