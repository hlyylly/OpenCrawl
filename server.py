import os
import json
import uuid
import time
import asyncio
from pathlib import Path
from urllib.parse import urlparse

import re
import random
import httpx
import boto3
from botocore.config import Config
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# ============ 配置 ============
HTTP_PORT = int(os.getenv("PORT", "9877"))
WS_PORT = HTTP_PORT  # FastAPI 同端口处理 HTTP + WS
TASK_TIMEOUT = 60
MAX_HISTORY = 500
ADMIN_KEY = os.getenv("ADMIN_KEY", "admin_OpenCrawl")
CREDITS_PER_TASK = 1
CREDITS_LITE = 0.1  # lite 模式积分
REGISTER_CREDITS = 100  # 注册赠送积分

# ============ UA 池 ============
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
]

# ============ URL 黑名单 ============
BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.",
    "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
    "192.168.",
    "metadata.google.internal",
    "169.254.169.254",  # 云厂商 metadata
}

BLOCKED_SCHEMES = {"file", "ftp", "javascript", "data"}


def is_url_blocked(url: str) -> str | None:
    """检查 URL 是否被屏蔽，返回原因或 None"""
    try:
        parsed = urlparse(url)
    except Exception:
        return "无效的 URL"

    if parsed.scheme.lower() in BLOCKED_SCHEMES:
        return f"不允许的协议: {parsed.scheme}"

    if not parsed.hostname:
        return "无效的 URL"

    host = parsed.hostname.lower()

    # 精确匹配
    if host in BLOCKED_HOSTS:
        return f"禁止访问: {host}"

    # 前缀匹配（内网 IP 段）
    for prefix in BLOCKED_HOSTS:
        if prefix.endswith(".") and host.startswith(prefix):
            return f"禁止访问内网地址: {host}"

    # 端口检查：常见危险端口
    if parsed.port and parsed.port in {22, 3306, 5432, 6379, 27017, 11211}:
        return f"禁止访问端口: {parsed.port}"

    return None

# ============ Cloudflare R2 ============
r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    region_name="auto",
    config=Config(signature_version="s3v4"),
)
R2_BUCKET = os.getenv("R2_BUCKET", "OpenCrawl")


def get_upload_url(task_id: str):
    key = f"tasks/{task_id}.json"
    url = r2.generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": "application/json"},
        ExpiresIn=600,
    )
    return url, key


def get_download_url(key: str):
    return r2.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key},
        ExpiresIn=3600,
    )


def verify_upload(key: str) -> bool:
    try:
        r2.head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception:
        return False


def setup_lifecycle():
    try:
        r2.put_bucket_lifecycle_configuration(
            Bucket=R2_BUCKET,
            LifecycleConfiguration={
                "Rules": [{
                    "ID": "auto-delete-tasks",
                    "Filter": {"Prefix": "tasks/"},
                    "Status": "Enabled",
                    "Expiration": {"Days": 1},
                }]
            },
        )
        print("[OpenCrawl] R2 lifecycle 规则已设置 (tasks/ 1天过期)")
    except Exception as e:
        print(f"[OpenCrawl] R2 lifecycle 设置失败: {e}")


# ============ Lite 抓取引擎 ============
http_client = httpx.AsyncClient(timeout=15, follow_redirects=True)


def _random_headers():
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _extract_text(html: str, selector: str | None = None) -> str:
    """从 HTML 中提取纯文本"""
    if selector:
        # 简单 CSS 选择器提取（class/id/tag）
        # 对于复杂选择器，lite 模式能力有限
        pass  # 直接走全文提取

    # 去掉 script/style/noscript
    text = re.sub(r'<script[\s\S]*?</script>', '', html, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<noscript[\s\S]*?</noscript>', '', text, flags=re.I)
    text = re.sub(r'<svg[\s\S]*?</svg>', '', text, flags=re.I)
    # 去标签
    text = re.sub(r'<[^>]+>', '\n', text)
    # HTML 实体
    text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&amp;', '&').replace('&quot;', '"')
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    # 清理空白
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def lite_crawl(url: str, selector: str | None = None) -> str | None:
    """轻量抓取：直接 HTTP GET，不经过 Chrome"""
    try:
        resp = await http_client.get(url, headers=_random_headers())
        resp.raise_for_status()
        html = resp.text
        text = _extract_text(html, selector)
        if len(text) > 200:
            return text
        return None  # 内容太少，可能是 JS 渲染页面
    except Exception:
        return None


async def upload_lite_result(url: str, data: str) -> dict:
    """将 lite 结果上传到 R2"""
    task_id = uuid.uuid4().hex
    key = f"tasks/{task_id}.json"
    payload = json.dumps({"url": url, "data": data, "timestamp": time.time()}).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=payload, ContentType="application/json")
    download_url = get_download_url(key)
    return {"r2Key": key, "downloadUrl": download_url}


