# Step 16：盲标复测 —— 把 `✅ 达标` 从 provisional 里**抬出一半**

> 承接 **Step 15 §七**：那里写着"下一步真正该做的是**换一份独立金标复测** ——
> 其它任何动作（继续调提示词、加采样、换模型）都在'不知道 v3 是不是只在拟合这份金标'的前提下进行。"
>
> 本篇是这件事的**执行记录与结果**。结论一句话：
> **盲标条件下的标签与"给结论+复核"的标签逐条相同** —— 这排除了"标签被 agent 结论锚定"，
> 但**没有**把它升级成"独立人类验证"（原因见 §五）。

---

## 一、这次盲标具体做了什么（可审计的部分）

三份文件 + 一条时间线，全部在 `lab/runs/`（该目录不进版本控制）：

| 时间 | 文件 | 内容 |
|---|---|---|
| 19:43:35 | `judge_gold_v4_human.csv` | 95 条，**"agent 给结论 + 用户逐批复核"**的标签（Step 15 §5.9） |
| 21:45:43 | `_blind_map_v4.csv` | 95 行映射：`blind_id, sample_id, level`（**level 留在映射里**） |
| 21:45:43 | `judge_gold_v4_blind.csv.bak` | **空标签模板**：`human_*` 全空（**0/95 已填**）、`level` 列被清空、id 换成 `blind_XXX` |
| 21:58:25 | `judge_gold_v4_blind.csv` | 同一 95 条，`human_*` **95/95 已填**（`level` 仍为空） |
| 21:58:26 | `judge_gold_v4_relabeled.csv` | 用 `_blind_map_v4.csv` 把 id / level 还原回原名 |

三个细节值得记下来，因为它们让"盲"这件事**部分可审计**：

1. **先出空模板、后填满** —— `.bak` 是 0/95，最终是 95/95。所以标签是**在模板之后产生的**，
   不是"先把答案抄进去再假装盲标"。
2. **`level` 列在 blind 表里是空的，而映射表里是有的** —— 说明填写过程**没有**沿用原始抽样档位
   （`positive` / `mild` / `wrong_solution` / `db_negative` 这个分类本身就是一条捷径）。
3. **还原是可验证的** —— `relabeled` 由 `_blind_map_v4.csv` 机械生成，映射表 95 行齐全。

> ⚠️ **一个必须写死的边界**：以上只证明"**文件层面**是先空后填、且没带 level"。
> 它**不能**证明"填写者当时确实看不到 agent 结论"（文件不记录人的视野）。
> 这一步是**流程承诺**，不是**文件事实**。见 §五.3。

---

## 二、结果：三种引出方式给出**逐条相同**的标签

### 2.1 逐列对账（human vs relabeled）

比较列：`task_key / group / suite_id / run_index / arm / human_task_completion /
human_instruction_following / human_output_quality`。

**95 行 × 8 列 = 760 个单元格，不一致 0 个。**

### 2.2 三种引出方式的 κ

判定用的是**同一份** `lab/runs/judge_judged_v4.json`（rubric **v3**，`temperature=0`）。
所以这里变动的**只有金标**，judge 侧一个字节都没动。

| 维度 | ① 规则推（§5.8） | ② 逐批复核（human） | **③ 盲标（relabeled）** |
|---|---|---|---|
| `task_completion` | 0.889 | 0.889 | **0.889** |
| `instruction_following` | 1.000 | 1.000 | **1.000** |
| `output_quality` | 0.869 | 0.869 | **0.869** |
| 是否允许启用 | ✅ | ✅ | **✅** |

（参与 κ 的条数也一模一样：TC 92 / IF 87 / OQ 93；`Unknown` 分别 3 / 8 / 2。）

### 2.3 ⚠️ 这次复测的是**金标**，不是 **judge 的运行稳定性**

两次算出的 κ 完全相同，**不能**读成"judge 稳定复现"。
这里 judge 的判定是**同一份文件**（没有重跑模型），变的只有金标。
judge 本身的运行稳定性是另一回事（`temperature=0` 条件下的重复调用一致性），
本步**没有**测它，也**不声称**测了它。

---

## 三、能说 / 不能说

| 能说 | 不能说 |
|---|---|
| "在**去锚定**的条件下重标一遍，95 条的标签**逐条复现**（0/760 不一致）" | "已由**独立人类**验证" |
| "原标签**不是**被 agent 的结论锚定出来的 —— 换一种引出方式得到同一批标签" | "换了**第二个标注者**，结论仍成立" |
| "`✅ 达标` 的**一半**provisional 成分被消掉了：口径与采样数量这层风险已排除" | "judge 已可信到可以进**确定性基线**"（它仍只是辅助信号） |
| "v3 在**这两个条件下**（给结论 / 不给结论）都达标" | "v3 在**其它金标**（别的标注者、别的任务分布）上也达标" |

