# 部署指南

## 前置条件

- Linux 主机，建议至少为 Chromium 留出 1 GiB 内存。
- Docker Engine。
- Docker Compose v2，命令形式为 `docker compose`。
- 首次启动时可以访问容器镜像仓库。

项目不要求在宿主机安装 Python、Node.js、Nginx 或 Chromium。

## 一键启动

在仓库根目录运行：

```bash
bash scripts/start.sh
```

默认启动完整服务。脚本会：

1. 构建 API 和 Web 镜像。
2. 创建私有 `runtime` 与 `archives` Docker 卷。
3. 初始化卷权限。
4. 启动 Selenium Chromium、轮次驱动、API 和网页。
5. 在 Compose 支持时等待健康检查通过。

只启动 API 和网页：

```bash
bash scripts/start.sh web
```

## 部署参数

所有参数都有安全默认值，不创建 `.env` 也能启动。需要调整时：

```bash
cp .env.example .env
```

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | `zhixing-v3` | Compose 项目名及数据卷前缀 |
| `ZHIXING_WEB_HOST` | `127.0.0.1` | 网页监听地址 |
| `ZHIXING_WEB_PORT` | `18765` | 网页端口 |
| `ZHIXING_BROWSER_IMAGE` | 已验证的 Chromium 镜像 digest | 浏览器容器 |
| `ZHIXING_BROWSER_MEMORY` | `1g` | 浏览器内存上限 |
| `ZHIXING_BROWSER_SHM_SIZE` | `512m` | Chromium 共享内存 |
| `ZHIXING_API_IMAGE` | `zhixing-api:3.260817.00` | 本地 API 镜像标签 |
| `ZHIXING_WEB_IMAGE` | `zhixing-web:3.260817.00` | 本地 Web 镜像标签 |

`.env` 只用于非机密部署参数。模型 Key、验证码 Key、资金账号和交易密码不要写入 `.env`，应在网页“运行设置”中填写。

## 远程服务器访问

网页默认绑定 `127.0.0.1`，外部网络不能直接连接。推荐从本机建立 SSH 隧道：

```bash
ssh -L 18765:127.0.0.1:18765 user@server
```

隧道保持连接时，在本机打开 `http://127.0.0.1:18765`。

如果需要长期通过域名访问，建议保持回环绑定，在同一主机使用带身份认证和 TLS 的反向代理转发。不要把未加认证的工作台直接监听到公网。

## 首次运行设置

完整 Compose 中的浏览器服务名为 `browser`。在券商连接设置里填写：

```text
http://browser:4444/wd/hub
```

随后先在“交易对象”页添加标的，再按页面提示配置模型服务、验证码识别方式与备用服务、券商凭据、调度时点和运行模式。所有个人值都写入 `runtime` 卷。

验证码备用服务按数组位置关联已保存密钥。网页支持追加备用项；修改已有项的识别方式、地址或模型时必须同时填写新密钥，并且不提供已保存项的重排或删除操作，以免空密钥错位沿用。

## 日常运维

```bash
# 状态
bash scripts/status.sh

# 最近 200 行日志
bash scripts/logs.sh

# 只看 API 日志
bash scripts/logs.sh api

# 停止服务，保留数据卷
bash scripts/stop.sh

# 更新源码后重新构建并启动
bash scripts/start.sh
```

日志使用 Docker 的 `json-file` 驱动，并限制为每个容器最多 3 个、每个 10 MiB。

`stop.sh` 会删除容器：`runtime`、`archives` 及其中保存的券商凭据会保留，但浏览器容器里的 Cookie/profile 不持久化，下次启动可能需要重新登录。

## 数据与备份

运行数据位于 Compose 管理的两个命名卷：

- `runtime`：配置、凭据状态、账户快照和运行状态。
- `archives`：策略与执行归档。

卷的实际名称带 `COMPOSE_PROJECT_NAME` 前缀。备份前先运行 `bash scripts/stop.sh`，再使用主机现有的 Docker 卷备份方案同时备份这两个卷。备份文件本身含有个人和交易数据，不得提交到 Git。

`docker compose down` 会保留卷；`docker compose down -v` 会永久删除它们。不要在没有备份时使用 `-v`。

## 排错

```bash
docker compose -f deploy/compose.yaml --profile collector ps
docker compose -f deploy/compose.yaml --profile collector logs --tail=200
docker compose -f deploy/compose.yaml --profile collector config
```

- `data-init` 显示 `Exited (0)` 是正常状态，它只负责初始化数据卷权限。
- 浏览器长时间不健康时，先检查主机内存和镜像下载状态。
- 网页可打开但没有采集轮次时，确认使用的是默认 `full` 模式，而不是 `web` 模式。
- 修改 `.env` 中的 `COMPOSE_PROJECT_NAME` 会切换到另一组空白数据卷。
