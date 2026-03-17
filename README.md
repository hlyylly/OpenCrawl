# OpenCrawl

积分制分布式爬虫平台。使用者付积分调 API，Worker（Chrome 扩展）贡献算力赚积分，数据通过 Cloudflare R2 中转。

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
- **R2 自动过期** — 结果文件 1 天自动删除，不占存储

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

### 服务端

1. Python 3.10+ 环境
2. 安装依赖：`pip install -r requirements.txt`
3. 配置 `.env`：
   ```
   R2_ACCOUNT_ID=your_account_id
   R2_ACCESS_KEY_ID=your_access_key
   R2_SECRET_ACCESS_KEY=your_secret_key
   R2_BUCKET=OpenCrawl
   PORT=9877
   ADMIN_KEY=your_admin_key
   ```
4. 启动：`uvicorn server:app --host 0.0.0.0 --port 9877`

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
