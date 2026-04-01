"""
OpenCrawl Local — 本地轻量版，无需 R2/积分/认证
直接局域网内通信，结果通过 WebSocket 直传
"""
import json
import uuid
import time
import asyncio
from pathlib import Path
from urllib.parse import urlparse, quote_plus

import re
import random
import httpx
from urllib.parse import parse_qs
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware


async def safe_parse_body(request: Request) -> dict:
    """兼容各种编码和格式的请求体解析
    - JSON (UTF-8 / GBK / Latin-1)
    - x-www-form-urlencoded
    - 纯文本 JSON
    """
    raw = await request.body()
    if not raw:
        return {}

    content_type = request.headers.get("content-type", "")

    # 1. 尝试 form-urlencoded
    if "x-www-form-urlencoded" in content_type:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        parsed = parse_qs(text)
        return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

    # 2. 尝试多种编码解析 JSON
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"):
        try:
            text = raw.decode(encoding)
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

    # 3. 最后兜底：强制 latin-1 解码后尝试
    try:
        return json.loads(raw.decode("latin-1", errors="replace"))
    except Exception:
        return {}

# ============ 配置 ============
HTTP_PORT = 9878  # 本地版用不同端口，避免和云端冲突
TASK_TIMEOUT = 60
MAX_HISTORY = 500
MIN_WORKER_VERSION = "1.2.0"

def version_gte(v: str, min_v: str) -> bool:
    try:
        return tuple(int(x) for x in v.split(".")) >= tuple(int(x) for x in min_v.split("."))
    except Exception:
        return False

# ============ UA 池 ============
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

# ============ Search 引擎 ============
SEARCH_ENGINES = {
    "duckduckgo": "https://html.duckduckgo.com/html/?q={}",
    "bing": "https://www.bing.com/search?q={}&setlang=en&cc=us",
    "google": "https://www.google.com/search?q={}",
    "baidu": "https://www.baidu.com/s?wd={}",
}

http_client = httpx.AsyncClient(timeout=15, follow_redirects=True)

def _random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "DNT": "1",
    }

def _build_search_url(q: str, engine: str) -> str:
    template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["duckduckgo"])
    return template.format(quote_plus(q))


# ============ 任务 & Worker ============
# task_id -> {url, selector, start_time, future, mode}
tasks: dict = {}
task_history: list = []

# websocket -> {id, join_time, completed, failed, domains, last_pong, active_tasks}
workers: dict = {}


# ============ FastAPI ============
app = FastAPI(title="OpenCrawl Local")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ 心跳检测 ============
async def heartbeat_checker():
    while True:
        await asyncio.sleep(15)
        now = time.time()
        dead = []
        for ws, info in list(workers.items()):
            if now - info.get("last_pong", info["join_time"]) > 120:
                dead.append((ws, info))
        for ws, info in dead:
            print(f"[Local] Worker {info['id']} 心跳超时，断开")
            workers.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass
        if dead:
            await broadcast_status()


