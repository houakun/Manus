# SUT 加固记录：结构化输出的统一兜底（D5 / D9 / D10）

> 触发来源：Step 4 的 baseline-D 中 `sem_json_to_csv run0` 以 `sut_crash` 失败。
> 排查后发现它是**同一个根因的第 3 次出现**，所以这次不做逐点补丁，而是做统一处理。

---

## 一、问题：同一个根因的三次显现

| 编号 | 位置 | 触发条件 | 修复前的后果 |
|---|---|---|---|
| **D5** | `planner_react.py` COMPLETED 分支 | `self.plan is None` | `AttributeError` → 整条流崩 |
| **D9** | `planner_react.py` PLANNING 分支 | `plan.steps` 为空 | **虚报成功**（`ok=True`、零工具调用、什么都没做） |
| **D10** | `react.py` `execute_step` | `Step.model_validate` 失败 | `ValidationError` → 整条流崩（实测发生） |

**根因一句话**：提示词要求"必须返回严格 JSON"，但代码没有把"**违反约定**"当成**可预期情况**处理。

D10 的实测形态（模型把转换后的数据数组当答案交了）：

```
ValidationError: 1 validation error for Step
Input should be a valid dictionary or instance of Step
input_value=[{'name': 'monitor', 'price': 320, 'stock': 4}, ...]
```

D9 的实测形态（0 步空计划却自称成功）：

```
[OK ] task=3dc1d79a  steps=0/0  tools=0  llm=1  error=None
└── [task] ✓ @0ms +3.1s
    └── [llm] ✓ in=1785 out=353
```

---

## 二、修法：不是"不崩"，而是"救回来"

### 2.1 为什么不只是 try/except

`try/except` 能让进程不崩，但那是**把活儿丢掉** —— 任务还是失败，只是失败得体面一点。

格式错误有一个很好的性质：**模型有能力自己改**，它只是需要看到"你错在哪 + 期望什么"。
所以正确的处理是**把错误回灌给模型重试一次**：

```
第 1 次：模型返回数组  → 解析失败
        ↓ 构造纠错提示（错误原因 + 期望结构）
第 2 次：模型看到自己的错误回复 + 纠错要求 → 返回合法 JSON  → 继续执行
        ↓ 若仍失败
        yield ErrorEvent（可枚举的失败，而不是崩溃）
```

### 2.2 落地形态：`BaseAgent.invoke_structured()`

新增在 `app/domain/services/agents/base.py`：

| 成员 | 作用 |
|---|---|
| `StructuredResult` | 结果容器。异步生成器没法优雅 return 值（要靠 `StopIteration.value`），传容器进去语义最清楚 |
| `_expected_schema(model_cls)` | 把 pydantic 的 JSON Schema 压成"字段名 + 类型 + 是否必填"几行 —— 完整 schema 动辄上千 token，而模型只需要这些就能改对 |
| `_parse_structured(raw, cls)` | 解析 + 校验，失败时返回**给 LLM 看的**错误说明（含出错原因 + 期望结构） |
| `invoke_structured(query, cls, holder, max_attempts=2)` | 完整流程：调用 → 解析 → 失败则纠错重试 → 仍失败 `yield ErrorEvent`；**中间事件（工具事件等）原样透出** |

**四个调用点统一改用它**：`create_plan` / `update_plan` / `execute_step` / `summarize`。

> 为什么放在 `BaseAgent`：这四个调用点的公共父类本来就是它。
> 四份重复的 `try/except` 必然会长成四种不同的行为（有的重试、有的不重试、有的标记状态不一样）。

### 2.3 为什么重试上限是 2

一次纠错重试已经能覆盖"手滑"；两次以上通常说明这个模型确实做不到这个格式，
再试只是烧 token。而 Step 3 的预算观察（observe）会把这部分成本记下来 ——
**先有数据，再决定要不要调这个上限**。

### 2.4 D9 的修法：把"假完成"变成错误事件

```python
# 修复前：空计划 → 直接置为 COMPLETED，一个字都不报
if not self.plan or len(self.plan.steps) == 0:
    self.status = FlowStatus.COMPLETED

# 修复后：空计划 = 任务根本没做，必须上报
if not self.plan or len(self.plan.steps) == 0:
    yield ErrorEvent(error="Agent未能生成任何可执行的计划步骤（Plan 为空），任务未执行")
    self.status = FlowStatus.COMPLETED
```

同时把 COMPLETED 分支对"空计划"的处理改成**不重复报错**（原来 D5 的修复会再报一次，
导致错误链里出现两条同样条目，按 `error_type` 归因时会被重复计数）。

---

## 三、改动清单

