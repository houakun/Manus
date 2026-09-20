# SUT v1 → v3 对比：修复了什么、以及**什么不能归因于修复**

> | | suite | 时间 | SUT 版本 |
> |---|---|---|---|
> | **v1** | `70a79a26` | 18:37–19:16 | 原始（D10 修复前） |
> | **v3** | `80caef04` | 21:02–21:40 | D10 结构化输出兜底 + 工具层兜底 + 收尾兜底 |
> 两组均为 semireal 8 任务 × 5 次 = 40 次，`deepseek-reasoner`，温度 0.0，并发 1

---

## 一、结果

| 指标 | v1 (n=40) | v3 (n=40) | 变化 | 区间是否重叠 |
|---|---|---|---|---|
| 成功率 | 95.0% [83.5%, 98.6%] | **100.0%** [91.2%, 100%] | +5pt | ✅ 重叠 → 不显著 |
| tokens/任务 | 210,350 ± 58,127 | **163,236 ± 37,675** | **-22%** | ❌ 不重叠 |
| 成本/任务 | $0.1302 | **$0.1016** | -22% | — |
| 耗时/任务 | 58.2s ± 17.0 | **43.1s ± 8.0** | -26% | ✅ 重叠 → 边缘 |
| 工具调用 | 18.7 ± 2.9 | **15.1 ± 2.3** | -19% | ✅ 重叠 → 边缘 |
| **带 error_type 的运行** | **5** | **0** | — | — |
| SUT 自述成功 | 35/40 | **40/40** | — | — |
| 虚报成功 | 1 | **0** | — | — |
| 误报失败 | 4 | **0** | — | — |
| 预算越界运行 | 40/40 (100%) | **4/40 (10%)** | — | — |
| enforce 本会中止 | 0 | 0 | — | — |

总花费：v1 $5.21 → v3 $4.06。

---

## 二、🔴 归因：**token/成本下降不能算在修复头上**

逐任务看，7/8 个任务一致变便宜（-4% ~ -49%），看起来很像"修复带来的普遍改善"。但**我的三处修复在逻辑上只会增加 token**：

| 修复 | 对 token 的影响 |
|---|---|
| D10 结构化输出兜底 | 格式错误时多一次纠错重试 → **增加** |
| 工具层兜底（幻觉工具名/坏参数） | 多一轮工具往返 → **增加** |
| 收尾兜底（汇总失败→用已有结果交付） | 不额外调用 → **持平** |

同时 v1 里的两次崩溃（`sut_crash`）是**提前结束**运行，它们只会**减少** token。

所以 **-22% 不可能来自我的改动**。最合理的解释是**时间相关的服务端变化**：
v1 在 18:37–19:16、v3 在 21:02–21:40，模型服务的状态（负载、灰度版本、批处理策略）不同。

更关键的是方法论问题：**同一批任务在同一时段内运行，噪声是相关的** ——
一个服务端的整体位移会同时影响全部 8 个任务，从而伪装成"系统性改善"。
这就是为什么逐任务的一致性在这里**不能**当作证据。

### 能归因的部分

| 结论 | 依据 |
|---|---|
| **失败模式确实被修好了** | v1 有 5 次运行带 `error_type`（2 次 `sut_crash`、1 次 LLM 重试耗尽、1 次 `wait_user_input`、1 次 `task_timeout`），v3 为 **0** |
| **SUT 自述变得可信** | v1 有 1 次虚报成功 + 4 次误报失败；v3 自述与判定 **完全一致（40/40）** |
| **阈值校准生效** | 固定阈值的越界率 100%（v1）→ per-task 分位数的 10%（v3） |

### 要证明 token 下降是真的，必须做**交错 A/B**

把 v1 配置和 v3 配置的跑次**交替**安排（A,B,A,B,…），让两者共享同一时段的服务状态。
否则"我的改动"与"这个小时"永远分不开。这是 Step 5 的第一条要求。