# ============ Search 引擎 ============
async def search_ddg(query: str, limit: int = 10) -> list:
    """DuckDuckGo HTML 搜索，按时间倒序"""
    params = {
        "q": query,
        "df": "",   # 时间范围留空，靠排序
        "s": "0",
        "o": "json",
    }
    # DDG HTML 版本
    url = f"https://html.duckduckgo.com/html/?q={httpx.URL(query)}&df=&s=0"
    try:
        resp = await http_client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "df": ""},
            headers=_random_headers(),
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        raise HTTPException(502, detail=f"搜索请求失败: {e}")

    return _parse_ddg_results(html, limit)


def _parse_ddg_results(html: str, limit: int) -> list:
    """解析 DDG HTML 搜索结果"""
    results = []
    # 匹配结果块
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.S
    )

    for href, title, snippet in blocks[:limit]:
        # 清理 DDG 的重定向 URL
        actual_url = href
        if "uddg=" in href:
            m = re.search(r'uddg=([^&]+)', href)
            if m:
                from urllib.parse import unquote
                actual_url = unquote(m.group(1))

        # 清理 HTML 标签
        title_clean = re.sub(r'<[^>]+>', '', title).strip()
        snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()

        if title_clean and actual_url:
            results.append({
                "title": title_clean,
                "url": actual_url,
                "snippet": snippet_clean,
            })

    return results


async def search_bing(query: str, limit: int = 10) -> list:
    """Bing 搜索备选"""
    try:
        resp = await http_client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": str(limit)},
            headers=_random_headers(),
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        raise HTTPException(502, detail=f"搜索请求失败: {e}")

    return _parse_bing_results(html, limit)


def _parse_bing_results(html: str, limit: int) -> list:
    """解析 Bing 搜索结果"""
    results = []
    blocks = re.findall(
        r'<li class="b_algo"[^>]*>.*?<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>.*?<p[^>]*>(.*?)</p>',
        html, re.S
    )

    for href, title, snippet in blocks[:limit]:
        title_clean = re.sub(r'<[^>]+>', '', title).strip()
        snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()
        if title_clean and href:
            results.append({
                "title": title_clean,
                "url": href,
                "snippet": snippet_clean,
            })

    return results


# ============ 用户 & 积分 ============
DATA_DIR = Path(__file__).parent / "data"
USERS_FILE = DATA_DIR / "users.json"


def load_users() -> dict:
    try:
        return json.loads(USERS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_users(users: dict):
    DATA_DIR.mkdir(exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False), "utf-8")


# 初始化
DATA_DIR.mkdir(exist_ok=True)
if not USERS_FILE.exists():
    save_users({})


def authenticate(request: Request):
    auth = request.headers.get("authorization", "")
    key = None
    if auth.startswith("Bearer "):
        key = auth[7:]
    if not key:
        key = request.query_params.get("key")
    if not key:
        raise HTTPException(401, detail="缺少 API Key (Authorization: Bearer ak_xxx)")
    users = load_users()
    if key not in users:
        raise HTTPException(401, detail="无效的 API Key")
    return key, users[key]


# ============ 任务 & Worker ============
# task_id -> {url, selector, r2_key, api_key, start_time, future}
tasks: dict = {}
task_history: list = []

# websocket -> {id, api_key, join_time, completed, failed, domains, last_pong}
workers: dict = {}


# ============ FastAPI ============
app = FastAPI(title="OpenCrawl")


