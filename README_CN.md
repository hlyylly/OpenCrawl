# OpenCrawl

**分布式浏览器渲染服务。** Worker 贡献真实 Chrome 浏览器，使用者付积分即可爬取任意 JS 渲染页面。

专为 [OpenClaw](https://github.com/browser-use/web-ui) 等运行在无头 VPS 上、缺少真实浏览器环境的 AI Agent 工具而生。

[English](README.md)

---

## 为什么需要 OpenCrawl？

AI Agent 和爬虫工具跑在 VPS/云服务器上时，往往无法渲染 JavaScript 页面——因为没有真实浏览器。Puppeteer、Playwright 等方案需要 4GB+ 内存，部署维护成本高。

OpenCrawl 通过**众包真实 Chrome 浏览器**解决这个问题：

- 任何人安装 Chrome 扩展即可成为 **Worker**
- Worker 每完成一个页面渲染任务获得积分
- 使用者通过简单的 API 调用消费积分爬取任意 URL
- 结果存储在 **Cloudflare R2**（零出口流量费）
- Worker 的 Cookie 和登录状态通过**无痕窗口完全隔离**

## OpenClaw 集成

OpenCrawl 专为 [OpenClaw](https://github.com/browser-use/web-ui) 在无头 VPS 上的部署场景设计。

无需在服务器上安装 Chromium + Playwright（4GB+ 内存，配置复杂），直接调用 OpenCrawl API：

```python
import requests

# 通过真实 Chrome 浏览器获取任意 JS 渲染页面
res = requests.post("https://your-opencrawl-server/api/crawl",
    headers={"Authorization": "Bearer ak_your_key"},
    json={"url": "https://example.com", "selector": ".main-content"})

data = res.json()
# data["downloadUrl"] → 从 R2 下载渲染后的页面内容
```

让你的 VPS Agent 拥有**真实浏览器池**，无需任何本地浏览器安装。

## 架构

```
使用者                    平台 (FastAPI)              Worker (Chrome 扩展)
  │                         │                            │
  │  POST /api/crawl        │                            │
  │  Authorization: Bearer  │      WebSocket /ws         │
  │  ak_xxx                 │◄───────────────────────────┤
  ├────────────────────────►│                            │
  │                         │  下发任务 + R2 上传地址     │
  │                         ├───────────────────────────►│
  │                         │                            │ 打开标签页
  │                         │                            │ 渲染页面
  │                         │                            │ 提取内容
  │                         │            Cloudflare R2   │
  │                         │           (零出口流量)     │
  │                         │                ▲           │
  │                         │                │ PUT       │
  │                         │                ├───────────┤
  │                         │  taskComplete  │           │
  │                         │◄───────────────┤           │
  │                         │                            │
  │  积分结算:               │                            │
  │  使用者 -1, Worker +1   │                            │
  │                         │                            │
  │  {downloadUrl}          │                            │
  │◄────────────────────────┤                            │
  │                         │                            │
  │  GET downloadUrl ───────┼───────► R2 下载结果         │
```

## 功能

- **API 爬取** — 发送 URL，返回渲染后的页面文本 + R2 下载链接
- **积分系统** — 使用者消费积分，Worker 赚取积分
- **API Key 认证** — 每个用户独立的 API Key
- **管理后台** — 创建用户、充值积分、查看统计
- **用户面板** — 查看积分余额、API Key、使用示例
- **Dashboard** — 实时监控 Worker 连接、任务状态
- **域名负载均衡** — 同一域名分散到不同 Worker
- **隐私保护** — 爬取在无痕窗口中进行，完全隔离 Worker 的 Cookie 和登录态
- **URL 黑名单** — 屏蔽 localhost、内网 IP、云 metadata、危险端口
- **R2 自动过期** — 结果文件 1 天自动删除，不占存储
- **一键注册** — 首页注册即送 100 积分

## 技术栈

| 组件 | 技术 |
|------|------|
| 平台服务端 | Python / FastAPI / uvicorn |
| 数据存储 | Cloudflare R2 (S3 兼容) |
| 积分存储 | JSON 文件 (data/users.json) |
| Worker | Chrome Extension (Manifest V3) |
| 通信协议 | HTTP API + WebSocket |

## 文件结构

```
OpenCrawl/
├── server.py            # FastAPI 服务端 (API + WebSocket + 积分)
├── dashboard.html       # 监控面板
├── admin.html           # 管理后台
├── user.html            # 用户面板
├── requirements.txt     # Python 依赖
├── .env                 # 环境变量 (不提交)
├── .env.example         # 环境变量示例
├── data/
│   └── users.json       # 用户数据 (不提交)
└── extension/           # Chrome 扩展
    ├── manifest.json
    ├── background.js    # WebSocket + 任务调度 + R2 上传
    ├── content.js       # 页面内容提取
    ├── popup.html       # 扩展弹窗 UI
    └── popup.js         # 弹窗逻辑
```

## 部署

### 1. 创建 Cloudflare R2 Bucket

1. 注册 [Cloudflare](https://dash.cloudflare.com/) 账号
2. 进入 **R2 Object Storage** → **Create bucket**，名称填 `opencrawl`（或自定义）
3. 进入 **R2** → **Manage R2 API Tokens** → **Create API Token**
   - Permissions: `Object Read & Write`
   - 创建后记下以下三个值：

| 环境变量 | 说明 | 获取位置 |
|---------|------|---------|
| `R2_ACCOUNT_ID` | Cloudflare Account ID | Dashboard 右侧栏，32 位字符串 |
| `R2_ACCESS_KEY_ID` | R2 API Token 的 Access Key | 创建 API Token 后显示 |
| `R2_SECRET_ACCESS_KEY` | R2 API Token 的 Secret Key | 创建 API Token 后显示（仅显示一次） |

### 2. 部署服务端

1. Python 3.10+ 环境
2. 安装依赖：`pip install -r requirements.txt`
3. 复制并编辑环境变量：
   ```bash
   cp .env.example .env
   ```
   编辑 `.env`：
   ```bash
   # Cloudflare R2（必填）
   R2_ACCOUNT_ID=你的Account_ID          # Cloudflare Dashboard 右侧栏
   R2_ACCESS_KEY_ID=你的Access_Key_ID    # R2 API Token 创建后获得
   R2_SECRET_ACCESS_KEY=你的Secret_Key   # R2 API Token 创建后获得（仅显示一次）
   R2_BUCKET=opencrawl                   # 你创建的 bucket 名称

   # 服务配置
   PORT=9877

   # 管理员密钥（自定义，用于创建用户和充值积分）
   ADMIN_KEY=your_admin_secret_key
   ```
4. 启动：
   ```bash
   uvicorn server:app --host 0.0.0.0 --port 9877
   ```
5. 首次启动后，创建第一个用户：
   ```bash
   curl -X POST http://localhost:9877/api/admin/create-key \
     -H "Authorization: Bearer your_admin_secret_key" \
     -H "Content-Type: application/json" \
     -d '{"name": "我的账号", "credits": 100}'
   ```
   返回的 `apiKey`（如 `ak_xxx`）就是你的 API Key

### Chrome 扩展 (Worker)

1. 打开 `chrome://extensions/`，启用开发者模式
2. 点击「加载已解压的扩展程序」，选择 `extension/` 目录
3. 点击扩展图标，配置：
   - 服务端地址：`ws://你的服务器IP:9877/ws`
   - API Key：填入你的 Key（可选，用于赚积分）
4. 点击「保存并重连」

## API

### 爬取页面（需认证）

```bash
curl -X POST http://your-server:9877/api/crawl \
  -H "Authorization: Bearer ak_xxx" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "selector": ".article"}'
```

响应：
```json
{
  "success": true,
  "url": "https://example.com",
  "r2Key": "tasks/xxx.json",
  "downloadUrl": "https://...签名下载链接..."
}
```

### 查询积分

```bash
curl http://your-server:9877/api/balance \
  -H "Authorization: Bearer ak_xxx"
```

### 平台状态（公开）

```bash
curl http://your-server:9877/api/status
```

### 管理员 — 创建用户

```bash
curl -X POST http://your-server:9877/api/admin/create-key \
  -H "Authorization: Bearer your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{"name": "用户名", "credits": 100}'
```

### 管理员 — 充值积分

```bash
curl -X POST http://your-server:9877/api/admin/recharge \
  -H "Authorization: Bearer your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{"apiKey": "ak_xxx", "credits": 50}'
```

## 页面

| 路径 | 说明 |
|------|------|
| `/` | Dashboard 监控面板 |
| `/admin` | 管理后台（需 Admin Key） |
| `/user` | 用户面板（需 API Key） |

## R2 免费额度

| 项目 | 免费/月 |
|------|--------|
| 存储 | 10 GB |
| 写入 (PUT) | 100 万次 |
| 读取 (GET) | 1000 万次 |
| 出口流量 | 无限免费 |

按每任务 30KB、1天过期计算，每天 3 万个任务完全免费。
