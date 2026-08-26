# 架构说明

知行的公开部署由四个长期运行服务和一个初始化服务组成：

```text
浏览器 -> web (Nginx) -> api
                         |\
                         | +-> runtime 卷
                         +---> archives 卷

daemon -> browser (Selenium Chromium)
   |\
   | +-> runtime 卷
   +---> archives 卷
```

- `web`：托管 `frontend-dist`，并把 `/api/` 请求转发给 `api`。
- `api`：提供状态、历史、配置与工作台接口。
- `daemon`：独立运行计划轮次；与 API 分进程，避免轮次异常影响网页可用性。
- `browser`：提供独立的远程 Chromium 会话。
- `data-init`：短暂运行一次，为非 root API 用户初始化 Docker 卷权限。

API 和 daemon 使用同一个后端镜像，但入口命令不同。它们共享运行状态和归档卷；浏览器容器不复用其他项目的会话。

## 源码与发布产物

- 后端：`source/backend/zhixing/`
- 前端：`source/frontend/`
- 网页发布产物：`frontend-dist/`
- 容器定义：`deploy/`

前端 `npm run check` 会生成 `source/frontend/dist/`。CI 会把它与 `frontend-dist/` 逐文件比较，防止公开的源码和实际部署网页不一致。

## 数据边界

源码镜像为只读文件系统。可变数据只写入两个 Docker 卷；临时文件写入受限的 `/tmp`。容器日志写到 Docker 日志驱动并进行大小轮转。

网页端口默认只发布到宿主机 `127.0.0.1`。服务间通信留在 Compose 网络中，API 和 Selenium 端口不发布到宿主机。