# ============ WebSocket Worker 连接 ============
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    worker_id = uuid.uuid4().hex[:8]
    workers[ws] = {
        "id": worker_id,
        "join_time": time.time(),
        "completed": 0,
        "failed": 0,
        "active_tasks": 0,
        "domains": {},
        "last_pong": time.time(),
    }
    print(f"[Local] Worker {worker_id} connected, total: {len(workers)}")
    await broadcast_status()

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "register":
                client_version = msg.get("version", "0.0.0")
                if not version_gte(client_version, MIN_WORKER_VERSION):
                    await ws.send_text(json.dumps({
                        "type": "update_required",
                        "current": client_version,
                        "required": MIN_WORKER_VERSION,
                    }))
                    workers.pop(ws, None)
                    await broadcast_status()
                    continue

                workers[ws]["version"] = client_version
                client_worker_id = msg.get("workerId")
                if client_worker_id:
                    workers[ws]["client_id"] = client_worker_id
                    for old_ws, old_info in list(workers.items()):
                        if old_ws is not ws and old_info.get("client_id") == client_worker_id:
                            print(f"[Local] 移除幽灵 Worker {old_info['id']}")
                            workers.pop(old_ws, None)
                            # 不主动 close 旧连接，避免触发扩展端连锁重连
                            # 旧连接会在心跳超时后自然断开
                print(f"[Local] Worker {worker_id} registered")

            elif msg.get("type") == "taskComplete" and msg.get("taskId") in tasks:
                task_id = msg["taskId"]
                task = tasks.pop(task_id)
                worker = workers.get(ws, {})
                if worker:
                    worker["active_tasks"] = max(0, worker.get("active_tasks", 0) - 1)
                duration = time.time() - task["start_time"]

                entry = {
                    "taskId": task_id,
                    "url": task["url"],
                    "status": "failed" if msg.get("error") else "success",
                    "error": msg.get("error"),
                    "workerId": worker.get("id"),
                    "startTime": task["start_time"],
                    "duration": round(duration * 1000),
                }

                if msg.get("error"):
                    worker["failed"] = worker.get("failed", 0) + 1
                    task["future"].set_result({"error": msg["error"]})
                else:
                    # 本地版：直接从 WebSocket 消息获取数据
                    result_data = msg.get("data")
                    if result_data:
                        worker["completed"] = worker.get("completed", 0) + 1
                        task["future"].set_result({"data": result_data})
                    else:
                        entry["status"] = "failed"
                        entry["error"] = "Worker 未返回数据"
                        worker["failed"] = worker.get("failed", 0) + 1
                        task["future"].set_result({"error": "Worker 未返回数据"})

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
        print(f"[Local] Worker {worker_id} error: {e}")
    finally:
        workers.pop(ws, None)
        print(f"[Local] Worker {worker_id} disconnected, total: {len(workers)}")
        await broadcast_status()


def select_worker(target_domain: str):
    best = None
    best_score = float("inf")
    for ws, info in workers.items():
        active = info.get("active_tasks", 0)
        domain_count = info["domains"].get(target_domain, 0)
        score = active * 100 + domain_count
        if score < best_score:
            best_score = score
            best = ws
    return best


async def dispatch(ws: WebSocket, task_id, url, selector, mode="full"):
    worker = workers.get(ws)
    if worker:
        worker["active_tasks"] = worker.get("active_tasks", 0) + 1

    # 本地版：不发 uploadUrl，Worker 会直接通过 WS 回传数据
    await ws.send_text(json.dumps({
        "type": "task", "taskId": task_id,
        "url": url, "selector": selector, "mode": mode,
    }))
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


async def crawl(url: str, selector: str = None, mode: str = "full"):
    if not workers:
        raise HTTPException(503, detail="没有可用的 Worker")

    domain = urlparse(url).hostname
    ws = select_worker(domain)
    if not ws:
        raise HTTPException(503, detail="没有可用的 Worker")

    task_id = uuid.uuid4().hex
    future = asyncio.get_event_loop().create_future()
    timeout = 30 if mode == "lite" else TASK_TIMEOUT

    tasks[task_id] = {
        "url": url, "selector": selector,
        "start_time": time.time(), "future": future,
        "mode": mode,
    }

    await dispatch(ws, task_id, url, selector, mode)

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        tasks.pop(task_id, None)
        raise HTTPException(504, detail="任务超时")

    if "error" in result:
        raise HTTPException(500, detail=result["error"])

    return result


# ============ HTTP API ============
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse("""
    <html><head><meta charset="UTF-8"><title>OpenCrawl Local</title>
    <style>body{background:#0d1117;color:#e6edf3;font-family:system-ui;display:flex;justify-content:center;align-items:center;min-height:100vh}
    .c{text-align:center}h1{font-size:2em}h1 span{color:#58a6ff}.sub{color:#8b949e;margin-top:8px}
    </style></head>
    <body><div class="c"><h1><span>Open</span>Crawl Local</h1><p class="sub">本地轻量版运行中</p></div></body></html>
    """)


@app.post("/api/crawl")
async def api_crawl_post(request: Request):
    body = await safe_parse_body(request)
    if not body.get("url"):
        return JSONResponse({"success": False, "error": "缺少 url"}, 400)

    url = body["url"]
    mode = body.get("mode", "full")
    selector = body.get("selector")

    result = await crawl(url, selector, mode)
    # 直接返回数据
    data = result["data"]
    # 尝试解析 JSON
    try:
        parsed = json.loads(data)
        return {"success": True, "url": url, "mode": mode, "data": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"success": True, "url": url, "mode": mode, "data": data}