---

## 四、它回答了 Step 15 §七 的什么问题

Step 15 §三.1 把风险点定在：

> "金标是'AI 给结论 + 你复核'…… 要让这个 `✅` 站得住，**唯一**的办法是让一位标注者
> 在不看 agent 结论的前提下重标一遍。"

**这一条现在做了，结果是复现。** 所以 Step 15 那个悬着的"下一步"可以结掉：
**当前 v3 的达标不能被解释为"只在这份被锚定的金标上成立"。**

### 但它**没有**回答的是

- 换**另一个标注者**（人的判法不同）会怎样 —— 这是 inter-rater，本次是**同一个人**（见 §五.1）；
- 换成**另一个模型**当 judge 会怎样；
- 在金标里换成**别的任务分布**（比如开放式任务）会怎样。

这些都不在本次证据范围内。

---

## 五、仍然存在的三条限制（请连同结论一起读）

### 5.1 它是 **intra-rater 重测**，不是 **inter-rater 一致性**

本次是**同一位标注者**在两种条件下标了两遍（19:43 一次、21:58 一次）。
它消除的是**"agent 结论的锚定效应"**，测出来的是这位标注者的**重测一致性**。
"人 vs judge" 的第二个未知量 —— **人与人之间的判法差异** —— 一步都没减小。

要减它只有一个办法：**换一位没参与过前面任何一轮的标注者**。

### 5.2 "去锚定" ≠ "双盲"：任务身份是可见的

`judge_gold_v4_blind.csv` 只藏了 **id 与 level**，而 `task_key`（`ci_float_settled` …）与
`artifact_dir`（真实工作区路径）**原样保留**。所以标注者知道"这是哪个任务、第几次运行"。

这不一定是缺陷（标注本来就要看产物），但**必须说清**：藏起来的是**别人的结论**，
不是**任务的答案**。写成"双盲"就是夸大。

### 5.3 填写过程**不能自证**

`judge_gold_v4_blind.csv.bak` 是空模板、最终是 95/95 —— 这证明"先空后填"。
但如果一次性把已有标签回填进去，得到的是**完全相同的文件**。
所以 §一 的"盲"依赖两条**流程承诺**：① 填写时看不到 agent 结论；② 看不到 19:43 那版标签。

> 这三条合起来是一句老话（本项目的自纠 #1/#8 已经踩过两次）：
> **"文件里有这个数" ≠ "这个数是这么来的"。**

---

## 六、复现（**$0**，不调用任何模型）

```bash
# 0. 前置：三份文件都在（lab/runs/ 不进版本控制，换机需重新生成）
#    judge_gold_v4_human.csv / judge_gold_v4_blind.csv / _blind_map_v4.csv

# 1. 逐列对账（应输出「不一致单元格数: 0」）
python - <<'EOF'
import csv
def load(p):
    with open(p, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))
h = load('lab/runs/judge_gold_v4_human.csv')
r = load('lab/runs/judge_gold_v4_relabeled.csv')
cols = ['task_key','group','suite_id','run_index','arm',
        'human_task_completion','human_instruction_following','human_output_quality']
print('不一致单元格数:', sum(1 for x, y in zip(h, r) for c in cols
                             if x.get(c,'').strip().lower() != y.get(c,'').strip().lower()))
EOF

# 2. 两份金标各自的 κ（同一份 judge 判定，只换金标）
python -m lab bench calibrate --gold lab/runs/judge_gold_v4_human.csv \
    --judged lab/runs/judge_judged_v4.json
python -m lab bench calibrate --gold lab/runs/judge_gold_v4_relabeled.csv \
    --judged lab/runs/judge_judged_v4.json
```

两次 `calibrate` 的输出表**逐格相同**。

---

## 七、结论

1. **盲标复测做完了，结果是复现**：95 条 / 760 列 / **0 处不一致**，
   三种引出方式的 κ 完全一致（**0.889 / 1.000 / 0.869**）。
2. 因此 `✅ 达标` 的**一半** provisional 成分（"标签被 agent 结论锚定"）**已排除**；
3. 剩下的一半**没有**排除：这仍是**同一位标注者**的重测，
   要升级为"独立人类验证"必须**换人**（§5.1）；
4. judge 的地位**不变**：它是**辅助信号** ——
   **不**进确定性基线（28 个任务的纯函数判定器才是基线）。

> 一句话记法：**从"provisional（可能是拟合）"变成"provisional（同一个人的判法很稳）"。**
> 变好了一格，但没到终点。