# ============ 心跳检测：30秒无响应踢掉 ============
async def heartbeat_checker():
    while True:
        await asyncio.sleep(15)
        now = time.time()
        dead = []
        for ws, info in list(workers.items()):
            # 超过 30 秒没有 pong
            if now - info.get("last_pong", info["join_time"]) > 30:
                dead.append((ws, info))

        for ws, info in dead:
            print(f"[OpenCrawl] Worker {info['id']} 心跳超时，断开")
            workers.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass

        if dead:
            await broadcast_status()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ WebSocket Worker 连接 ============
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    worker_id = uuid.uuid4().hex[:8]
    workers[ws] = {
        "id": worker_id,
        "api_key": None,
        "join_time": time.time(),
        "completed": 0,
        "failed": 0,
        "domains": {},
        "last_pong": time.time(),
    }
    print(f"[OpenCrawl] Worker {worker_id} connected, total: {len(workers)}")
    await broadcast_status()

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "register":
                workers[ws]["api_key"] = msg.get("apiKey")
                print(f"[OpenCrawl] Worker {worker_id} registered, apiKey: {'yes' if msg.get('apiKey') else 'none'}")

            elif msg.get("type") == "taskComplete" and msg.get("taskId") in tasks:
                task_id = msg["taskId"]
                task = tasks.pop(task_id)
                worker = workers.get(ws, {})
                duration = time.time() - task["start_time"]

                entry = {
                    "taskId": task_id,
                    "url": task["url"],
                    "status": "failed" if msg.get("error") else "success",
                    "error": msg.get("error"),
                    "r2Key": task["r2_key"],
                    "workerId": worker.get("id"),
                    "startTime": task["start_time"],
                    "duration": round(duration * 1000),
                    "_apiKey": task["api_key"],
                }

                if msg.get("error"):
                    worker["failed"] = worker.get("failed", 0) + 1
                    task["future"].set_result({"error": msg["error"]})
                else:
                    # 验证 R2 上传
                    exists = verify_upload(task["r2_key"])
                    if not exists:
                        entry["status"] = "failed"
                        entry["error"] = "R2 文件验证失败"
                        worker["failed"] = worker.get("failed", 0) + 1
                        task["future"].set_result({"error": "R2 文件验证失败"})
                    else:
                        # 积分结算
                        users = load_users()
                        if task["api_key"] in users:
                            users[task["api_key"]]["credits"] -= CREDITS_PER_TASK
                            users[task["api_key"]]["totalUsed"] = users[task["api_key"]].get("totalUsed", 0) + 1
                        if worker.get("api_key") and worker["api_key"] in users:
                            users[worker["api_key"]]["credits"] += CREDITS_PER_TASK
                            users[worker["api_key"]]["totalEarned"] = users[worker["api_key"]].get("totalEarned", 0) + 1
                        save_users(users)

                        worker["completed"] = worker.get("completed", 0) + 1
                        download_url = get_download_url(task["r2_key"])
                        task["future"].set_result({"r2Key": task["r2_key"], "downloadUrl": download_url})

                task_history.append(entry)
                if len(task_history) > MAX_HISTORY:
                    task_history.pop(0)
                await broadcast_status()

            elif msg.get("type") == "heartbeat":
                if ws in workers:
                    workers[ws]["last_pong"] = time.time()
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[OpenCrawl] Worker {worker_id} error: {e}")
    finally:
        workers.pop(ws, None)
        print(f"[OpenCrawl] Worker {worker_id} disconnected, total: {len(workers)}")
        await broadcast_status()


def select_worker(target_domain: str):
    best = None
    best_count = float("inf")
    for ws, info in workers.items():
        count = info["domains"].get(target_domain, 0)
        if count < best_count:
            best_count = count
            best = ws
    return best


async def dispatch(ws: WebSocket, task_id, url, selector, upload_url):
    await ws.send_text(json.dumps({
        "type": "task", "taskId": task_id,
        "url": url, "selector": selector, "uploadUrl": upload_url,
    }))
    worker = workers.get(ws)
    if worker:
        domain = urlparse(url).hostname
        worker["domains"][domain] = worker["domains"].get(domain, 0) + 1


async def broadcast_status():
    msg = json.dumps({
        "type": "status",
        "workers": len(workers),
        "activeTasks": [
            {"taskId": tid, "url": t["url"], "startTime": t["start_time"]}
            for tid, t in tasks.items()
        ],
        "totalCompleted": sum(1 for h in task_history if h["status"] == "success"),
        "totalFailed": sum(1 for h in task_history if h["status"] == "failed"),
        "recentHistory": task_history[-20:],
    })
    for ws in list(workers.keys()):
        try:
            await ws.send_text(msg)
        except Exception:
            pass


