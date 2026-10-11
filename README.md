# MoocManus —— 一个被**严格测量**的 Agent 系统

这个仓库有两层，而**主角是第二层**：

| 层 | 位置 | 角色 |
|---|---|---|
| 通用 Agent（仿 Manus：A2A + MCP + 沙箱） | `api/` `ui/` `sandbox/` `nginx/` | **被测对象（SUT）** |
| Agent 可靠性评测实验台 | `lab/` | **本项目的主角**：测量仪器 + 控制手段 |

---

## 主角：`lab/`（Agent 可靠性评测实验台）

一套独立的、不依赖任何评测框架的 **Agent 评测与加固工具**：
28 个确定性任务集（含 CI 失败归因）、span 级轨迹、10 类故障注入、
预算/熔断/重试/后置校验中间件，以及统计（Wilson / bootstrap / `pass^k`）与对照实验开关。

它把 `api/` 里的 Agent 当作**被测对象**，回答四个问题：

| 问题 | 用什么回答 |
|---|---|
| 它做得对吗？ | 28 个确定性任务 + 纯函数判定器（**不看 Agent 自述**） |
| 它每次都能做对吗？ | `pass^k`（任务级 k 次全对）+ Wilson / bootstrap 区间 |
| 钱和时间花在哪了？ | span 级轨迹 + per-tool/per-step 成本归因（与库对账到 0.00%） |
| 坏了怎么修？ | 10 类故障注入 + 加固中间件 + 对照实验开关 |

**三条 $0 命令**（不需要 API Key）：

```bash
python -m lab bench validate        # 28/28 任务自检（判定器健全性 + fixture 真实性 + 歧义 lint）
python -m lab bench purpose-audit   # 标签 vs 实测对账：标签与数据不符就告警
python -m lab sandbox env           # Agent 实际会看到的环境指纹
```

另外三项让"每个 delta 都可信"的基础设施：
- **LLM 录制回放**：真跑 24.5s / $0.057 → 离线复现 **0.4s / $0**（不需要 API Key）；
- **交错 A/B**：两条臂在同一个 suite 内轮转，分离"时间相关的服务端漂移"；
- **噪声地板**：测出"同一配置跑两次能差多少"，回归门禁的阈值由此而来。

> 详见 **[lab/README.md](./lab/README.md)**；方法论与自纠记录在 `docs/`（含"我错在哪"10 条）。
> 英文入口：[README.en.md](./README.en.md)。

---

## 已实测的数字（每条都带限制 —— 请连同限制一起读）

