# OpenCrawl 架构

## 系统总览

```
                    ┌──────────────────────────────┐
                    │       使用者 (API 调用方)     │
                    │  Python / cURL / Node.js     │
                    │  Authorization: Bearer ak_xx │
                    └──────────────┬───────────────┘
                                   │
                          POST /api/crawl
                          {url, selector?}
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │     Platform (Node.js)        │
                    │     :9877 HTTP API            │
                    │     :9876 WebSocket           │
                    │                              │
                    │  • API Key 认证              │
                    │  • 积分检查 & 扣除           │
                    │  • 任务分发 (WebSocket)       │
                    │  • R2 签名 URL 生成          │
                    │  • 积分结算 (Worker 加分)     │
                    └───────┬──────────┬───────────┘
                            │          │
                   WebSocket│          │HTTP Response
                            │          │{downloadUrl}
                            ▼          │
            ┌───────────────────┐      │
            │  Chrome Extension │      │
            │  (Worker)         │      │
            │                   │      │
            │  • 接收任务       │      │
            │  • 打开标签页渲染 │      │
            │  • 提取 DOM 内容  │      │
            │  • 上传到 R2      │ ─────┼──► Cloudflare R2
            │  • 汇报完成       │      │    (零出口流量)
            └───────────────────┘      │
                                       │
                    使用者 ◄────────────┘
                    通过 downloadUrl 从 R2 下载结果
```

## 积分流转

```
使用者调用 API ──► 扣 1 积分 ──► 任务分发 ──► Worker 完成 ──► Worker 得 1 积分
                                                              使用者得 downloadUrl
```

## 请求流程

```
 ┌──────┐  POST /api/crawl   ┌──────────┐  WebSocket    ┌───────────┐
 │使用者├───────────────────►│ Platform  ├─────────────►│  Worker   │
 │      │  Authorization     │          │  {task,       │ (Chrome)  │
 │      │  {url, selector}   │ 验证Key  │   uploadUrl}  │           │
 └──┬───┘                    │ 检查积分  │               │ 打开标签页│
    │                        └────┬─────┘               │ 渲染页面  │
    │                             │                     │ 提取内容  │
    │                             │                     └─────┬─────┘
    │                             │                           │
    │                             │                     PUT uploadUrl
    │                             │                           │
    │                             │                     ┌─────▼─────┐
    │                             │                     │Cloudflare │
    │                             │    taskComplete      │    R2     │
    │                             │◄─────────────────────│  (存储)   │
    │                             │                     └─────┬─────┘
    │  {downloadUrl}              │  验证上传 → 积分结算       │
    │◄────────────────────────────┤                           │
    │                             │                           │
    │  GET downloadUrl            │                           │
    │─────────────────────────────┼───────────────────────────►
    │  ◄── 爬取结果 JSON ─────────┼───────────────────────────┤
    │                             │                    (1天自动过期)
```

## 文件结构

```
OpenCrawl/
├── server.js               # Platform 服务端
│                           #   HTTP :9877  API + Dashboard
│                           #   WS   :9876  Worker 通信
│                           #   认证、积分、R2 签名 URL
│
├── dashboard.html          # Web Dashboard (监控面板)
│
├── data/                   # 持久化数据
│   └── users.json          # 用户 + 积分 + API Key
│
├── extension/              # Chrome 扩展 (Worker)
│   ├── manifest.json       # MV3 配置
│   ├── background.js       # Service Worker (WS + 任务调度 + R2 上传)
│   ├── content.js          # 内容脚本 (注入目标页, 提取 DOM)
│   ├── popup.html          # 扩展弹窗 UI (配置 + 状态)
│   ├── popup.js            # 弹窗逻辑
│   └── icon*.png           # 图标
│
├── .env                    # 环境变量 (R2 密钥等)
├── package.json
└── test.js                 # API 测试脚本
```

## API 接口

```
需要认证 (Authorization: Bearer ak_xxx):

  POST /api/crawl
    Request:  {"url": "https://...", "selector": ".class" (可选)}
    Response: {"success": true, "r2Key": "tasks/xxx.json", "downloadUrl": "https://..."}
    积分:     成功扣 1 积分

  GET /api/crawl?url=https://...&selector=.class&key=ak_xxx
    Response: 同上

  GET /api/balance
    Response: {"credits": 99, "totalUsed": 1, "totalEarned": 0}

公开:

  GET /api/status
    Response: {"workers": 1, "activeTasks": 0, "totalCompleted": 5, ...}

  GET /
    Dashboard 监控面板

管理员 (Authorization: Bearer admin_key):

  POST /api/admin/create-key
    Request:  {"name": "用户名", "credits": 100}
    Response: {"apiKey": "ak_xxx", "credits": 100}

  POST /api/admin/recharge
    Request:  {"apiKey": "ak_xxx", "credits": 50}
    Response: {"apiKey": "ak_xxx", "credits": 150}
```