async def crawl(url: str, selector: str | None, api_key: str):
    # URL 安全检查
    blocked = is_url_blocked(url)
    if blocked:
        raise HTTPException(403, detail=blocked)

    if not workers:
        raise HTTPException(503, detail="没有可用的 Worker")

    domain = urlparse(url).hostname
    ws = select_worker(domain)
    if not ws:
        raise HTTPException(503, detail="没有可用的 Worker")

    task_id = uuid.uuid4().hex
    upload_url, r2_key = get_upload_url(task_id)
    future = asyncio.get_event_loop().create_future()

    tasks[task_id] = {
        "url": url, "selector": selector,
        "r2_key": r2_key, "api_key": api_key,
        "start_time": time.time(), "future": future,
    }

    await dispatch(ws, task_id, url, selector, upload_url)

    try:
        result = await asyncio.wait_for(future, timeout=TASK_TIMEOUT)
    except asyncio.TimeoutError:
        tasks.pop(task_id, None)
        raise HTTPException(504, detail="任务超时")

    if "error" in result:
        raise HTTPException(500, detail=result["error"])

    return result


# ============ HTTP API ============
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(html_path.read_text("utf-8"))


@app.post("/api/crawl")
async def api_crawl_post(request: Request):
    key, user = authenticate(request)
    body = await request.json()
    if not body.get("url"):
        return JSONResponse({"success": False, "error": "缺少 url"}, 400)

    url = body["url"]
    mode = body.get("mode", "auto")  # auto / lite / full
    selector = body.get("selector")

    # URL 安全检查
    blocked = is_url_blocked(url)
    if blocked:
        return JSONResponse({"success": False, "error": blocked}, 403)

    # 积分检查
    cost = CREDITS_LITE if mode == "lite" else CREDITS_PER_TASK
    if mode == "auto":
        cost = CREDITS_LITE  # auto 先尝试 lite
    if user["credits"] < cost:
        return JSONResponse({"success": False, "error": "积分不足", "credits": user["credits"]}, 402)

    # Lite 模式
    if mode in ("lite", "auto"):
        text = await lite_crawl(url, selector)
        if text:
            result = await upload_lite_result(url, text)
            # 扣积分
            users = load_users()
            if key in users:
                users[key]["credits"] -= CREDITS_LITE
                users[key]["totalUsed"] = users[key].get("totalUsed", 0) + 1
                save_users(users)
            return {
                "success": True, "url": url, "mode": "lite",
                "r2Key": result["r2Key"], "downloadUrl": result["downloadUrl"],
                "length": len(text),
            }
        if mode == "lite":
            return JSONResponse({"success": False, "error": "Lite 模式无法获取内容（可能是 JS 渲染页面），请使用 mode=full"}, 422)
        # auto 模式降级到 full
        if user["credits"] < CREDITS_PER_TASK:
            return JSONResponse({"success": False, "error": "Lite 失败需降级 Full 模式，但积分不足", "credits": user["credits"]}, 402)

    # Full 模式
    result = await crawl(url, selector, key)
    return {"success": True, "url": url, "mode": "full", "r2Key": result["r2Key"], "downloadUrl": result["downloadUrl"]}


@app.get("/api/crawl")
async def api_crawl_get(request: Request, url: str = None, selector: str = None, mode: str = "auto"):
    key, user = authenticate(request)
    if not url:
        return JSONResponse({"success": False, "error": "缺少 url"}, 400)

    # 转发到 POST 逻辑
    blocked = is_url_blocked(url)
    if blocked:
        return JSONResponse({"success": False, "error": blocked}, 403)

    cost = CREDITS_LITE if mode == "lite" else CREDITS_PER_TASK
    if mode == "auto":
        cost = CREDITS_LITE
    if user["credits"] < cost:
        return JSONResponse({"success": False, "error": "积分不足", "credits": user["credits"]}, 402)

    if mode in ("lite", "auto"):
        text = await lite_crawl(url, selector)
        if text:
            result = await upload_lite_result(url, text)
            users = load_users()
            if key in users:
                users[key]["credits"] -= CREDITS_LITE
                users[key]["totalUsed"] = users[key].get("totalUsed", 0) + 1
                save_users(users)
            return {
                "success": True, "url": url, "mode": "lite",
                "r2Key": result["r2Key"], "downloadUrl": result["downloadUrl"],
                "length": len(text),
            }
        if mode == "lite":
            return JSONResponse({"success": False, "error": "Lite 模式无法获取内容"}, 422)
        if user["credits"] < CREDITS_PER_TASK:
            return JSONResponse({"success": False, "error": "Lite 失败需降级 Full 模式，但积分不足"}, 402)

    result = await crawl(url, selector, key)
    return {"success": True, "url": url, "mode": "full", "r2Key": result["r2Key"], "downloadUrl": result["downloadUrl"]}


