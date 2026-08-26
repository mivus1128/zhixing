# 测试兼容镜像

这里的两个 Dockerfile 仅用于保持原始后端 smoke 测试的相对路径兼容，权威部署入口始终是仓库根目录 `deploy/`。

CI 会逐字节比较两处 Dockerfile；修改根目录部署文件时必须同步这里。不要从本目录运行 Docker 构建。
