# Step 6 前置体检：对账「推荐 1（EvalOps）」与「推荐 4（可靠性工程）」

> 生成时间：本文件写入时。
> **性质：项目自评，不是 Step 1~5 的完成记录。** 它回答两个问题：
> 1. 外部给的两条推荐（EvalOps / 可靠性工程）**到底做完了没有**？
> 2. Step 6（开源化）之前，**还差什么**？
>
> **基线声明**：本文件的结论来自读代码 + 查 `lab/runs/traces.db`，
> **部分数字是本次现场重算的，不是历史报告里的数字** —— 凡是重算的都单独标注。
> 全部涉及本项目的内容标 `[PROJECT:mooc-manus]`。
>
> 相关但**不重复**的文件：
> - `AI-Research/skills/agent-evals/references/11-mapping-to-mooc-manus.md`
>   —— 对外部文章做对账，产出补盲项 G1~G7，有严格的来源标记规则；
>   本文件是**对内做对账**（对两条工程推荐），且不遵守那套标记规则，故不合并。
> - `docs/step1~5-*.md` —— 各 Step 的完成记录与实测数据。

---

## 0. 一句话结论

| 推荐 | 机制完成度 | 实验证据完成度 | 一句话 |
|---|---|---|---|
| **推荐 4（可靠性工程）** | **~90%** | **~40%** | 故障注入 + 加固中间件**已做完且质量高**；但「加固前后成功率曲线」这个**核心讲法未落地** —— 10 类故障只实测过 1 类（6 次注入），且**没有关掉加固的开关**，拿不出对照曲线 |
| **推荐 1（EvalOps）** | **~60%** | — | 测量仪器骨架全部到位；5 个子项里 **2 个完全为零**（`pass@k`、LLM-judge κ 校准），1 个半成品（回归看门狗） |

**状态画像**：资产是「一套很讲究的方法论 + 一堆诚实的限制声明」；
缺的是**能写进简历的那条曲线**。

---

## 1. 推荐 1（EvalOps）逐项对账

| 子项 | 状态 | 证据 / 缺口 |
|---|---|---|
| ① Trace 数据模型<br>（span 嵌套 / 工具调用 / token / 成本 / 延迟 / **可回放**） | ✅ 5/6 | `lab/trace/span.py`：`Span.parent_id`、`SpanKind.TASK/STEP/TOOL/LLM`、相对任务开始的毫秒偏移（便于两条 trace 叠加对比）<br>`lab/trace/store.py`：SQLite 落盘，`tasks` / `spans` / `bench_suites` / `bench_runs` 四张表 + ALTER 迁移<br>`lab/trace/view.py`：文本树渲染（因"当前模型不支持读图"而**刻意选文本**，可 grep、可进 CI）<br>`view.py:56`：LLM span 带 `prompt_tokens` / `completion_tokens`<br>🔴 **「可回放」缺**：只有 `bench recover`（从 workspace + trace **重建判定**，`lab/bench/recover.py`），**没有 LLM 调用的录制/回放** → 调试一次失败必须重新花钱调模型，评测无法离线复现 |
| ② 评测执行器<br>（并行跑 N 次 / **pass@k** / 方差 / 置信区间） | ⚠️ 3/4 | 并行 ✅ `lab/bench/runner.py`：`asyncio.Semaphore`（`:431`）+ `as_completed`（而非 `gather`）以支持**每次运行完成即增量落库**<br>置信区间 ✅ `lab/bench/stats.py`：Wilson（比率）/ t95（连续量）/ bootstrap / 配对 bootstrap / 经验分位数<br>方差 ✅ `runner.py` 的 `TaskSummary.unstable` + `report.py` 第四章「不稳定任务」<br>🔴 **`pass@k` / `pass^k` 完全没有** |
| ③ 失败归因<br>（定位 / 规划 / 验证 / 工具 / **上下文腐化**） | ⚠️ 部分 | ✅ `runner.py` 的 `error_attribution()`：按 `error_type` 统计，并把工具级失败合并为 `tool:*`（"Agent 说成功但工具层出过错"不会被漏掉）<br>✅ `process_flags`：`too_many_tool_calls` / `low_action_diversity` / `hardcoded_answer` / `path_escape` / `writes_outside_workspace`<br>⚠️ 归因目前是**计数**，不是自动的五分类<br>🔴 **「上下文腐化」零观测**：没有 token 增长曲线 vs 成功率、没有"重复动作占比对失败的贡献"分析（`action_diversity` 只是 flag，未做成连续归因变量） |
| ④ LLM-as-judge 校准<br>（人工金标 + Cohen's κ） | ❌ **0%** | `lab/bench/task.py:46` 明确写"刻意**不支持** LLM judge"（依据：handoff 决策 2，先建确定性基线）<br>无 `judge.py`，无 `calibrate.py`，无人工金标数据集<br>⚠️ handoff 里的 **κ=0.74 是计划数字，不是实测** —— `11-mapping-to-mooc-manus.md` §5 专门警告过不得表述为"已做 κ 校准" |
| ⑤ 成本归因 + 回归看门狗<br>（改 prompt/工具 → 自动跑基准 → 输出 delta） | ⚠️ 部分 | 成本：**task 级** ✅（`bench_runs.cost_usd`），LLM span 有 token 但**无 span/step 级成本聚合函数**（grep `cost_by` / `per_tool` 无结果）<br>看门狗：`bench compare` / `bench pareto` **手动** ✅（`compare.py:183 compare_suites` / `:255 pareto_points`）<br>🔴 **"自动跑基准 + 输出 delta + 门禁"没有**：无 `.github/`，无 threshold gate，无退出码语义 |

