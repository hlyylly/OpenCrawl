const TASK_TIMEOUT = 45000;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];

let wsUrl = "";
let apiKey = null;
let ws = null;
let reconnectAttempt = 0;
let connected = false;
let incognitoAllowed = false;
let workerId = null;

// taskId -> { tabId, url, selector, uploadUrl, timer }
const activeTasks = new Map();
let stats = { completed: 0, failed: 0, credits: 0 };
let incognitoWindowId = null; // 复用的无痕窗口

// ============ 配置管理 ============
async function loadConfig() {
  const cfg = await chrome.storage.local.get(["wsUrl", "apiKey", "workerId"]);
  wsUrl = cfg.wsUrl || "ws://localhost:9878/ws";
  if (cfg.apiKey) apiKey = cfg.apiKey;

  // 持久化 Worker ID，Service Worker 重启后保持同一个 ID
  if (!cfg.workerId) {
    const id = "w_" + Math.random().toString(36).slice(2, 10);
    await chrome.storage.local.set({ workerId: id });
    cfg.workerId = id;
  }
  workerId = cfg.workerId;

  // 检查是否允许无痕模式
  try {
    const ext = await chrome.management.getSelf();
    incognitoAllowed = ext.enabled && (await chrome.extension.isAllowedIncognitoAccess());
  } catch {
    incognitoAllowed = false;
  }
}

async function saveConfig(newCfg) {
  if (newCfg.wsUrl) wsUrl = newCfg.wsUrl;
  if (newCfg.apiKey !== undefined) apiKey = newCfg.apiKey;
  await chrome.storage.local.set({ wsUrl, apiKey });
}

// ============ 无痕窗口管理 ============
let _incognitoPromise = null;

async function getIncognitoWindow() {
  // 1. 检查内存中的窗口 ID
  if (incognitoWindowId) {
    try {
      await chrome.windows.get(incognitoWindowId);
      return incognitoWindowId;
    } catch {
      incognitoWindowId = null;
    }
  }

  // 2. Service Worker 重启后内存丢失，查找已有的无痕窗口复用
  try {
    const allWindows = await chrome.windows.getAll({ windowTypes: ["normal"] });
    const existing = allWindows.find(w => w.incognito);
    if (existing) {
      incognitoWindowId = existing.id;
      return incognitoWindowId;
    }
  } catch {}

  // 3. 并发锁：多个任务同时请求时只创建一个窗口
  if (_incognitoPromise) {
    return _incognitoPromise;
  }

  _incognitoPromise = (async () => {
    const win = await chrome.windows.create({
      incognito: true,
      focused: false,
      state: "minimized",
      url: "about:blank",
    });
    incognitoWindowId = win.id;
    _incognitoPromise = null;
    return incognitoWindowId;
  })();

  return _incognitoPromise;
}

// ============ WebSocket ============
function connect() {
  // 配置未加载完，等一下
  if (!wsUrl) {
    setTimeout(connect, 500);
    return;
  }

  // 验证 URL 格式
  if (!wsUrl.startsWith("ws://") && !wsUrl.startsWith("wss://")) {
    addLog("error", `无效的 WebSocket 地址: ${wsUrl}`);
    scheduleReconnect();
    return;
  }

  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    addLog("error", `连接失败: ${e.message}`);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    reconnectAttempt = 0;
    connected = true;
    console.log("[OpenCrawl] 已连接到服务端");
    addLog("success", `已连接 ${wsUrl}`);

    if (!incognitoAllowed) {
      addLog("warn", "未启用无痕模式权限，爬取将携带你的 Cookie（建议在扩展设置中开启「在无痕模式下启用」）");
    }

    // 注册 Worker（发送 ID + 版本 + API Key）
    const manifest = chrome.runtime.getManifest();
    ws.send(JSON.stringify({ type: "register", workerId, version: manifest.version, apiKey: apiKey || null }));
    broadcastState();
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "task") {
        handleTask(msg);
      } else if (msg.type === "update_required") {
        addLog("error", `版本过低 (${msg.current})，需要 ${msg.required}+，请更新扩展`);
        connected = false;
        ws.onclose = null; // 不触发重连
        broadcastState();
        ws.close();
      }
    } catch (e) {
      console.error("[OpenCrawl] 消息解析错误:", e);
    }
  };

  ws.onclose = () => {
    connected = false;
    broadcastState();
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose 会紧跟着触发，不需要在这里处理
  };
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
  ws = null;
  connected = false;
  reconnectAttempt = 0;
  connect();
}

