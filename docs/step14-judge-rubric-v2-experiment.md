# Step 14：rubric `v1 → v2` 的**对照实验**（把维度边界写进提示词）

> 承接 Step 13：三轮校准里 `instruction_following` 的 κ 一直是 **0.208 ~ 0.244**，
> 而分歧**全部**是同一个模式 —— "人判 true、judge 判 false"，约 30 例。
> `docs/judge-labeling-guide.md` 把它定位成**边界**问题：judge 把"内容错"读成了"约束没遵守"。
>
> 本篇是**针对该维度的一次对照实验**：把那套边界**写进喂给模型的文本**（`v1 → v2`），
> 看它到底**判得更对**，还是只是**更合人意**。

---

## 一、实验设计（关键是"两边都比"）

| | A 臂（对照） | B 臂（处理） |
|---|---|---|
| rubric | **v1**：边界只写在代码注释里 | **v2**：边界写进 `RUBRIC` 的**描述文本** |
| 触发方式 | `LAB_JUDGE_RUBRIC=v1`（默认曾是 v1） | `LAB_JUDGE_RUBRIC=v2` |
| 样本 | **同一批 111 条**（`lab/runs/judge_gold_v3.csv`） | 同 |
| 金标 | **同一份**（`lab/runs/judge_gold_v3_human.csv`，用户逐条复核过） | 同 |
| 代码 | **同一份** | 同 |

`LAB_JUDGE_RUBRIC` 用**环境变量**而不是改代码，就是为了让两个臂跑在同一份代码上 ——
否则"差异来自提示词"这句话不成立。

**v2 改了什么**（只动这两个维度的描述，`task_completion` 一个字没改）：

| 维度 | v1 | v2 |
|---|---|---|
| `instruction_following` | "格式/路径/命名等**显式约束**是否遵守（例如要求的输出文件、字段名）" | 明确列出"**只看形状**：文件在不在、路径/命名/结构/表头/有没有碰冻结文件"，并写明"**不要判断内容对不对**：数值/文字/事实错了属于 `output_quality`" |
| `output_quality` | "产物本身的质量（内容是否正确、是否明显残缺或占位）" | 明确"**只判内容，不判路径/命名**（那些属于 `instruction_following`）" |

⚠️ 这条边界是**先写下来的**（`docs/judge-labeling-guide.md`，做实验之前），不是看了结果才编的规则。

---

## 二、结果

| 维度 | **A（v1）** | **B（v2）** | 变化 |
|---|---|---|---|
| `task_completion` | 0.977 | 0.977 | 0 |
| `instruction_following` | **0.208** | **0.556** | **+0.348** |
| `output_quality` | **0.978** | **0.842** | **−0.136** |

关键混淆矩阵行（"人✓judge✗" = judge 误判为不合格）：

| | A（v1） | B（v2） |
|---|---|---|
| `instruction_following` 人✓judge✗ | **32** | **11** |
| `output_quality` 人✓judge✗ | 1 | **4** |
| `output_quality` 人✗judge✓ | 0 | **3** |
| **总分歧（粗算）** | **33** | **15** |

成本：B 臂 **333 次调用 / 696,447 tokens / $0.2553 等价**（预估 $0.22）。
A 臂 $0（沿用既有判定：93 条来自 `judge_judged_v2.json`，18 条来自 `judge_judged_v3_mild.json`）。

---

## 三、结论：这是**转移**，不是白拿

1. **它确实修好了我们诊断出的那个病**：judge 不再把"内容错"算成"格式错" ——
   `instruction_following` 的误判从 **32 → 11**。
2. **但它把一部分错误推到了 `output_quality`**（1+0 → 4+3），那一维从 0.978 掉到 0.842。
   也就是说：**边界被移动了，没有被消除干净** —— 一部分"内容 vs 形状"的判断本来就落在灰区。
3. **总分歧降了（33 → 15），但它仍然不达标**（`instruction_following` 0.556 < 0.6）。

### 为什么还是采纳了 v2

| 理由 | 说明 |
|---|---|
| 改动性质是**消除规格歧义** | v1 的描述确实没定义这条边界；v2 补上了 |
| **先定规则、后做实验** | 规则写在 `judge-labeling-guide.md`（实验之前），不是照着结果编的 |
| **两边都比了** | 没有只看 `instruction_following` 涨没涨 —— `output_quality` 的 0.136 代价被明确记下来 |
| **它没跨过门槛** | 如果动机是"凑指标"，最有说服力的反证就是**它凑不过去** |
| 总分歧更少 | 33 → 15 |

### 反过来说：如果你的取舍是"宁可 IF 低也不许 OQ 掉"，就不该采纳

这不是一句客套 —— 数据支持两边：B 在 IF 上明显更好、在 OQ 上明显更差。**采纳它是一个价值判断，不是数据结论。**

---

## 四、复现命令

```bash
# A 臂（v1）—— 若默认已是 v2，就用环境变量切回去
LAB_JUDGE_RUBRIC=v1 python -m lab bench judge \
  --gold lab/runs/judge_gold_v3_human.csv --out lab/runs/judge_judged_v3.json --yes
python -m lab bench calibrate --gold lab/runs/judge_gold_v3_human.csv \
  --judged lab/runs/judge_judged_v3.json

# B 臂（v2）
LAB_JUDGE_RUBRIC=v2 python -m lab bench judge \
  --gold lab/runs/judge_gold_v3_human.csv --out lab/runs/judge_judged_v3_v2rubric.json --yes
python -m lab bench calibrate --gold lab/runs/judge_gold_v3_human.csv \
  --judged lab/runs/judge_judged_v3_v2rubric.json
```

---

## 五、遗留：v2 仍不达标，下一步的选项

`instruction_following` 0.556 —— 差 0.044。可选：

| 选项 | 说明 | 成本 |
|---|---|---|
| **加样本** | 111 条里参与 κ 的只有 83~96 条；增到 200+ 能收窄区间，但**不改变点估计** | 中（要重标） |
| **让灰区显式化** | 把"`cp` 代替 `mv`"这类**人与人也会分歧**的样本单独抽出来（见 `judge-labeling-guide.md` §一），先由多人对齐口径 | 小 |
| **换更强的模型跑 judge** | 当前 `deepseek-v4.1-flash`；换更强的模型看 IF 是否上去 | 中（要重跑） |
| **接受"不启用"** | 门禁的目的就是不达标别用 —— 这是**当前正确的状态**，不是失败 | $0 |

⚠️ 仍然不变：**不许为了让 κ 达标去改 rubric**。上面任何一条都要**两边都比**（改前/改后 × 各维度），
并且改提示词必须升版本号、旧金标不得混算。