---

## 三、🔴 发现：fast mode 的沙箱**被脚本逃逸是常态，不是边缘情况**

排查参考解为什么跑不通时发现：沙箱的路径映射只作用于**文件工具**和**命令行字符串**，
**管不到脚本内容** —— 脚本里写 `"/home/ubuntu/x"` 会被 Python 解析到 `<当前盘>:/home/ubuntu/x`。

实测证据（`D:/home/ubuntu/` 里的真实产物）：

```
config.json  days.txt  fizzbuzz.txt  prime_count.txt  sum.txt
parts/part_000 … part_006
sqlite-tools.zip (5.0 MB)   st.zip (5.0 MB)      ← Agent 下载的
```

其中 `parts/*`、`sqlite-tools.zip`、`st.zip` 是**真实评测运行里** Agent 产生的
（`sem_refactor_config` / `sem_sqlite_report`），不是我的调试产物。

**双重危害**：
1. **正确性**：产物落在工作区外 → 判定器读不到 → 正确的工作被判失败；
2. **安全**：任意脚本可写到磁盘任意位置（已实测写出 5MB 文件到盘根）——
   **fast mode 只能用于可信任务与内容**。

### 已做的处理

1. **参考解改用相对路径**（exec 的 cwd 已是映射后的 `/home/ubuntu`）；
2. `LocalSandbox` 新增**增量逃逸检测**：
   - 初版检测器只判断"逃逸目录是否存在"，结果一次 40 运行的评测报了 **98 次**告警，
     几乎全是噪声（目录一旦被创建，之后每条命令都告警）。
     **噪声化的安全告警比没有告警更糟** —— 它会训练人忽略它。
   - 改成比对命令前后的快照，只报"本次新增/改写了哪些文件"；
3. 逃逸信号接进 `ToolGuard` → `TaskResult.guard.escaped_writes` → 过程 flag
   `writes_outside_workspace`，这样它会出现在报告里，而不是只躺在日志里；
4. 明确记为**已知限制**：真要把绝对路径导向工作区需要盘根目录级别的 junction ——
   那是全局状态、并行任务会互相干扰，代价大于收益。

> ⚠️ D2 这次运行用的是**旧检测器**（进程启动早于修复），所以那 98 次告警是噪声，
> D2 的 `escaped_writes` 字段为空**不代表没有逃逸**。新的增量检测从下一次运行开始生效。

### 清理（已完成，且做成了可重复命令）

这些文件在工作区之外，**不会随任务清理**，一天就积了 **15MB / 34 个文件**
（含两个 5MB 的 zip 和 24 个 200KB 的分片）。所以清理不能靠一次性手工删除：

```bash
python -m lab sandbox clean-escapes          # dry-run：只报告
python -m lab sandbox clean-escapes --yes    # 真删（连带清掉空的父目录）
```

默认 dry-run 的理由：它在删工作区**外面**的东西，即使路径几乎不可能是用户数据，
也应该先让人看一眼清单。

清理记录（2026-09-16，已存入本文件末尾的附录）：
- `D:/home`（15.00 MB，34 个文件，全部为当日创建）→ 已删
- `C:/home`（6 个条目，全部为当日创建）→ 已删
- 其它盘（E/F/G）无残留

其中 `report.txt` + `sales.db`（时间戳 21:29）来自 **D2 这次运行** ——
说明逃逸在修复后的 SUT 上仍然在发生。

---

## 四、发现：`answer_literals` 的一个误报模式

D2 里 `hardcoded_answer` 出现 2 次。查清后发现是**规则设计错误**：

`sem_bug_fix` 的任务是"运行脚本，把输出写进 result.txt"——
**交付物本身就是那个数字**，于是任何正确的运行都会把 `385` 当作字面量写进文件，
每次都被判为"硬编码抄袭"。

