let currentMode = "cloud"; // cloud | local
const DEFAULT_CLOUD_URL = "";
const DEFAULT_LOCAL_URL = "ws://localhost:9878/ws";

function updateUI(state) {
  const badge = document.getElementById("badge");
  badge.textContent = state.connected ? "已连接" : "未连接";
  badge.className = "badge " + (state.connected ? "on" : "off");

  document.getElementById("active").textContent = state.activeTasks || 0;
  document.getElementById("completed").textContent = state.completed || 0;
  document.getElementById("failed").textContent = state.failed || 0;
  document.getElementById("credits").textContent = state.credits || 0;
}

function renderLogs(logs) {
  const container = document.getElementById("logs");
  if (!logs || logs.length === 0) {
    container.innerHTML = '<div class="empty">等待任务...</div>';
    return;
  }

  container.innerHTML = [...logs]
    .reverse()
    .slice(0, 50)
    .map((l) => {
      const t = new Date(l.time).toLocaleTimeString();
      return `<div class="log ${l.level}"><span class="time">${t}</span><span class="msg">${escapeHtml(l.message)}</span></div>`;
    })
    .join("");
}

function escapeHtml(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function updateModeUI(mode) {
  currentMode = mode;
  const cloudBtn = document.getElementById("modeCloud");
  const localBtn = document.getElementById("modeLocal");

  cloudBtn.className = "toggle-btn" + (mode === "cloud" ? " active" : "");
  localBtn.className = "toggle-btn local" + (mode === "local" ? " active local" : "");

  // 本地模式下隐藏 API Key 行和积分显示
  const apiKeyRow = document.getElementById("apiKey").closest(".config-row");
  const creditsBox = document.getElementById("credits").closest(".stat");
  if (mode === "local") {
    apiKeyRow.style.display = "none";
    creditsBox.style.opacity = "0.3";
  } else {
    apiKeyRow.style.display = "";
    creditsBox.style.opacity = "1";
  }
}

// 切换模式
function switchMode(mode) {
  // 保存当前 URL 到对应模式
  const currentUrl = document.getElementById("wsUrl").value.trim();
  chrome.storage.local.get(["cloudWsUrl", "localWsUrl"], (stored) => {
    const save = {};
    if (currentMode === "cloud") {
      save.cloudWsUrl = currentUrl;
    } else {
      save.localWsUrl = currentUrl;
    }
    save.mode = mode;

    // 切换到新模式的 URL
    if (mode === "local") {
      document.getElementById("wsUrl").value = stored.localWsUrl || DEFAULT_LOCAL_URL;
    } else {
      document.getElementById("wsUrl").value = stored.cloudWsUrl || currentUrl;
    }

    chrome.storage.local.set(save, () => {
      updateModeUI(mode);
      // 自动保存并重连
      const config = {
        wsUrl: document.getElementById("wsUrl").value.trim(),
        apiKey: mode === "local" ? "" : document.getElementById("apiKey").value.trim(),
      };
      chrome.runtime.sendMessage({ type: "saveConfig", config });
    });
  });
}
// 绑定点击事件
document.getElementById("modeCloud").addEventListener("click", () => switchMode("cloud"));
document.getElementById("modeLocal").addEventListener("click", () => switchMode("local"));

// 初始加载状态
chrome.runtime.sendMessage({ type: "getState" }, (state) => {
  if (state) updateUI(state);
});
chrome.runtime.sendMessage({ type: "getLogs" }, (logs) => {
  if (logs) renderLogs(logs);
});

// 加载配置 + 模式
chrome.storage.local.get(["mode", "cloudWsUrl", "localWsUrl"], (stored) => {
  const mode = stored.mode || "cloud";

  chrome.runtime.sendMessage({ type: "getConfig" }, (cfg) => {
    if (cfg) {
      document.getElementById("wsUrl").value = cfg.wsUrl || "";
      document.getElementById("apiKey").value = cfg.apiKey || "";
      if (!cfg.incognitoAllowed) {
        document.getElementById("privacyWarn").style.display = "block";
      }
    }
    updateModeUI(mode);
  });
});

// 保存配置
document.getElementById("saveBtn").addEventListener("click", () => {
  const config = {
    wsUrl: document.getElementById("wsUrl").value.trim(),
    apiKey: document.getElementById("apiKey").value.trim(),
  };
  // 同时存到对应模式的 URL
  const save = {};
  if (currentMode === "cloud") {
    save.cloudWsUrl = config.wsUrl;
  } else {
    save.localWsUrl = config.wsUrl;
  }
  chrome.storage.local.set(save);

  const btn = document.getElementById("saveBtn");
  chrome.runtime.sendMessage({ type: "saveConfig", config }, () => {
    btn.textContent = "已保存";
    btn.className = "btn saved";
    setTimeout(() => {
      btn.textContent = "保存并重连";
      btn.className = "btn";
    }, 1500);
  });
});

// 实时更新
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "stateUpdate") {
    updateUI(msg.state);
    chrome.runtime.sendMessage({ type: "getLogs" }, (logs) => {
      if (logs) renderLogs(logs);
    });
  }
});

// 定期轮询状态（防止 popup 打开时恰好处于重连间隙显示未连接）
setInterval(() => {
  chrome.runtime.sendMessage({ type: "getState" }, (state) => {
    if (state) updateUI(state);
  });
}, 2000);
