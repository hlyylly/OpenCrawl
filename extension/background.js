const TASK_TIMEOUT = 45000;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];

let wsUrl = "ws://localhost:9877/ws";
let apiKey = null;
let ws = null;
let reconnectAttempt = 0;
let connected = false;

// taskId -> { tabId, url, selector, uploadUrl, timer }
const activeTasks = new Map();
let stats = { completed: 0, failed: 0, credits: 0 };

// ============ 配置管理 ============
async function loadConfig() {
  const cfg = await chrome.storage.local.get(["wsUrl", "apiKey"]);
  if (cfg.wsUrl) wsUrl = cfg.wsUrl;
  if (cfg.apiKey) apiKey = cfg.apiKey;
}

async function saveConfig(newCfg) {
  if (newCfg.wsUrl) wsUrl = newCfg.wsUrl;
  if (newCfg.apiKey !== undefined) apiKey = newCfg.apiKey;
  await chrome.storage.local.set({ wsUrl, apiKey });
}

// ============ WebSocket ============
function connect() {
  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    reconnectAttempt = 0;
    connected = true;
    console.log("[OpenCrawl] 已连接到服务端");
    addLog("success", `已连接 ${wsUrl}`);

    // 注册 Worker（发送 API Key 以获取积分）
    if (apiKey) {
      ws.send(JSON.stringify({ type: "register", apiKey }));
    }
    broadcastState();
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "task") {
        handleTask(msg);
      }
    } catch (e) {
      console.error("[OpenCrawl] 消息解析错误:", e);
    }
  };

  ws.onclose = () => {
    connected = false;
    console.log("[OpenCrawl] 连接断开");
    broadcastState();
    scheduleReconnect();
  };

  ws.onerror = () => {};
}

function scheduleReconnect() {
  const delay = RECONNECT_DELAYS[Math.min(reconnectAttempt, RECONNECT_DELAYS.length - 1)];
  reconnectAttempt++;
  setTimeout(connect, delay);
}

// 重连（配置更新后调用）
function reconnect() {
  if (ws) {
    ws.onclose = null;
    ws.close();
  }
  reconnectAttempt = 0;
  connect();
}

setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "heartbeat" }));
  }
}, 10000);

// ============ 任务处理 ============
async function handleTask(task) {
  const { taskId, url, selector, uploadUrl } = task;
  console.log(`[OpenCrawl] 收到任务 [${taskId.slice(0, 8)}] -> ${url}`);
  addLog("info", `收到任务 -> ${url}`);

  try {
    const tab = await chrome.tabs.create({ url, active: false });

    const timer = setTimeout(() => {
      finishTask(taskId, null, "渲染超时");
    }, TASK_TIMEOUT);

    activeTasks.set(taskId, { tabId: tab.id, url, selector, uploadUrl, timer });
    broadcastState();
  } catch (e) {
    reportComplete(taskId, "打开标签页失败: " + e.message);
    addLog("error", `打开标签页失败: ${e.message}`);
  }
}

// Content script 返回提取结果后，上传 R2
async function finishTask(taskId, data, error) {
  const task = activeTasks.get(taskId);
  if (!task) return;

  clearTimeout(task.timer);
  activeTasks.delete(taskId);

  // 关闭标签页
  try { chrome.tabs.remove(task.tabId); } catch (e) {}

  if (error) {
    reportComplete(taskId, error);
    stats.failed++;
    addLog("error", `[${taskId.slice(0, 8)}] ${error}`);
  } else {
    // 上传结果到 R2（带 1 次重试）
    const body = JSON.stringify({
      url: task.url,
      data,
      length: data.length,
      timestamp: Date.now(),
    });

    let uploaded = false;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const response = await fetch(task.uploadUrl, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        uploaded = true;
        break;
      } catch (e) {
        if (attempt === 0) {
          addLog("warn", `[${taskId.slice(0, 8)}] 上传失败，重试...`);
        }
      }
    }

    if (uploaded) {
      reportComplete(taskId, null);
      stats.completed++;
      stats.credits++;
      addLog("success", `[${taskId.slice(0, 8)}] 完成，${data.length} 字符`);
    } else {
      reportComplete(taskId, "R2 上传失败");
      stats.failed++;
      addLog("error", `[${taskId.slice(0, 8)}] R2 上传失败`);
    }
  }

  broadcastState();
}

// 通知服务端任务完成
function reportComplete(taskId, error) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = { type: "taskComplete", taskId };
    if (error) msg.error = error;
    ws.send(JSON.stringify(msg));
  }
}

// ============ Content Script 通信 ============
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "getTask") {
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse(null); return; }
    for (const [taskId, task] of activeTasks) {
      if (task.tabId === tabId) {
        sendResponse({ taskId, selector: task.selector });
        return;
      }
    }
    sendResponse(null);
    return;
  }

  if (msg.type === "taskResult") {
    finishTask(msg.taskId, msg.data, msg.error);
    return;
  }

  if (msg.type === "getState") {
    sendResponse(getState());
    return;
  }

  if (msg.type === "getLogs") {
    sendResponse(logs);
    return;
  }

  // 来自 popup 的配置操作
  if (msg.type === "getConfig") {
    sendResponse({ wsUrl, apiKey });
    return;
  }

  if (msg.type === "saveConfig") {
    saveConfig(msg.config).then(() => {
      reconnect();
      sendResponse({ ok: true });
    });
    return true; // async
  }
});

// ============ 状态 & 日志 ============
const logs = [];
const MAX_LOGS = 100;

function addLog(level, message) {
  logs.push({ level, message, time: Date.now() });
  if (logs.length > MAX_LOGS) logs.shift();
  broadcastState();
}

function getState() {
  return {
    connected,
    activeTasks: activeTasks.size,
    completed: stats.completed,
    failed: stats.failed,
    credits: stats.credits,
  };
}

function broadcastState() {
  chrome.runtime.sendMessage({ type: "stateUpdate", state: getState() }).catch(() => {});
}

// ============ 启动 ============
console.log("[OpenCrawl] Background service worker started");
loadConfig().then(connect);
