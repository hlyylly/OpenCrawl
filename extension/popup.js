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

// 初始加载状态
chrome.runtime.sendMessage({ type: "getState" }, (state) => {
  if (state) updateUI(state);
});
chrome.runtime.sendMessage({ type: "getLogs" }, (logs) => {
  if (logs) renderLogs(logs);
});

// 加载配置
chrome.runtime.sendMessage({ type: "getConfig" }, (cfg) => {
  if (cfg) {
    document.getElementById("wsUrl").value = cfg.wsUrl || "";
    document.getElementById("apiKey").value = cfg.apiKey || "";
    if (!cfg.incognitoAllowed) {
      document.getElementById("privacyWarn").style.display = "block";
    }
  }
});

// 保存配置
document.getElementById("saveBtn").addEventListener("click", () => {
  const config = {
    wsUrl: document.getElementById("wsUrl").value.trim(),
    apiKey: document.getElementById("apiKey").value.trim(),
  };
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