@app.get("/api/crawl")
async def api_crawl_get(request: Request, url: str = None, selector: str = None, mode: str = "full"):
    if not url:
        return JSONResponse({"success": False, "error": "缺少 url"}, 400)

    result = await crawl(url, selector, mode)
    data = result["data"]
    try:
        parsed = json.loads(data)
        return {"success": True, "url": url, "mode": mode, "data": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"success": True, "url": url, "mode": mode, "data": data}


# ============ Search API ============
def _merge_results(all_results, sources) -> list:
    seen_urls = set()
    merged = []
    for results, source in zip(all_results, sources):
        for r in results:
            url = r.get("url", "")
            norm = url.rstrip("/").lower().split("?")[0]
            if norm in seen_urls or not url:
                continue
            seen_urls.add(norm)
            r["source"] = source
            merged.append(r)
    return merged


async def _do_search(q: str, mode: str):
    if mode == "full":
        engines = ["duckduckgo", "bing", "google", "baidu"]
        coros = []
        for eng in engines:
            url = _build_search_url(q, eng)
            coros.append(crawl(url, "__search__", "full"))

        try:
            results_raw = await asyncio.wait_for(
                asyncio.gather(*coros, return_exceptions=True),
                timeout=45
            )
        except asyncio.TimeoutError:
            results_raw = [TimeoutError("总超时")] * len(engines)

        all_results = []
        for i, r in enumerate(results_raw):
            if isinstance(r, (Exception, BaseException)):
                print(f"[Local] Search {engines[i]} failed: {type(r).__name__}: {r}")
                all_results.append([])
            else:
                data_str = r.get("data", "")
                try:
                    items = json.loads(data_str) if isinstance(data_str, str) and data_str.startswith("[") else (data_str if isinstance(data_str, list) else [])
                except Exception:
                    items = []
                print(f"[Local] Search {engines[i]}: {len(items)} results")
                all_results.append(items)

        merged = _merge_results(all_results, engines)
        return merged, engines
    else:
        url = _build_search_url(q, "duckduckgo")
        result = await crawl(url, "__search__", "full")
        data_str = result.get("data", "")
        try:
            items = json.loads(data_str) if isinstance(data_str, str) and data_str.startswith("[") else (data_str if isinstance(data_str, list) else [])
        except Exception:
            items = []
        return items, ["duckduckgo"]


@app.post("/api/search")
async def api_search_post(request: Request):
    body = await safe_parse_body(request)
    q = body.get("q", "").strip()
    if not q:
        return JSONResponse({"success": False, "error": "缺少搜索词 q"}, 400)

    mode = body.get("mode", "lite")
    results, engines = await _do_search(q, mode)

    return {
        "success": True,
        "query": q,
        "type": "search",
        "mode": mode,
        "engines": engines,
        "web": {"results": results[:30]},
    }


@app.get("/api/search")
async def api_search_get(request: Request, q: str = None, mode: str = "lite"):
    if not q:
        return JSONResponse({"success": False, "error": "缺少搜索词 q"}, 400)

    results, engines = await _do_search(q, mode)

    return {
        "success": True,
        "query": q,
        "type": "search",
        "mode": mode,
        "engines": engines,
        "web": {"results": results[:30]},
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


# ============ 启动 ============
@app.on_event("startup")
async def startup():
    print(f"[OpenCrawl Local] 本地版运行中")
    print(f"[OpenCrawl Local] API: http://0.0.0.0:{HTTP_PORT}")
    print(f"[OpenCrawl Local] WebSocket: ws://0.0.0.0:{HTTP_PORT}/ws")
    print()
    print("[OpenCrawl Local] API (无需认证):")
    print("  POST /api/crawl    {{url, selector?, mode?}}")
    print("  POST /api/search   {{q, mode?}}")
    print("  GET  /api/status   平台状态")
    asyncio.create_task(heartbeat_checker())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
