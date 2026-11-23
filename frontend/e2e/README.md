# 前端浏览器冒烟测试

用 Playwright 驱动 headless 浏览器，真实走一遍界面：API Key 准入 → 新建 Agent →
新建会话 → 流式发消息 → 四个视图 → 窄屏 / 手机宽度布局 → 错误提示，并保存截图。

## 前置

```bash
pip install playwright
# 优先使用系统已安装的 Edge / Chrome；没有才需要下载 Chromium：
python -m playwright install chromium
```

脚本按 `msedge` → `chrome` → 自带 Chromium 的顺序尝试，所以 Windows 上通常不用下载。

## 运行

需要两个进程：

```bash
# 1. 后端（8000）
cd backend
DATABASE_URL=sqlite:///./e2e.db \
MODEL_PROVIDER=mock \
CORS_ORIGINS=http://localhost:5173 \
BOOTSTRAP_ADMIN_API_KEY=oak_e2e_admin_key_0001 \
python -m uvicorn app.main:app --port 8000

# 2. 构建产物（5173）
cd frontend
npm run build
python -m http.server 5173 --directory dist
```

然后：

```bash
cd frontend
python e2e/smoke.py /tmp/e2e-shots
```

全部断言通过时会打印 `全部通过`，截图写到指定目录。

## 覆盖的断言

- 未认证时显示准入界面；填入密钥后进入工作区
- `window.prompt` 新建 Agent（对话框由 Playwright 接管）
- 新建会话、发送消息、等待流式结束并拿到回答
- 工具 / 运行 / 组织三个视图可切换且标题正确
- 900px 与 420px 下侧栏变成整宽顶栏；整页无横向溢出
- 非法 JSON schema 触发错误提示
