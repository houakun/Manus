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