---

## 2. 推荐 4（可靠性工程）逐项对账

| 子项 | 状态 | 证据 / 缺口 |
|---|---|---|
| 故障注入 10 类 | ✅ 定义完整，质量高 | `lab/faults/kinds.py`：10 类，**按"只有什么能力才抓得住它"分类**（显式失败 → 重试/熔断；静默失败 → 后置校验）<br>`lab/faults/injector.py`：<br>　• **按调用序号**判定而非按概率（保证 `transient_error` 的"前 N 次失败"可复现，否则无法确认重试是否生效）<br>　• `partial_write` **真的截断内容**，而不是伪造一个"只写了一半"的报告（这样后置校验比对的是真实文件）<br>　• 只读工具白名单，**绝不篡改 `write_file` 的结果**（那是伪造成功，会让 workspace 与 trace 不一致）<br>🔴 **但只实测过 `partial_write` 6 次**（`docs/step3-completion-report.md:119`），**没有 10 类 × N 次的故障矩阵** |
| 自愈 / 加固中间件 | ✅ 完成 | `lab/middleware.py` 的 `GuardedTool`：靠依赖注入代理，**SUT 零改动**；中间件**永不抛异常**（避免与 SUT 的 `_invoke_tool` 形成双重试）<br>`guard/retry.py`：**幂等性感知**（"该不该重试"≠"失败是否暂态"）<br>`guard/budget.py`：observe / degrade / enforce 三模式 + `from_metrics()` **按历史分位数推导阈值**（P95 软线 / P99 硬线，且硬线有下限倍数防小样本误杀）<br>`guard/loop_guard.py`：连续重复 + 动作多样性<br>`guard/postcondition.py`：6 类后置校验 |
| **加固前后成功率曲线** | ❌ **未完成** | 三个硬问题，见下 |

### 2.1 为什么「加固前后曲线」是不成立的

1. **没有 guard 开关。**
   `ToolGuard.__init__` 有 `verify_postconditions` / `max_attempts`（`middleware.py:85-86`），
   但 `cli.py` / `bench` / `config.py` **都不可配**（grep 确认只有 `middleware.py` 内部引用）。
   加固**永远是开的** → 物理上无法做"无加固 vs 加固"对照。

