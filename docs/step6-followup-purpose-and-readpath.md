# Step 6 后续：purpose 分类 + 读路径检测面

> 承接 `docs/ab-guard-experiment.md` §5 的三条硬条件：
> ① 先修污染源（不稳定任务）② guard 的检测面必须覆盖 **Agent 无法自证** 的故障。
> 本文件记录这两项落地，以及一个把负结果变成**干净结论**的发现。

---

## 一、`sem_markdown_toc` 的诊断：**能力边界**，不是规格歧义

它在本轮 2×2 里 4/9 失败、三种错法（留下 `##` 标记 / 漏 H1 / 缩进错）。
查它的工具序列后发现关键事实：

| run | 判定 | 工具序列 |
|---|---|---|
| 0 | ❌ | read_file → write_file → **read_file** → notify → **read_file** → write_file → **read_file** → shell → **read_file** |
| 1 | ❌ | read_file → notify → write_file → **read_file** → write_file → **read_file** → write_file → **read_file** → shell → **read_file** |
| 2 | ❌ | read_file → write_file → **read_file** → notify → shell×2 → write_file → **read_file** → write_file → shell×3 |

**Agent 反复 `read_file` 回验了 4~5 次，仍然交付了错的内容。**

所以它既不是"规格歧义"，也不是"没验证" —— 是**能力边界**：
它**能读回自己的输出，但判断不了它是否符合格式要求**。
（验证 ≠ 能判断对错。这反过来是"独立判定器 + guard 检测层"存在必要性的最好论据。）

### 处置：加 `purpose` 分类，而不是"修任务"

`BenchTask.purpose`：`capability`（探测能力边界，允许不稳定）/ `regression`（回归门禁，要求稳定）。
`sem_markdown_toc` 标为 `capability`。

报告现在按 purpose 分开看口径，并且：

- `regression` 口径才是**回归门禁**指标；
- capability 任务的波动是**能力边界的发现**，不是回归噪声；
- Wilson 下界 > 95% 时打 **🟡 已饱和**（100% 的 eval 只能追回归，给不出改进信号）。

### 🔴 加上这个分类后，负结果变成了干净结论

| 口径 | A 无故障 | C 故障×无加固 | D 故障×加固 |
|---|---|---|---|
| 运行级成功率 | 100% | 95.8% | 87.5% |
| **regression pass^3** | **100%** | **100%** | **100%** |
| capability（`sem_markdown_toc`） | 3/3 | 2/3 | **0/3** |

**那个"加固变差 8.3pt"完全来自一个能力边界任务的 3 次运行。**
在稳定任务上，三格的成功率**完全一致**。

由此得到三条比原始数字更可用的结论：

1. **加固没有伤到任何稳定任务**（原先担心 enforce 误报——没有发生）；
2. 故障也没伤到稳定任务（Agent 自愈）→ **guard 的恢复能力在这个任务集上没有用武之地**；
3. **`enforce` 在能力边界任务上把 3/3 变成 0/3** —— 它让 Agent 反复重写，
   而每次重写都是新的出错机会。**所以 enforce 只应在"恢复路径可靠"的任务上开。**

---

## 二、读路径检测面：唯一"Agent 无法自证"的故障类

原来的读校验只查 `filepath`/`content` 两个字段存在 ——
**只要拿回一个非空字符串就算通过**。于是读路径上的静默损坏无人发现。

新增两类，**强度刻意不同**：

| 校验 | 强度 | 为什么 |
|---|---|---|
| **JSON 结构合法性** | **可 enforce** | 结构能不能解析是**与来源无关的客观不变量**，可以高置信度自动判定 |
| **读回内容 vs 最近写入** | **只作审计** | ⚠️ 文件可能被 `shell_execute` 改过（sed/重定向），不一致是**正常现象**；拿它 enforce 会造成假失败 |

> 这个区分是本次最重要的设计取舍：**把"确定错"和"可疑"分开**。
> 把正确的工作判错，比漏掉一次损坏更糟。

### 误报防范（同一个教训的第二次应用）

JSON 结构校验有一个必然的假告警源：`read_file` 默认只读 10000 字符，
一个 50KB 的 JSON 读回来就是半截 —— 那是**工具的正常行为**，不是故障。
所以两种情况直接跳过校验：

1. 工具自己标了 `truncated`（本地沙箱会标）；
2. 内容长度正好等于请求的 `max_length`（强提示是被上限截的）。

> 这和之前那 98 次噪声告警是同一个教训：**假告警会让真告警失效**。

---

## 三、验证

**170 passed**（新增 4 个用例）：

| 用例 | 验证什么 |
|---|---|
| `test_content_check_catches_broken_json_on_read_path` | 截断的 JSON 被抓；合法 JSON / 普通文本不误报 |
| `test_content_check_skips_legitimately_truncated_content` | 触达 `max_length` 或标了 `truncated` → 跳过（防假告警） |
| `test_read_consistency_is_audit_only_never_enforced` | 读写不一致 → 记审计信号，但**即使 enforce 开着结果仍成功** |
| `test_read_consistency_passes_when_content_matches` | 内容一致时不误报 |

---

## 四、下一步：现在才具备做"加固曲线"的条件

`docs/ab-guard-experiment.md` §5 的三条硬条件，现在补齐了两条：

| 条件 | 状态 |
|---|---|
| 故障必须 Agent 自身无法发现 | ✅ **读路径 JSON 截断**（新增检测面正好对应） |
| 任务必须稳定（不淹没小效应） | ✅ `purpose` 分类可把 capability 任务排除 |
| guard 提供 Agent 没有的恢复手段 | ⚠️ 部分：JSON 结构校验给了 Agent 一个它自己没有的判据 |

**建议的下一次实验**（比上次便宜）：