| 能力 | 数字 | 限制 / 口径 |
|---|---|---|
| 任务集 + 判定器 | **28 个**确定性任务（12 synthetic / 8 semireal / 8 ci 归因），`bench validate` **28/28** | 判定器是纯函数、**不看 Agent 自述**；`ci` 组是 `capability`（方差未知，不进回归门禁） |
| `pass^k`（任务级 k 次全对） | semireal v1 **6/8 = 75%** → v3 **8/8 = 100%** | 区间 **仍然重叠**；样本量是**任务数**，加重复次数不会缩窄区间 |
| 噪声地板（同配置跑两次能差多少） | `deepseek-v4.1-flash`：成本 **±15.2%**（**18 个 regression 任务** × **6** 次 = 216 次，suite `01417afc`）；同一模型在 7 个任务上测是 ±46.8% | **任何小于当前地板的 delta 都不能当结论**。⚠️ 地板是**任务数**的函数，跳任务集不可直接比（`bench gate` 会当场报口径不一致）。**加次数几乎没用**（3→6 次只降 0.95pt）。真正的收获是它把成功率地板压到 ±5.56pt 后**暴露了失败的真实机制**：216 次里 **16 次（7.4%）planner 输出格式不合规 → 一个工具都没调用 → 把计划文本当答案 → 且自述成功**（判定器抓住了全部 16 次，假阳性率 94%）。两臂 6.48pt 的差统计上**不显著**（Fisher p=0.064），不是装置偏差。见 `docs/step17-resolution-budget.md` |
| LLM 录制回放 | 真跑 24.5s / $0.057 → 离线 **0.4s / $0**（18 次调用全命中） | SUT 提示词含随机 UUID → 需 `--replay-ignore-volatile`；未命中会**显式失败**（不假装成功） |
| 加固 2×2（写路径静默故障） | **负结果**：两臂都 100%，成本 +31% 方向不可信 | 机制：Agent 写完会**读回自证** → 写路径的后置校验天然冗余 |
| 读路径 2×2（`truncated_result`） | **负结果**：两臂 21/21 = 100%；注入次数与 `read_file` 次数一一对应（inj=1→2、2→3、3→4） | 只测了 `truncated_result@read_file(rate=0.5)` 与 7 个 regression 任务 |
| `silent_wrong_result` 2×2（T7） | **负结果**：两臂各 21/21 = 100%；加固臂有 6 条内容级告警 | 机制：内容错在**重读时自相矛盾** → 于是下一步测"让损坏跨读取稳定" |
| **稳定损坏** 2×2（A1，`silent_wrong_result@read_file(rate=0.5,stable)`） | **负结果**：两次运行合计 63 次；**44 次"故障真的生效"的运行里 43 次通过**（唯一失败是 `task_timeout`）；与 T7（非 stable）一致 | 机制比 T7 更根本：Agent 有**独立执行通道** —— 44 次有注入的运行里 `shell_execute` **386** 次 vs `read_file` **159** 次（42/44 次运行 shell≥read），而故障只污染读通道。实际花费 **$2.23**（预估 $8.68）；两个 suite 都因工具超时而不完整（31/42、32/42），另有一次遇 provider 403 |
| 装置保真修复 | 脚本逃逸 **0/42**（修复前会真实导致一次失败）；两种启动方式的工具面一致 | `subst` 不可用时会退化并**告警**；文件 CRLF 仍有"同一条复合命令内"的观察窗口 |
| 回归门禁 | `bench gate` 自动读噪声地板；**方向不可信就不判失败** | 阈值必须 ≥ 地板，否则"频繁变红的门禁等于没有门禁" |
| LLM-judge 校准（rubric **v3**） | κ = `task_completion` **0.889** / `instruction_following` **1.000** / `output_quality` **0.869**（**95 条**金标 × 2 轮引出，门禁 ✅） | ⚠️ 只算**辅助信号**，**不进**确定性基线。两轮标签是**同一位标注者**标的（"给结论+复核" / **盲标**），所以它测的是 **intra-rater 重测一致性**，**不是**"独立人类 vs judge" —— 要升级必须换一位没参与过的标注者。详见 `docs/step16-blind-relabel.md` |

> 这些数字全部可复现：花钱的那部分都记了实际金额，不花钱的入口写在上面三条命令里。

## 我错在哪（比数字更重要）

`lab/README.md` 的「我错在哪」一节有 **11 条自纠**，包括：用一个自己没验证过的观测手段得出假阴性、
任务集期望值写错导致 10 次“失败”全是假的、差点把一个不存在的缺陷（“缺 sqlite3”）写进结论、
差点把地板变化归功于自己的改动，以及一次过宽的工具链自动修复误伤了 40 个文件。

---

## 被测对象：MoocManus（部署与使用）

MoocManus 是一个通用的 AI Agent 系统，支持完全私有化部署，使用 A2A + MCP 连接 Agent/Tool，同时支持在沙箱中运行各种内置工具和操作。

> 它在本仓库里的角色是 **SUT**：`lab/` 用它做被测对象，而它本身也是可部署的完整系统。

## 项目结构