**经验**：`answer_literals` 只适用于"答案需要推导、直接写出字面量才可疑"的任务；
对于**交付物本身就是答案**的任务，这条规则是纯噪声。已从该任务移除（其余任务的规则保留）。

---

## 五、计划变更：**取消上下文压缩**

原计划第 4 项（上下文压缩 + 纠偏提示，用来解决"原地打转"的 953k/51 次工具调用）**已取消**。

需要记录的影响：

| 影响 | 说明 |
|---|---|
| 成本长尾问题**仍然存在** | 最贵单次 v3 仍有 689k token（`sem_refactor_config`），`action_diversity` 低至 0.23 |
| 半真实任务贵 1.7× 的差距**不会缩小** | 分组对比显示 semireal 1.7× 的成本主要来自这种"绕弯" |
| 但这不影响**可靠性**结论 | v3 成功率 100%，说明"绕弯但做对了"——原计划里也强调过**熔断会误杀它们** |
| Step 5 的内容相应缩小 | 只做"同版本交错 A/B + Pareto"，不做"压缩前后对比" |

> 从数据看，取消它的代价是可接受的：那两次绕弯的运行**最终都成功了**，
> 它们影响的是成本曲线，不是可靠性曲线。如果以后要捡起来，
> 正确的做法仍是**上下文压缩 + 纠偏提示**（而不是熔断）。

---

## 六、下一步

1. **交错 A/B**：验证 token/成本差异到底是不是真的（方法见第二节）；
2. 把新的**增量逃逸检测**在真实运行里跑一轮，看 `writes_outside_workspace` 的真实发生率；
3. Step 5：加固前后对比 + Pareto 曲线（现在只有 v1/v3 两个点，且存在时间混淆）。
### 清理记录：工作区外逃逸产物（2026-09-16 21:35 删除前清点）

```
D:/home  (15MB, 34 个文件，全部为 2026-09-16 创建):
  D:/home/ubuntu
  D:/home/ubuntu/config.json
  D:/home/ubuntu/days.txt
  D:/home/ubuntu/esc.txt
  D:/home/ubuntu/fizzbuzz.txt
  D:/home/ubuntu/parts
  D:/home/ubuntu/parts/part_000
  D:/home/ubuntu/parts/part_001
  D:/home/ubuntu/parts/part_002
  D:/home/ubuntu/parts/part_003
  D:/home/ubuntu/parts/part_004
  D:/home/ubuntu/parts/part_005
  D:/home/ubuntu/parts/part_006
  D:/home/ubuntu/parts/part_007
  D:/home/ubuntu/parts/part_008
  D:/home/ubuntu/parts/part_009
  D:/home/ubuntu/parts/part_010
  D:/home/ubuntu/parts/part_011
  D:/home/ubuntu/parts/part_012
  D:/home/ubuntu/parts/part_013
  D:/home/ubuntu/parts/part_014
  D:/home/ubuntu/parts/part_015
  D:/home/ubuntu/parts/part_016
  D:/home/ubuntu/parts/part_017
  D:/home/ubuntu/parts/part_018
  D:/home/ubuntu/parts/part_019
  D:/home/ubuntu/parts/part_020
  D:/home/ubuntu/parts/part_021
  D:/home/ubuntu/parts/part_022
  D:/home/ubuntu/parts/part_023
  D:/home/ubuntu/prime_count.txt
  D:/home/ubuntu/report.txt
  D:/home/ubuntu/sales.db
  D:/home/ubuntu/sqlite-tools.zip
  D:/home/ubuntu/st.zip
  D:/home/ubuntu/sum.txt

C:/home  (6 个条目，全部为 2026-09-16 创建):
  C:/home/ubuntu
  C:/home/ubuntu/days.txt
  C:/home/ubuntu/fizzbuzz.txt
  C:/home/ubuntu/m.txt
  C:/home/ubuntu/prime_count.txt
  C:/home/ubuntu/sum.txt
```