| 格 | 故障 | guard | 任务 |
|---|---|---|---|
| C′ | `truncated_result` on `read_file` @0.5 | `none` | 只用 **regression** 任务（7 个） |
| D′ | 同上 | `retry,postcondition,enforce` | 同上 |

预测：Agent 读到半截 JSON 会直接拿去做转换 → 产物错 → 失败；
而 enforce 会告知"内容无法解析"→ Agent 重读/重算 → 成功率差异。
每格 7 任务 × 3 次 = 21 次，按故障格 $0.43/次 ≈ **$18 两格**（或 `--runs 2` ≈ $12）。

> 但要注意：**这次的负结果本身已经是一个可交付的结论**
> （"对自验证型 Agent，写路径的后置校验冗余；加固在稳定任务上净收益为 0、净成本 +31%"）。
> 是否还要花钱追那条曲线，取决于它值不值另外 $12~18。

---

## 五、全量标注：22 个任务逐个定性（2026-09-18）

> 承 §1：`purpose` 当时只标了 3 个任务（`sem_markdown_toc` + 两个新 `ci`），
> 其余 19 个落在 `task.py` 的默认值 `regression` 上。
> **默认值不是结论** —— 它表示"还没人看过这个任务的稳定性"，
> 而在报告里它和"已确认稳定"长得一模一样。本节把 22 个逐个定性，
> 依据全部来自 `lab/runs/traces.db` 现场重算（$0），不是拍脑袋。

### 5.1 判定规则（先定规则，再看数据）

| 标签 | 判据 |
|---|---|
| `regression` | 干净运行（`faults_injected=0`）**无失败**，或失败已被证实是**任务集/装置缺陷**且已修 |
| `capability` | ① 干净运行里出现过**无法归因给任务集/装置**的失败；或 ② 新任务类、方差未知 |

**为什么不能直接按成功率排序**：下面 6 行里有两行的原始比率是 50%，而它们其实是稳定的。

### 5.2 六个"非满分"任务逐个归因（这一步决定标签，不是看比率）

| 任务 | 干净 n/ok | 那一次/那几次失败 | 归因 | 标签 |
|---|---|---|---|---|
| `syn_string_reverse` | 5/10 | 5 次全在 suite `4cc51bc2`（09-16 20:23，**修正前**） | 任务集期望值写错（`TNEGA SUNUM`→`SUNAM`），已修；修正后 5/5 | `regression` |
| `syn_word_freq` | 5/10 | 同上，5 次全在修正前 | 任务集期望值写错（词频 Top3），已修；修正后 5/5 | `regression` |
| `syn_log_error_count` | 11/12 | 09-16 14:05（DB 里最早一次）：产物不存在、`error_type` 空、无 flag | SUT 三处兜底修复**之前**的装置问题 | `regression` |
| `sem_json_to_csv` | 26/27 | 09-16 19:16：`error_type=sut_crash`（ValidationError） | 已修的 SUT 缺陷 D10 | `regression` |
| `syn_date_diff` | 14/15 | 09-18 12:49：**期望 76.0，实际 -76.0**，且同 suite 另一臂是对的 | 🔴 **真实符号错误** —— 题目明写"用后一个日期减去前一个日期"，不是规格歧义；同次还响了 `too_many_tool_calls:13>12` | **`capability`** |
| `sem_sqlite_report` | 31/32 | 09-18 13:55：`writes_outside_workspace:2`，`sales.db` 不在工作区 | 🔴 **未修的装置缺陷**（产物逃逸，见 `noise-floor-measured.md` §8），**不可归因于 SUT** | **`capability`**（⚠️ 逃逸修好后应改回 `regression`） |

另外 `sem_markdown_toc`（16/22）沿用 §1 的"能力边界"判定；两个 `ci` 任务按设计保持 `capability`（方差未知，已有测试钉住）。

### 5.3 结果

```
标签分布：regression 17 / capability 5
  ci         capability 2
  semireal   capability 2   regression 6
  synthetic  capability 1   regression 11
```

两个 50% 的任务**故意**标成 `regression`：它们的失败是任务集的错，
把任务集的错记成"Agent 不稳定"会让能力描述变假 —— 这也是 `validate` 的同义反复检测想防的同一类错误。

### 5.4 连带影响（改标签就是改口径，必须写下来）

1. **`noise-floor` 的默认口径从 20 个任务变成 17 个**（synthetic 12→11、semireal 8→6、ci 2→0）。
   已测出的地板数字（±23.6% 等）是**旧口径**下的，引用时要一起说明。
2. **`bench run` 不受影响**：它没有 `--purpose` 参数，照跑全部任务。
   purpose 影响的是**分析口径**（`report.py` 按 purpose 分开）与 `noise-floor` 的取样。
3. **`--group ci` + 默认 `--purpose regression` = 空集**（ci 全是 capability）。
   原来只报"没有任务可测"，会把人赶去查任务集；已改成说出真正原因：

   ```
   [lab] 没有任务可测：--group ci 下有 2 个任务，但 --purpose=regression 一个都不剩（capability=2）。
         改用 `--purpose all`（全部）或 `--purpose capability`（只测能力边界任务）。
   ```

   （用例：`test_cli_noise_floor_empty_purpose_names_the_reason`，在任何花钱/联网之前返回。）

### 5.5 已知弱点：标签会腐烂

标签是**某一时刻数据的快照**，而任务集会变（新增 `ci` 组就是例子）。
现在没有"标签 vs 实测"的对账机制 —— 一个 `regression` 任务如果后来变得不稳定，
没人会收到通知。**下一步**：加一个数据驱动的对账（读 `traces.db`，对每个 `regression`
任务算干净成功率与同配置翻转次数，与标签不符就告警）。在没有它之前，
本节 5.1 的规则要靠人重跑一次这条查询。