2. **现有的 v1 → v3 对比不是加固对比。**
   `docs/baseline-v3-comparison.md` 里的"v3 加固后"指的是
   **SUT 的三处兜底修复**（D10 结构化输出兜底 + 工具层兜底 + 收尾兜底），
   不是 `lab/guard/` 这层中间件。
   而且该文件自己**已正确判定不能作因果结论**：
   - 两组时间不重叠（18:37–19:16 vs 21:02–21:40）→ 服务端漂移混淆；
   - 机制上说不通（三处修复**只会增加** token，两次崩溃**只会减少** token，所以 -22% 不可能来自改动）。

3. **实测曲线本身不显著。**
   semireal 成功率 **95.0% [83.5%, 98.6%] → 100.0% [91.2%, 100%]**，Wilson 区间**重叠**。
   即不是模板里那种戏剧性的 31% → 79%。

### 2.2 一个可继续使用的部分

故障注入**接线已经通了**：`cli.py:325` 调 `fault_rules_from_spec`，
`bench run --fault <kind> --fault-tool <glob> --fault-rate <r>` 可用。
所以补实验**不需要新写注入逻辑**，只需要一个 guard 开关（见 §3.2）。

---

## 3. 最有价值的三个改进（按 ROI 排序）

### 3.1 🥇 `pass^k`：零成本，立刻把"不显著"变成真数字

**数据来源：本次从 `lab/runs/traces.db` 现场 GROUP BY 重算，非历史报告数字。**

| suite | 场景 | pass^5（该任务 5 次全对） | pass@5（至少 1 次对） | 二元成功率 |
|---|---|---|---|---|
| `70a79a26` | semireal v1（8 任务 × 5） | **6/8 = 75%** | 8/8 = 100% | 38/40 = 95.0% |
| `80caef04` | semireal v3（8 任务 × 5） | **8/8 = 100%** | 8/8 = 100% | 40/40 = 100.0% |
| `4cc51bc2` | synthetic 修正前（12 × 5） | 10/12 = 83% | 10/12 = 83% | 50/60 = 83.3% |
| `6895569c` | synthetic 修正后（12 × 5） | 12/12 = 100% | 12/12 = 100% | 60/60 = 100.0% |

v1 的两个不稳定任务是 `sem_json_to_csv`（4/5）与 `sem_markdown_toc`（4/5）。

**这就是 `pass@k` 要给你的东西**：二元口径 95% → 100% 区间重叠、无法下结论；
换成 **pass^5 就是 75% → 100%** —— 因为"8 个任务里 2 个不稳定"这件事
在二元口径下被 40 次运行平摊掉了，`pass^k` 把它直接暴露。

**落地**：
- `lab/bench/stats.py` 增加 `pass_pow_k(successes_per_task, k)`；
- **必须先按 task 分组**（定义就是"任务级 k 次全对"，不是全局成功率连乘），
  `compare.py:230 _group_by_task` 可复用；
- `report.py` 第一部分把 `pass@1` 与 `pass^k` **并列**输出，并打印 k 值；
- 同时在报告里声明**独立性假设**：`pass^k` 假设试验独立，
  而 `runner.py` 支持并发 → 并发运行会引入相关失败，必须标注。

> ⚠️ **回源要求**（来自 `11-mapping-to-mooc-manus.md` G1）：
> `pass@k` 的无偏估计公式**不来自** Root Article，
> 不要从文章的 `(0.75)³` 例子反推通用公式，实现前必须回源核对。

### 3.2 🥈 加固开关 + 故障矩阵：把推荐 4 的核心讲法补上

目标形态：

```bash
# 无加固
python -m lab bench run --runs 5 --group semireal \
  --fault partial_write,malformed_result,timeout --guard none
# 加固
python -m lab bench run --runs 5 --group semireal \
  --fault partial_write,malformed_result,timeout --guard all
```