```
mooc-manus/
├── api/              # 后端 API 服务（FastAPI）—— 同时是 lab 的**被测对象**
├── lab/              # Agent 可靠性评测实验台（详见 lab/README.md）
├── ui/               # 前端服务（Next.js）
├── sandbox/          # 沙箱服务（Ubuntu + Chrome + VNC）
├── nginx/            # Nginx 网关配置
│   ├── nginx.conf
│   └── conf.d/
│       └── default.conf
├── docker-compose.yml
├── .env              # 环境变量配置（需自行创建）
└── README.md
```

## 快速部署

### 前置要求

- Docker >= 20.10
- Docker Compose >= 2.0

### 一键部署

1. **配置环境变量**

   项目根目录下的 `.env` 文件包含所有配置项，请根据实际情况修改：

   ```bash
   # 必须修改的配置
   COS_SECRET_ID=your_cos_secret_id_here       # 腾讯云 COS SecretId
   COS_SECRET_KEY=your_cos_secret_key_here     # 腾讯云 COS SecretKey
   COS_BUCKET=your_cos_bucket_here             # COS 存储桶名称

   # 可选修改
   POSTGRES_PASSWORD=postgres                   # 数据库密码
   NGINX_PORT=8088                              # 对外访问端口
   ```

2. **配置 AI 模型**

   修改 `api/config.yaml` 中的 LLM 配置：

   ```yaml
   llm_config:
     base_url: https://opencode.ai/zen/go/v1
     api_key: your_opencode_zen_api_key_here
     model_name: deepseek-v4.1-flash
   ```

3. **启动所有服务**

   ```bash
   docker compose up -d --build
   ```

4. **访问系统**

   打开浏览器访问 `http://your-server-ip:8088`

### 服务架构

```
                    ┌─────────────┐
     Port 8088      │   Nginx     │
   ─────────────────►  (Gateway)  │
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              │ /                       │ /api
              ▼                         ▼
       ┌─────────────┐          ┌─────────────┐
       │  Next.js UI │          │  FastAPI     │
       │  (Port 3000)│          │  (Port 8000) │
       └─────────────┘          └──────┬──────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                   │
                    ▼                  ▼                   ▼
             ┌───────────┐     ┌───────────┐       ┌───────────┐
             │ PostgreSQL│     │   Redis   │       │  Sandbox  │
             │(Port 5432)│     │(Port 6379)│       │ (VNC/HTTP)│
             └───────────┘     └───────────┘       └───────────┘
```

### 容器列表

| 容器名称 | 服务 | 说明 |
|---------|------|------|
| manus-nginx | Nginx | 反向代理网关，唯一对外暴露端口 |
| manus-ui | Next.js | 前端 UI 服务 |
| manus-api | FastAPI | 后端 API 服务 |
| manus-postgres | PostgreSQL | 数据库 |
| manus-redis | Redis | 缓存 |
| manus-sandbox | Sandbox | 沙箱环境（Chrome + VNC） |

### 常用命令

```bash
# 启动所有服务（后台运行）
docker compose up -d --build

# 查看所有服务状态
docker compose ps

# 查看服务日志
docker compose logs -f              # 所有服务
docker compose logs -f manus-api    # 仅 API 服务
docker compose logs -f manus-ui     # 仅 UI 服务

# 重启单个服务
docker compose restart manus-api

# 停止所有服务
docker compose down

# 停止并清除数据卷（谨慎操作）
docker compose down -v
```

### 启用 HTTPS

1. 将 SSL 证书放入 `nginx/ssl/` 目录：
   - `fullchain.pem`（证书链）
   - `privkey.pem`（私钥）

2. 修改 `nginx/conf.d/default.conf`，取消 SSL server 块注释

3. 修改 `docker-compose.yml`，取消 443 端口映射注释

4. 重启 Nginx：
   ```bash
   docker compose restart manus-nginx
   ```

## 本地开发

各子项目的本地开发说明请参考对应目录下的 README：

- [API 服务](./api/README.md)
- [前端 UI](./ui/README.md)
- [沙箱服务](./sandbox/README.md)
