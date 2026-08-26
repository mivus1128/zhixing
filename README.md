# 知行 Zhixing

知行是第三代自托管交易研究与自动化工作台。项目包含 Python 后端、React 前端、独立浏览器容器和 Docker Compose 部署配置。

当前构建号为 `3.260817.00`。它采用“代次.日期.构建”的项目编号，不是语义化版本号。

## 公开版包含什么

- `source/backend/`：后端源码与自检用例。
- `source/frontend/`：React + TypeScript 前端源码和锁定依赖。
- `frontend-dist/`：可直接部署的网页产物；由仓库中的前端源码可重复构建。
- `deploy/`：API、轮次驱动、浏览器与 Web 网关的容器配置。
- `scripts/`：启动、停止、状态检查和公开内容扫描脚本。

公开仓库不包含任何资金账号、交易密码、API Key、Cookie、浏览器登录态、策略归档、账户快照或服务器日志。首次启动会得到一个空白运行环境，个人配置需要在网页的“运行设置”中填写。

## 快速启动

适用于装有 Docker Engine 和 Docker Compose v2 的 Linux 主机：

```bash
cd <仓库目录>
bash scripts/start.sh
```

脚本默认构建并启动完整的 `api`、`daemon`、`browser` 和 `web` 服务。启动完成后，在部署机器上打开：

```text
http://127.0.0.1:18765
```

只想先查看空白工作台、不启动采集轮次和浏览器时：

```bash
bash scripts/start.sh web
```

默认只监听本机回环地址，不会直接暴露到公网。部署在远程服务器时，从自己的电脑建立 SSH 隧道：

```bash
ssh -L 18765:127.0.0.1:18765 user@server
```

然后仍访问 `http://127.0.0.1:18765`。

## 首次配置

进入“运行设置”后按页面提示填写：

1. 在“交易对象”页添加需要研究或运行的标的；公开版不会预置个人清单。
2. 模型服务的接口地址、协议、模型名称和 Key。
3. 验证码服务的识别方式、接口地址、模型和 Key；需要时可增加备用识别服务。
4. 券商连接；Docker 部署的浏览器远端填写 `http://browser:4444/wd/hub`，账号和交易密码在这里录入。
5. 调度时点与运行模式。

这些值保存在 Docker 的私有 `runtime` 卷中，不写入源码目录，也不通过 `.env` 提交。重新构建镜像不会自动删除它们。

验证码备用服务按显示顺序与已保存密钥对应。公开界面允许追加备用服务和修改原位置配置，但不会重排或删除已保存项目；更换某一项的识别方式、地址或模型时，需要同时输入该项的新密钥。

## 常用命令

```bash
# 查看服务状态
bash scripts/status.sh

# 查看日志
bash scripts/logs.sh

# 停止容器但保留运行数据
bash scripts/stop.sh

# 运行公开内容扫描、后端自检和前端构建校验；有 Docker 时也校验 Compose
bash scripts/verify.sh
```

部署参数可通过根目录 `.env` 调整；先复制 [.env.example](.env.example)，其中只允许放非机密的容器参数。完整部署说明见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，数据边界见 [docs/PRIVACY.md](docs/PRIVACY.md)。

## 开发

后端仅使用 Python 标准库：

```bash
cd source/backend
PYTHONDONTWRITEBYTECODE=1 python -m tests.smoke
```

前端使用锁定版本的 Node 依赖：

```bash
cd source/frontend
npm ci
npm run check
```

`npm run check` 会生成 `source/frontend/dist/`。发布前，该目录应与根目录 `frontend-dist/` 逐文件一致。

## 许可证

本项目以 [Apache License 2.0](LICENSE) 发布。自动化交易具有实际资金风险，使用者需要自行确认券商规则、接口许可和适用法律，并对运行结果负责。