**改动很小**：
1. 把 `ToolGuard` 的 `verify_postconditions` / `max_attempts` 接出来，
   加 `--guard {none,retry,budget,loop,postcondition,all}` 与 `LAB_GUARD` 环境变量；
2. `RunOutcome` 增加 `guard_config` 字段（**没有它，两组数据无法区分是谁的**，
   报告里也就无法归因）；
3. 注入逻辑**不用改**（已通）。

**优先只跑这 3 类**，因为它们正好对应三种不同能力：

| 故障 | 只有什么能力能救它 |
|---|---|
| `timeout` | 超时控制 + 幂等重试 |
| `permanent_error` | 熔断（别把预算烧在必败上） |
| `malformed_result` / `partial_write` | 后置校验（静默失败只能靠它发现） |

剩下 7 类先各跑一次确认"真的注入了"即可。
**这一步之后才有资格说"加固把成功率从 X% 提到 Y%"** ——
现在的 95% → 100% 是修 bug 的功劳，不是加固的功劳。

### 3.3 🥉 回归看门狗 + CI：零成本，面试直接加分

现状：`bench validate`（20/20 自检 + 同义反复检测）**本来就不花钱**，
是天然的 CI 入口，但**没有 `.github/`**（已确认目录不存在），
也没有任何 threshold gate。

建议：
1. `validate` + 全量 pytest（**153 passed**）进 CI —— 完全免费；
2. 新增 `bench gate --suite <baseline> --max-regression 5pt --max-cost-regression 20%`：
   把 `compare.py` 的配对 bootstrap 结果变成**退出码**（区间跨 0 + 超阈值 → 失败）；
3. 这样"回归看门狗"才是**一句能演示的话**：
   "改 prompt/工具后一条命令跑基准，delta 超阈值直接 CI 失败"，
   而不是一个人肉执行 `bench compare`。

---

## 4. 其他改进项（按性价比，不必全做）

| # | 项 | 为什么 | 成本 |
|---|---|---|---|
| 4 | **噪声地板（test-retest）** | `docs/step5-report.md` §2.4 自己承认这是"最大遗留缺口"：**没测过同一份代码跑两次差多少，所以 22% 的差异无法判断是否超过测量分辨率**。补上它，此前所有 A/B 才有意义 | 一轮重复跑（≈$4） |
| 5 | **交错 A/B（interleaved）** | 已正确诊断出"两组跑在不同时段 → 时间混淆"。把 A/B 交替排（A,B,A,B…）是唯一解法，代码上就是 task × run 的调度顺序 | 小改 `runner.py` |
| 6 | **partial credit** | `CheckResult.ok` 是 bool，20 个任务全是全或无。加 `weight` + `required` + `scoring: binary\|weighted`，**保留二元通过率**以兼容历史基线 | 中 |
| 7 | **LLM-judge + Cohen's κ** | 唯一完全为零的高分项。**不必推翻现有体系**：新增 `judge.py`（**按维度独立调用**，允许返回 `"Unknown"` 且**不计入 κ**）+ `calibrate.py`（人工金标 → κ + 每维混淆矩阵，低于阈值**拒绝启用**）；rubric 版本号写进 task YAML | 高（需人工标注） |
| 8 | **`bench show` / `sample-failures`** | 文章 Step 6 要求"读 transcript"，但项目**没有读 transcript 的入口**（`view.py` 只有渲染函数，CLI 未接）。加 `bench show <suite> <task> --run N` 与 `bench sample-failures --n 5`，把"读轨迹"变成一条命令 | 低 |
| 9 | **capability / regression 分离 + 饱和告警** | semireal 已 100%，按文章说法**"100% 的 eval 只能追踪回归，没有改进信号"**。加 `purpose: capability\|regression`（与 `group` 正交）+ Wilson 下界 > 0.95 时打"已饱和"标 | 低 |
| 10 | **歧义 lint** | `validate.py` 加一条：`VerifyCheck.path` 里的路径是否在 `goal` 中被提及；未提及 → warning。已被 `sem_markdown_toc` 的歧义咬过一次 | 低 |
| 11 | **LLM 录制回放** | "可回放"的真实缺口。按 prompt hash 落盘 LLM 响应，`--replay` 时离线重跑。**收益：调试失败不再花钱、评测可离线复现** | 中 |
| 12 | **span / step 级成本归因** | 现在只有 task 级成本 + LLM span 的 token。加 `cost_by(kind='step'\|'tool')` 聚合，"钱花在哪一步"才回答得出来 | 低 |

