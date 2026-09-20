# MoocManus - 通用 AI Agent 系统

MoocManus 是一个通用的 AI Agent 系统，支持完全私有化部署，使用 A2A + MCP 连接 Agent/Tool，同时支持在沙箱中运行各种内置工具和操作。

> ### 🔬 另附：Agent 可靠性评测实验台（`lab/`）
>
> 本仓库同时包含一套独立的、不依赖任何评测框架的 **Agent 评测与加固工具**：
> 22 个确定性任务集（含 CI 失败归因）、span 级轨迹、10 类故障注入、
> 预算/熔断/重试/后置校验中间件，
> 以及统计（Wilson / bootstrap / `pass^k`）与对照实验开关。
>
> 它把上方的 Agent 当作**被测对象**，回答"它做得对吗、每次都能做对吗、钱花在哪了、坏了怎么修"。
>
> 另外三项让"每个 delta 都可信"的基础设施：
> - **LLM 录制回放**：真跑 24.5s / $0.057 → 离线复现 **0.4s / $0**（不需要 API Key）；
> - **交错 A/B**：两条臂在同一个 suite 内轮转，分离"时间相关的服务端漂移"；
> - **噪声地板**：测出"同一配置跑两次能差多少"，回归门禁的阈值由此而来。
>
> 详见 **[lab/README.md](./lab/README.md)**；不花钱的入口：`python -m lab bench validate`。

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
     base_url: https://api.deepseek.com/
     api_key: your_api_key_here
     model_name: deepseek-reasoner
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