// 用 chrome.alarms 发心跳（Service Worker 休眠后 setInterval 会停，alarm 不会）
chrome.alarms.create("heartbeat", { periodInMinutes: 0.25 }); // 每 15 秒
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "heartbeat") {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "heartbeat" }));
    }
  }
});

// ============ Lite 模式资源屏蔽 ============
const LITE_RULE_ID_BASE = 100000;
let liteRuleCounter = 0;

async function addLiteRules(tabId) {
  const ruleId = LITE_RULE_ID_BASE + (liteRuleCounter++ % 50000);
  const rules = [
    {
      id: ruleId,
      priority: 1,
      action: { type: "block" },
      condition: {
        tabIds: [tabId],
        resourceTypes: ["image", "font", "media", "stylesheet"],
      },
    },
  ];
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      addRules: rules,
      removeRuleIds: [ruleId],
    });
  } catch (e) {
    console.warn("[OpenCrawl] Failed to set lite rules:", e);
  }
  return ruleId;
}

async function removeLiteRules(ruleId) {
  if (!ruleId) return;
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: [ruleId],
    });
  } catch (e) {}
}

// ============ 任务处理 ============
async function handleTask(task) {
  const { taskId, url, selector, uploadUrl, mode } = task;
  const isLite = mode === "lite";
  const timeout = isLite ? 15000 : TASK_TIMEOUT;

  console.log(`[OpenCrawl] 收到任务 [${taskId.slice(0, 8)}] ${isLite ? "(lite)" : ""} -> ${url}`);
  addLog("info", `${isLite ? "[lite] " : ""}收到任务 -> ${url}`);

  try {
    let tabId;
    let liteRuleId = null;

    if (incognitoAllowed) {
      const winId = await getIncognitoWindow();
      const tab = await chrome.tabs.create({ windowId: winId, url: "about:blank", active: false });
      tabId = tab.id;
    } else {
      const tab = await chrome.tabs.create({ url: "about:blank", active: false });
      tabId = tab.id;
    }

    // Lite 模式：屏蔽图片/字体/媒体/CSS
    if (isLite) {
      liteRuleId = await addLiteRules(tabId);
    }

    // 导航到目标 URL
    await chrome.tabs.update(tabId, { url });

    const timer = setTimeout(() => {
      finishTask(taskId, null, "渲染超时");
    }, timeout);

    activeTasks.set(taskId, { tabId, url, selector, uploadUrl, timer, liteRuleId, mode });
    broadcastState();
  } catch (e) {
    reportComplete(taskId, "打开标签页失败: " + e.message);
    addLog("error", `打开标签页失败: ${e.message}`);
  }
}

// Content script 返回提取结果后，上传 R2 或直接回传（本地模式）
async function finishTask(taskId, data, error) {
  const task = activeTasks.get(taskId);
  if (!task) return;

  clearTimeout(task.timer);
  activeTasks.delete(taskId);

  // 清理 lite 规则 + 关闭标签页
  await removeLiteRules(task.liteRuleId);
  try {
    await chrome.tabs.remove(task.tabId);
  } catch (e) {}

  if (error) {
    reportComplete(taskId, error);
    stats.failed++;
    addLog("error", `[${taskId.slice(0, 8)}] ${error}`);
  } else if (!task.uploadUrl) {
    // 本地模式：无 uploadUrl，数据直接通过 WebSocket 回传
    reportComplete(taskId, null, data);
    stats.completed++;
    addLog("success", `[${taskId.slice(0, 8)}] 完成 (本地)，${data.length} 字符`);
  } else {
    // 云端模式：上传结果到 R2（带 1 次重试）
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

// 通知服务端任务完成（本地模式时附带 data）
function reportComplete(taskId, error, data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = { type: "taskComplete", taskId };
    if (error) msg.error = error;
    if (data) msg.data = data;
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
        sendResponse({ taskId, selector: task.selector, mode: task.mode || "full" });
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
    sendResponse({ wsUrl, apiKey, incognitoAllowed });
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
    incognitoAllowed,
  };
}

function broadcastState() {
  chrome.runtime.sendMessage({ type: "stateUpdate", state: getState() }).catch(() => {});
}

// ============ 启动 ============
console.log("[OpenCrawl] Background service worker started");
loadConfig().then(connect);
