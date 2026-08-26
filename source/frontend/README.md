# 知行前端

React + Vite + TypeScript 前端。业务请求统一经过 `src/api/`，`src/fixtures/` 只提供虚构的开发与测试场景。

```bash
npm ci
npm run dev
npm run check
```

`npm run dev` 默认使用 `src/fixtures/` 的虚构场景。连接真实后端调试时设置 `VITE_API_BASE_URL`，例如 `VITE_API_BASE_URL=http://127.0.0.1:8765`；不要把真实地址或凭据写入仓库。

生产构建输出到 `dist/`。当前源码可重复生成仓库根目录 `frontend-dist/` 中的发布产物；发布前应逐文件比较两者。

约束：

- 页面和组件不得直接导入 fixture。
- 页面和组件不得绕过 API 客户端直接调用 `fetch`。
- 成功空态与 `ok:false` 错误必须保持可区分。
- fixture 不得使用真实账号、持仓、交易或服务凭据。
