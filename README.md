# flow_captcha_service

`flow_captcha_service` 是独立的有头浏览器打码服务，给 `flow2api`
通过 HTTP 透传调用。

## 能力范围

- 只支持有头浏览器打码（Playwright）。
- 不接第三方打码平台（yescaptcha/capsolver 等）。
- 会话化协议：`solve -> finish/error`。
- 支持 `standalone / master / subnode` 三种角色。
- 支持 API Key、额度、日志、基础统计。

## 角色说明

- `standalone`：本地直接打码。
- `master`：不本地打码，只调度子节点。
- `subnode`：本地打码，并向 master 注册/心跳。

> 关键约束：`master` 角色不会执行本地浏览器打码。

## 核心接口

### 业务接口（需要 `Authorization: Bearer <service_api_key>`）

- `POST /api/v1/solve`
- `POST /api/v1/sessions/{session_id}/finish`
- `POST /api/v1/sessions/{session_id}/error`
- `POST /api/v1/custom-score`
- `GET /api/v1/health`

`master` 返回的 `session_id` 为可路由格式：

```text
nodeId:childSessionId
```

### 集群内部接口（需要 `X-Cluster-Key`，仅 master 生效）

- `POST /api/cluster/register`
- `POST /api/cluster/heartbeat`

### 管理接口

- `POST /api/admin/login`
- `POST /api/admin/logout`
- `GET /api/admin/profile`
- `POST /api/admin/credentials`
- `GET /api/admin/system-config`
- `POST /api/admin/system-config`
- `GET /api/admin/apikeys`
- `POST /api/admin/apikeys`
- `PATCH /api/admin/apikeys/{api_key_id}`
- `GET /api/admin/logs`
- `GET /api/admin/stats`
- `GET /api/admin/captcha-config`
- `POST /api/admin/captcha-config`
- `GET /api/admin/cluster/config`
- `POST /api/admin/cluster/config/rotate-key`
- `GET /api/admin/cluster/nodes`
- `PATCH /api/admin/cluster/nodes/{node_id}`

### 管理面板

- 入口：`GET /admin`
- 登录后会根据节点角色展示不同内容：
  - `master`：展示 API Key 管理、Cluster Key 轮换、子节点列表管理
  - `subnode`：隐藏主节点专属模块，保留运行配置与系统配置
  - `standalone`：展示基础运行管理能力
- 系统配置支持在线写入 `config/setting.toml`，部分字段会提示“需要重启服务”。

## 配置项

复制模板：

```bash
cp config/setting_example.toml config/setting.toml
```

### `cluster` 段说明

```toml
[cluster]
role = "standalone"              # standalone / master / subnode
master_base_url = ""             # subnode 必填
master_cluster_key = ""          # subnode 必填
node_public_base_url = ""        # subnode 对 master 暴露地址
node_api_key = ""                # subnode 被 master 调用的内部 key
heartbeat_interval_seconds = 15
node_weight = 100
node_max_concurrency = 1
master_node_stale_seconds = 120
master_dispatch_timeout_seconds = 45
```

常用环境变量：

- `FCS_CLUSTER_ROLE`
- `FCS_CLUSTER_MASTER_BASE_URL`
- `FCS_CLUSTER_MASTER_CLUSTER_KEY`
- `FCS_CLUSTER_NODE_PUBLIC_BASE_URL`
- `FCS_CLUSTER_NODE_API_KEY`
- `FCS_CLUSTER_HEARTBEAT_INTERVAL_SECONDS`
- `FCS_CLUSTER_NODE_WEIGHT`
- `FCS_CLUSTER_NODE_MAX_CONCURRENCY`

## 快速启动

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
python main.py
```

默认地址：`http://127.0.0.1:8060`

## Docker（有头）

### standalone

```bash
docker compose -f docker-compose.headed.yml up -d --build
```

使用浏览器镜像：`Dockerfile.headed`

### master

```bash
docker compose -f docker-compose.cluster.master.yml up -d --build
```

使用轻量镜像：`Dockerfile.master`

### subnode

```bash
docker compose -f docker-compose.cluster.subnode.yml up -d --build
```

使用浏览器镜像：`Dockerfile.headed`

> `subnode` 启动前，需要在 `master` 管理接口获取 `cluster_key`，并写入
> `FCS_CLUSTER_MASTER_CLUSTER_KEY`。

### 一键起 master + subnode（示例）

```bash
docker compose -f docker-compose.cluster.stack.yml up -d --build
```

> `docker-compose.cluster.stack.yml` 默认带占位符：
> `FCS_CLUSTER_MASTER_CLUSTER_KEY` 与 `FCS_CLUSTER_NODE_API_KEY`。
> 请在启动前替换为真实值。

## GHCR Packages

仓库已内置 GitHub Actions 工作流：

- 文件：`.github/workflows/publish-ghcr.yml`
- 触发：`push main`、`push tag(v*)`、手动触发(`workflow_dispatch`)
- 目标镜像：
  - `ghcr.io/<owner>/flow_captcha_service-master`
  - `ghcr.io/<owner>/flow_captcha_service-headed`

镜像分工：

- `flow_captcha_service-master`：轻量主节点镜像，不安装 Playwright/Chromium
- `flow_captcha_service-headed`：有头浏览器镜像，用于 `subnode` / `standalone`

典型拉取示例：

```bash
docker pull ghcr.io/genz27/flow_captcha_service-master:latest
docker pull ghcr.io/genz27/flow_captcha_service-headed:latest
```

如果仓库/包是私有，请使用有 `read:packages` 权限的 PAT 登录后再拉取。

### 拉取镜像是否需要环境变量？

- `docker pull`：不需要设置环境变量。
- `docker run` / `docker compose up`：需要按节点角色配置环境变量。

最小角色配置建议：

- `master`：
  - `FCS_CLUSTER_ROLE=master`
- `subnode`：
  - `FCS_CLUSTER_ROLE=subnode`
  - `FCS_CLUSTER_MASTER_BASE_URL`
  - `FCS_CLUSTER_MASTER_CLUSTER_KEY`
  - `FCS_CLUSTER_NODE_PUBLIC_BASE_URL`
  - `FCS_CLUSTER_NODE_API_KEY`

如果使用仓库内的 compose 文件，绝大多数变量已内置，通常只需要替换：

- `FCS_CLUSTER_MASTER_CLUSTER_KEY`
- `FCS_CLUSTER_NODE_API_KEY`

## 主节点如何通知子节点关闭

`flow_captcha_service` 没有单独的 `/close` 业务接口，关闭语义通过会话协议实现：

1. `flow2api` 先向 `master` 调用 `POST /api/v1/solve`，master 返回路由 session：
   `nodeId:childSessionId`
2. 业务请求成功后，`flow2api` 调 `POST /api/v1/sessions/{session_id}/finish`
3. `master` 解析路由 session 并转发到对应 `subnode`
4. `subnode` 执行本地 `runtime.finish()`，通知浏览器打码实例“请求已结束，可关闭”
5. 若业务失败则走 `POST /api/v1/sessions/{session_id}/error`，会触发提前错误关闭

这就是“主节点告诉子节点关闭”的实际链路：`finish/error` 转发，而不是单独 close。
