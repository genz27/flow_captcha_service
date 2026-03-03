# flow_captcha_service

`flow_captcha_service` 是一个独立的有头浏览器打码服务，专门给 `flow2api`
通过 HTTP 透传调用。

## 目标范围

- 只支持有头浏览器打码（Playwright）。
- 不接第三方打码平台（yescaptcha/capsolver 等）。
- 提供会话化接口：`solve -> finish/error`。
- 提供后台 API：管理 API Key、可用次数、日志、基础统计。

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

## 核心接口

### 1. 业务打码接口

- `POST /api/v1/solve`
- `POST /api/v1/sessions/{session_id}/finish`
- `POST /api/v1/sessions/{session_id}/error`

调用时需携带：

```http
Authorization: Bearer <service_api_key>
```

`solve` 请求体示例：

```json
{
  "project_id": "xxxx",
  "action": "IMAGE_GENERATION",
  "token_id": null
}
```

`solve` 返回示例：

```json
{
  "success": true,
  "session_id": "f4f1a8d6-...",
  "token": "03AFc....",
  "fingerprint": {
    "userAgent": "..."
  },
  "node_name": "standalone-node",
  "expires_in_seconds": 7200
}
```

### 2. 管理后台 API

- `POST /api/admin/login`
- `POST /api/admin/logout`
- `GET /api/admin/apikeys`
- `POST /api/admin/apikeys`
- `PATCH /api/admin/apikeys/{api_key_id}`
- `GET /api/admin/logs`
- `GET /api/admin/stats`
- `GET /api/admin/captcha-config`
- `POST /api/admin/captcha-config`

管理员登录默认账号密码来自 `config/setting.toml` 或环境变量：

- `FCS_ADMIN_USERNAME`
- `FCS_ADMIN_PASSWORD`

## 配置

复制示例配置：

```bash
cp config/setting_example.toml config/setting.toml
```

常用环境变量：

- `FCS_SERVER_HOST`
- `FCS_SERVER_PORT`
- `FCS_DB_PATH`
- `FCS_ADMIN_USERNAME`
- `FCS_ADMIN_PASSWORD`
- `FCS_BROWSER_COUNT`
- `FCS_BROWSER_PROXY_ENABLED`
- `FCS_BROWSER_PROXY_URL`
- `ALLOW_DOCKER_HEADED_CAPTCHA=true`

## Docker（有头）

```bash
docker compose -f docker-compose.headed.yml up -d --build
docker compose -f docker-compose.headed.yml logs -f
```

## 下一步计划

- `v0.2`：补管理后台页面（Web UI）。
- `v0.3`：引入主节点/子节点集群角色。
- `v0.4`：增加子节点健康检查与调度策略。
