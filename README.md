# OpenCrawl

**Distributed browser rendering as a service.** Workers contribute real Chrome browsers, users pay credits to crawl any JavaScript-rendered page.

Built for tools like [OpenClaw](https://github.com/browser-use/web-ui) that run on headless VPS environments without real browser access.

[中文文档](README_CN.md)

---

## Why OpenCrawl?

AI agents and scraping tools on VPS/cloud servers often can't render JavaScript-heavy pages — they lack a real browser. Solutions like Puppeteer or Playwright are resource-heavy (4GB+ RAM) and hard to maintain.

OpenCrawl solves this by **crowdsourcing real Chrome browsers**:

- Anyone can install the Chrome extension and become a **Worker**
- Workers earn credits for each page they render
- Users spend credits to crawl any URL via a simple API
- Results are stored on **Cloudflare R2** (zero egress fees)
- Workers' cookies and sessions are **isolated via incognito mode**

```
User (API)                Platform (FastAPI)           Worker (Chrome Extension)
    │                          │                             │
    │  POST /api/crawl         │        WebSocket /ws        │
    │  {url, selector?}        │◄────────────────────────────┤
    ├─────────────────────────►│                             │
    │                          │  dispatch task + upload URL  │
    │                          ├────────────────────────────►│
    │                          │                             │ open tab (incognito)
    │                          │                             │ render JavaScript
    │                          │                             │ extract DOM content
    │                          │          Cloudflare R2      │
    │                          │         (zero egress)       │
    │                          │               ▲ PUT         │
    │                          │               ├─────────────┤
    │                          │  taskComplete  │             │
    │  credits: user -1        │◄──────────────┤             │
    │  credits: worker +1      │               │             │
    │                          │               │             │
    │  {downloadUrl}           │               │             │
    │◄─────────────────────────┤               │             │
    │                          │               │             │
    │  GET downloadUrl ────────┼──────► R2 download          │
```

## OpenClaw Integration

OpenCrawl is designed as a **browser rendering backend** for [OpenClaw](https://github.com/browser-use/web-ui) agents running on headless VPS environments.

Instead of installing Chromium + Playwright on your server (4GB+ RAM, complex setup), point your OpenClaw agent to OpenCrawl's API:

```python
import requests

# Fetch any JS-rendered page through real Chrome browsers
res = requests.post("https://your-opencrawl-server/api/crawl",
    headers={"Authorization": "Bearer ak_your_key"},
    json={"url": "https://example.com", "selector": ".main-content"})

data = res.json()
# data["downloadUrl"] → download the rendered page content from R2
```

This gives your VPS-hosted agent access to a **pool of real browsers** without any local browser installation.

## Features

- **API Crawling** — Send a URL, get back rendered page text + R2 download link
- **Credit System** — Users spend credits, Workers earn credits
- **API Key Auth** — Each user gets a unique API key
- **Admin Panel** — Create users, recharge credits, view stats
- **User Panel** — Check balance, view API key, usage examples
- **Dashboard** — Real-time monitoring of Workers, tasks, history
- **Domain Load Balancing** — Tasks distributed across Workers per domain
- **Privacy Protection** — Crawling happens in incognito windows, isolating Worker cookies
- **URL Blacklist** — Blocks localhost, internal IPs, cloud metadata, dangerous ports
- **Auto Cleanup** — R2 objects expire after 1 day, zero storage cost
- **One-Click Registration** — Sign up on the homepage, get 100 free credits

## Quick Start

### 1. Set Up Cloudflare R2

1. Create a [Cloudflare](https://dash.cloudflare.com/) account
2. Go to **R2 Object Storage** → **Create bucket** (name: `opencrawl`)
3. Go to **R2** → **Manage R2 API Tokens** → **Create API Token**
   - Permissions: `Object Read & Write`

You'll need these values:

| Variable | Description | Where to find |
|----------|-------------|---------------|
| `R2_ACCOUNT_ID` | Cloudflare Account ID | Dashboard sidebar, 32-char string |
| `R2_ACCESS_KEY_ID` | R2 API Access Key | Shown after creating API Token |
| `R2_SECRET_ACCESS_KEY` | R2 API Secret Key | Shown once after creating API Token |

### 2. Deploy the Server

```bash
git clone https://github.com/hlyylly/OpenCrawl.git
cd OpenCrawl
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your R2 credentials and admin key

uvicorn server:app --host 0.0.0.0 --port 9877
```

### 3. Create Your First User

```bash
curl -X POST http://localhost:9877/api/admin/create-key \
  -H "Authorization: Bearer your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-account", "credits": 100}'
```

Or just visit `http://localhost:9877` and click **Register**.

### 4. Install the Chrome Extension (Worker)

1. Open `chrome://extensions/`, enable Developer Mode
2. Click "Load unpacked", select the `extension/` directory
3. Click the extension icon, configure:
   - Server URL: `ws://your-server-ip:9877/ws`
   - API Key: your key (optional, for earning credits)
4. Click "Save & Reconnect"
5. **Recommended:** Go to extension details → Enable "Allow in incognito" for cookie isolation

## API Reference

### Crawl a Page

```bash
# POST
curl -X POST http://your-server:9877/api/crawl \
  -H "Authorization: Bearer ak_xxx" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "selector": ".article"}'

# GET (simpler)
curl "http://your-server:9877/api/crawl?url=https://example.com&key=ak_xxx"
```

Response:
```json
{
  "success": true,
  "url": "https://example.com",
  "r2Key": "tasks/xxx.json",
  "downloadUrl": "https://...signed-r2-url..."
}
```

### Check Balance

```bash
curl http://your-server:9877/api/balance \
  -H "Authorization: Bearer ak_xxx"
```

### Platform Status (Public)

```bash
curl http://your-server:9877/api/status
```

### Admin — Create User

```bash
curl -X POST http://your-server:9877/api/admin/create-key \
  -H "Authorization: Bearer admin_key" \
  -H "Content-Type: application/json" \
  -d '{"name": "username", "credits": 100}'
```

### Admin — Recharge Credits

```bash
curl -X POST http://your-server:9877/api/admin/recharge \
  -H "Authorization: Bearer admin_key" \
  -H "Content-Type: application/json" \
  -d '{"apiKey": "ak_xxx", "credits": 50}'
```

## Web Pages

| Path | Description |
|------|-------------|
| `/` | Dashboard + one-click registration |
| `/admin` | Admin panel (requires Admin Key) |
| `/user` | User panel (requires API Key) |

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Server | Python / FastAPI / uvicorn |
| Storage | Cloudflare R2 (S3-compatible, zero egress) |
| Credits | JSON file (data/users.json) |
| Worker | Chrome Extension (Manifest V3) |
| Communication | HTTP REST + WebSocket |

## R2 Free Tier

| Resource | Free/month |
|----------|-----------|
| Storage | 10 GB |
| Write (PUT) | 1M requests |
| Read (GET) | 10M requests |
| Egress | **Unlimited free** |

With 30KB average per task and 1-day auto-expiry, **up to 30,000 tasks/day for free**.

## Security

- **URL Blacklist** — Blocks `localhost`, private IPs (`10.x`, `192.168.x`, `172.16-31.x`), cloud metadata (`169.254.169.254`), dangerous ports (22, 3306, 6379...)
- **Incognito Isolation** — Crawling tabs open in incognito windows, completely isolating Worker's personal cookies and sessions
- **Signed URLs** — R2 upload/download URLs are time-limited (10min upload, 1hr download)
- **Upload Verification** — Server verifies R2 upload via `HEAD` before confirming task completion
- **Heartbeat Detection** — Stale Worker connections are automatically cleaned up after 30s

## License

MIT