---

## 5. 面试风险提示（比代码更该处理）

1. **最大风险：拿不出"加固有效"的数字。**
   面试官要的是曲线，现在只有"95% → 100% 且区间重叠"，
   而 10 类故障只实测 1 类。
   **在补完 §3.2 之前，"故障注入 + 自愈"这段讲起来是虚的。**

2. **`pass@k` 缺失会被直接问倒。**
   它同时是成本最低的一个（§3.1，零成本、已有数据）。

3. **项目名与仓库定位。**
   目录仍叫 `mooc-manus`，根 `README.md` 是"仿 Manus 一键部署文档"；
   `lab/README.md` **不存在**，`.github/` 不存在，
   git log 停在 step5（`3876e20 已有数据做出了加固前后对比、失效归因和 Pareto`）
   —— 即 **Step 6（开源化）一行没做**。
   面试官打开仓库的第一眼决定了他问什么。

4. **"你评的是什么"仍可能露馅。**
   20 个任务 = 12 个合成算法题 + 8 个数据处理题，且 `goal` 必须写清输出路径
   （`lab/bench/task.py` 自己承认"比真实场景更规定化"）。
   现在**不缺真实负载**（SUT 是自研 Agent），**缺一个有说服力的业务场景**。
   建议加一类：**以测试结果为裁判的 CI 失败归因**（8~10 个任务），
   这是唯一能同时回答"评的是什么"和"有什么用"的一类。

5. **8 个任务仍是同义反复验证**（`docs/baseline-group-comparison.md` 记录曾达 85%），
   期望值只经人工核对 —— 已是已知弱点且会一直显示在自检输出里。
   属于"诚实但没补完"，是机械工作，值得补。

6. **最强的地方要主动讲。**
   `docs/` 里那几次自纠比数字更能证明工程判断力：
   - `ps aux` 在 Git Bash 下看不到 Windows 进程 → **用了一个自己没验证过的观测手段**（`docs/baseline-D-semireal.md` §6）；
   - 任务集期望值写错导致 10 次"失败"全是假的（`docs/baseline-group-comparison.md` §3.1）；
   - 同义反复检测（参考解直接写出期望值 → 自检无法发现期望值错误）；
   - 主动放弃无法归因的成本改善结论（`docs/step5-report.md` §2.3）。
   **但要在 README 里有一段显式的"我错在哪、怎么发现的、改了什么"** ——
   否则这些好料埋在大量中文注释里，面试官看不到。

---

## 6. 行动建议（一句话版）

先花一天做 §3.1（`pass^k`，零成本，立刻多一个 75% → 100% 的硬数字）
与 §3.3（CI + gate，零成本），
再花一轮钱做 §3.2（加固开关 + 3 类故障矩阵）。

**这三件事做完：推荐 1 到 ~75%，推荐 4 才算真正闭环。**

---

## 7. 本文件未做的事（避免误读）

| 未做 | 说明 |
|---|---|
| **未改任何代码** | 本文件只做对账与方案，实际改动见后续 commit |
| **未重跑评测** | 所有数字来自已有数据（`traces.db`）或已有报告；`pass^k` 是本次从库中重算 |
| **未验证外部文章的公式** | `pass@k` / `pass^k` 的解析定义需回源核对，见 §3.1 的 ⚠️ |
| **未对 §2 的完成度百分比做形式化定义** | 那两个百分比是主观汇总，用于排序优先级，不是指标 |