| 文件 | 改动 |
|---|---|
| `agents/base.py` | 新增 `StructuredResult` / `_expected_schema` / `_parse_structured` / `invoke_structured`；解析失败时记录**模型原始输出**（截断 500 字） |
| `prompts/system.py` | 新增 `STRUCTURED_OUTPUT_CORRECTION_PROMPT` |
| `agents/planner.py` | `create_plan` / `update_plan` 改用 `invoke_structured`；移除已无用的 `logger` |
| `agents/react.py` | `execute_step` / `summarize` 改用 `invoke_structured`；移除已无用的 `logger` |
| `flows/planner_react.py` | D9：空计划 → `ErrorEvent`；COMPLETED 分支不再重复报错 |

**lab 侧零改动** —— 这次全部是 SUT 自身的健壮性修复，
lab 只是提供了能发现它的观测能力（外部判定器 + SUT 自述交叉校验）。

---

## 四、顺带修掉的两个问题

### 4.1 失败的步骤被覆盖成"完成"

`react.execute_step` 原实现把 `step.status = COMPLETED` 放在循环**外面**无条件执行：

```python
elif isinstance(event, ErrorEvent):
    step.status = ExecutionStatus.FAILED   # 先标记失败
    yield StepEvent(step=step, status=StepEventStatus.FAILED)
...
step.status = ExecutionStatus.COMPLETED     # ← 又被覆盖成完成
```

后果：**步骤统计里的 `failed` 永远是 0**，失败只在 `step.error` 里看得到，
而轨迹上显示"完成"。现在只在解析成功时才置 COMPLETED。

> 这一条是重构时顺带发现的，属于"修复一件事时看见的相邻问题"。
> 单独列出来是因为它会让失败率指标失真 —— 而失败率正是这个项目要量化的东西。

### 4.2 一个容易误判的认知：`json_repair` 几乎不抛异常

`_parse_structured` 里有两个失败分支：`JSON 解析异常` 与 `ValidationError`。
实测发现 **`json_repair` 极其宽容** —— 传"这不是 JSON"它也返回一个字符串而不抛异常。
所以绝大多数格式错误实际落到 `ValidationError` 分支（报"你返回的是 str/list，期望一个对象"），
第一个分支只作防御。这一点已写进代码注释，免得后人误判某条分支"从没被走到 = 死代码"。

---

## 五、验证

### 5.1 确定性验证（单元测试，不花钱）

新增 `lab/tests/test_sut_robustness.py`，6 个用例：

| 用例 | 验证什么 |
|---|---|
| `test_malformed_structured_output_is_retried_and_recovers` | 模型返回数组 → 纠错重试 → **任务真的成功了**（不是"不崩但失败"）；且断言纠错提示里含"格式不符合约定"+ 期望字段 + 上一轮错误回复 |
| `test_correction_prompt_lists_required_fields` | 纠错提示的内容质量：含字段名/类型/必填；三种坏输入的报错都可读 |
| `test_persistent_malformed_output_yields_error_event_not_crash` | 连续两次坏格式 → `ErrorEvent`，且 `error_type != "sut_crash"`；只试 2 次 |
| `test_tool_events_are_still_forwarded_during_structured_call` | 重构没破坏中间事件透传（轨迹/UI 依赖它） |
| `test_empty_plan_is_reported_as_error_not_success` | **D9**：空计划 → `ok=False`（修复前是 `ok=True`） |
| `test_empty_plan_does_not_duplicate_error_events` | 错误链只有 1 条（避免归因重复计数） |

### 5.2 真模型冒烟（`sem_json_to_csv` + `sem_bug_fix` × 2 次）

`suite 7be9ae47`：**4/4 成功，零 `sut_crash`**，happy path 无回归（llm 调用数 15~27，与修复前同量级）。

**诚实声明**：这次冒烟**没有触发纠错重试路径** ——
原来那个崩溃是 1/5 概率的事件，4 次运行在统计上说明不了什么。
纠错机制的正确性由 5.1 的确定性测试保证；冒烟只证明了"重构没有破坏正常路径"。
要在真实模型上观察纠错路径，需要跑到它自然复现（属于概率事件），或者人为构造一个
必然产生坏格式的任务 —— 后者值得在 Step 5 做。

---

## 六、这次修复对指标的意义

| 指标 | 修复前 | 预期变化 |
|---|---|---|
| `sut_crash` 类失败 | 2/40（1 次导致产出失败） | → 0（转为"纠错后成功"或"可枚举的 ErrorEvent"） |
| SUT 虚报成功 | 1/40（D9 空计划） | → 0（空计划现在明确报错） |
| `steps.failed` 统计 | 恒为 0 | → 真实（4.1 的修复） |

**但要注意**：这些修复会让下次 baseline 的"成功率"与本次不可直接比较 ——
**SUT 版本变了**。这正是 Step 2 里 `prompt_digest`（提示词/工具版本标识）存在的意义：
真正做 A/B 对比时，必须同时记录 SUT 版本，否则数字变化无法归因。
