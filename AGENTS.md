# AGENTS.md — 公开仓库协作约束

## 沟通与编码

- 默认使用简体中文说明问题，代码标识符和协议名称保持原样。
- 文本文件使用 UTF-8；部署脚本使用 LF 换行。
- 修改前先阅读 `README.md`、`docs/ARCHITECTURE.md`、`docs/contracts.md` 和 `docs/PRIVACY.md`。

## 隐私红线

- 绝不提交真实账号、密码、Key、Cookie、浏览器 profile、账户快照、策略归档或运行日志。
- 示例数据必须明显为虚构内容；网址优先使用保留域名 `.invalid`。
- 不得把本机用户名、绝对路径、服务器地址或内部运维记录写入仓库。
- 提交前运行 `python scripts/check_public_tree.py`。

## 核心行为边界

券商登录、验证码识别、交易执行、策略生成和自动轮次属于核心行为。除非项目所有者明确要求，不得修改、删除、重命名、重构或改变这些流程的既有语义，也不得加入额外确认步骤或默认禁用现有能力。

部署、文档、前端和不改变核心行为的数据展示改动可以正常进行。对边界有疑问时，先向项目所有者确认。

## 验证

- 后端：在 `source/backend` 运行 `PYTHONDONTWRITEBYTECODE=1 python -m tests.smoke`。
- 前端：在 `source/frontend` 运行 `npm ci && npm run check`。
- 发布：确认 `source/frontend/dist` 与 `frontend-dist` 逐文件一致，并检查 `docker compose -f deploy/compose.yaml --profile collector config`。