# ============ Search API ============
@app.post("/api/search")
async def api_search_post(request: Request):
    key, user = authenticate(request)
    if user["credits"] < CREDITS_LITE:
        return JSONResponse({"success": False, "error": "积分不足", "credits": user["credits"]}, 402)

    body = await request.json()
    q = body.get("q", "").strip()
    if not q:
        return JSONResponse({"success": False, "error": "缺少搜索词 q"}, 400)

    limit = min(body.get("limit", 10), 20)
    engine = body.get("engine", "duckduckgo")

    # 搜索
    if engine == "bing":
        results = await search_bing(q, limit)
    else:
        results = await search_ddg(q, limit)
        if not results:
            # DDG 失败，降级到 Bing
            results = await search_bing(q, limit)

    # 上传到 R2
    task_id = uuid.uuid4().hex
    r2_key = f"tasks/{task_id}.json"
    payload = json.dumps({
        "query": q, "engine": engine, "results": results,
        "count": len(results), "timestamp": time.time(),
    }, ensure_ascii=False).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=r2_key, Body=payload, ContentType="application/json")
    download_url = get_download_url(r2_key)

    # 扣积分
    users = load_users()
    if key in users:
        users[key]["credits"] -= CREDITS_LITE
        users[key]["totalUsed"] = users[key].get("totalUsed", 0) + 1
        save_users(users)

    return {
        "success": True, "query": q, "engine": engine,
        "count": len(results), "results": results,
        "r2Key": r2_key, "downloadUrl": download_url,
    }


@app.get("/api/search")
async def api_search_get(request: Request, q: str = None, limit: int = 10, engine: str = "duckduckgo"):
    key, user = authenticate(request)
    if user["credits"] < CREDITS_LITE:
        return JSONResponse({"success": False, "error": "积分不足", "credits": user["credits"]}, 402)
    if not q:
        return JSONResponse({"success": False, "error": "缺少搜索词 q"}, 400)

    limit = min(limit, 20)

    if engine == "bing":
        results = await search_bing(q, limit)
    else:
        results = await search_ddg(q, limit)
        if not results:
            results = await search_bing(q, limit)

    task_id = uuid.uuid4().hex
    r2_key = f"tasks/{task_id}.json"
    payload = json.dumps({
        "query": q, "engine": engine, "results": results,
        "count": len(results), "timestamp": time.time(),
    }, ensure_ascii=False).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=r2_key, Body=payload, ContentType="application/json")
    download_url = get_download_url(r2_key)

    users = load_users()
    if key in users:
        users[key]["credits"] -= CREDITS_LITE
        users[key]["totalUsed"] = users[key].get("totalUsed", 0) + 1
        save_users(users)

    return {
        "success": True, "query": q, "engine": engine,
        "count": len(results), "results": results,
        "r2Key": r2_key, "downloadUrl": download_url,
    }


@app.get("/api/balance")
async def api_balance(request: Request):
    key, user = authenticate(request)
    return {
        "success": True, "name": user.get("name"), "credits": user["credits"],
        "totalUsed": user.get("totalUsed", 0), "totalEarned": user.get("totalEarned", 0),
    }


@app.get("/api/status")
async def api_status():
    worker_list = []
    for info in workers.values():
        worker_list.append({
            "id": info["id"],
            "completed": info["completed"],
            "failed": info["failed"],
            "uptime": round(time.time() - info["join_time"]),
        })
    return {
        "workers": len(workers),
        "workerList": worker_list,
        "activeTasks": len(tasks),
        "totalCompleted": sum(1 for h in task_history if h["status"] == "success"),
        "totalFailed": sum(1 for h in task_history if h["status"] == "failed"),
        "history": task_history[-50:],
    }


# ============ 页面路由 ============
@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    html_path = Path(__file__).parent / "admin.html"
    return HTMLResponse(html_path.read_text("utf-8"))


@app.get("/user", response_class=HTMLResponse)
async def user_page():
    html_path = Path(__file__).parent / "user.html"
    return HTMLResponse(html_path.read_text("utf-8"))


# ============ 管理员 API ============
@app.get("/api/admin/users")
async def admin_users(request: Request):
    key = request.headers.get("authorization", "").replace("Bearer ", "") or request.query_params.get("key")
    if key != ADMIN_KEY:
        raise HTTPException(403, detail="无管理员权限")

    users = load_users()
    user_list = []
    total_credits = 0
    total_used = 0
    total_earned = 0
    for ak, u in users.items():
        user_list.append({
            "apiKey": ak,
            "name": u.get("name", ""),
            "credits": u.get("credits", 0),
            "totalUsed": u.get("totalUsed", 0),
            "totalEarned": u.get("totalEarned", 0),
            "created": u.get("created", ""),
        })
        total_credits += u.get("credits", 0)
        total_used += u.get("totalUsed", 0)
        total_earned += u.get("totalEarned", 0)

    return {
        "users": user_list,
        "stats": {
            "totalUsers": len(users),
            "totalCredits": total_credits,
            "totalUsed": total_used,
            "totalEarned": total_earned,
        },
    }


# ============ 用户 API ============
@app.get("/api/user/history")
async def user_history(request: Request):
    key, user = authenticate(request)
    # 返回该用户相关的任务历史
    my_history = [h for h in task_history if h.get("_apiKey") == key]
    return {"history": my_history[-50:]}


# ============ 公开注册 ============
@app.post("/api/register")
async def api_register(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"success": False, "error": "请输入名称"}, 400)
    if len(name) > 32:
        return JSONResponse({"success": False, "error": "名称最长 32 字符"}, 400)

    new_key = "ak_" + uuid.uuid4().hex[:24]
    users = load_users()
    users[new_key] = {
        "name": name,
        "credits": REGISTER_CREDITS,
        "created": time.strftime("%Y-%m-%d"),
        "totalUsed": 0,
        "totalEarned": 0,
    }
    save_users(users)
    print(f"[OpenCrawl] 新用户注册: {name} -> {new_key}")
    return {"success": True, "apiKey": new_key, "credits": REGISTER_CREDITS}


@app.post("/api/admin/create-key")
async def admin_create_key(request: Request):
    key = request.headers.get("authorization", "").replace("Bearer ", "") or request.query_params.get("key")
    if key != ADMIN_KEY:
        raise HTTPException(403, detail="无管理员权限")

    body = await request.json()
    new_key = "ak_" + uuid.uuid4().hex[:24]
    users = load_users()
    users[new_key] = {
        "name": body.get("name", "未命名"),
        "credits": body.get("credits", 100),
        "created": time.strftime("%Y-%m-%d"),
        "totalUsed": 0,
        "totalEarned": 0,
    }
    save_users(users)
    return {"success": True, "apiKey": new_key, "credits": users[new_key]["credits"]}


@app.post("/api/admin/recharge")
async def admin_recharge(request: Request):
    key = request.headers.get("authorization", "").replace("Bearer ", "") or request.query_params.get("key")
    if key != ADMIN_KEY:
        raise HTTPException(403, detail="无管理员权限")

    body = await request.json()
    api_key = body.get("apiKey")
    credits = body.get("credits")
    if not api_key or not isinstance(credits, (int, float)):
        return JSONResponse({"success": False, "error": "需要 apiKey 和 credits"}, 400)

    users = load_users()
    if api_key not in users:
        users[api_key] = {
            "name": body.get("name", "未命名"),
            "credits": credits,
            "created": time.strftime("%Y-%m-%d"),
            "totalUsed": 0,
            "totalEarned": 0,
        }
    else:
        users[api_key]["credits"] += credits
        if body.get("name"):
            users[api_key]["name"] = body["name"]
    save_users(users)
    return {"success": True, "apiKey": api_key, "credits": users[api_key]["credits"]}


# ============ 启动 ============
@app.on_event("startup")
async def startup():
    print(f"[OpenCrawl] API & Dashboard: http://0.0.0.0:{HTTP_PORT}")
    print(f"[OpenCrawl] WebSocket: ws://0.0.0.0:{HTTP_PORT}/ws")
    print(f"[OpenCrawl] R2 Bucket: {R2_BUCKET}")
    print()
    print("[OpenCrawl] API (需要认证):")
    print("[OpenCrawl]   POST /api/crawl       {url, selector?}")
    print("[OpenCrawl]   GET  /api/balance      查询积分")
    print()
    print("[OpenCrawl] API (公开):")
    print("[OpenCrawl]   GET  /api/status       平台状态")
    print()
    print("[OpenCrawl] 管理员:")
    print("[OpenCrawl]   POST /api/admin/create-key  创建 API Key")
    print("[OpenCrawl]   POST /api/admin/recharge    充值积分")
    setup_lifecycle()
    asyncio.create_task(heartbeat_checker())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
