// 向 background 查询：当前标签页是否有关联的爬取任务
chrome.runtime.sendMessage({ type: "getTask" }, (response) => {
  if (!response) return; // 普通浏览，不做任何事

  const { taskId, selector } = response;
  console.log(`[OpenCrawl] 任务 ${taskId.slice(0, 8)}，等待页面渲染...`);

  waitForRender(() => {
    console.log("[OpenCrawl] 页面就绪，开始提取...");
    const result = extract(selector);

    console.log(
      `[OpenCrawl] ${result.data ? result.data.length + " 字符" : "失败: " + result.error}`
    );

    chrome.runtime.sendMessage({
      type: "taskResult",
      taskId,
      data: result.data,
      error: result.error,
    });
  });
});

function waitForRender(callback) {
  let mutationTimer = null;
  let settled = false;
  const MAX_WAIT = 15000;

  function settle() {
    if (settled) return;
    settled = true;
    observer.disconnect();
    if (mutationTimer) clearTimeout(mutationTimer);
    callback();
  }

  const observer = new MutationObserver(() => {
    if (mutationTimer) clearTimeout(mutationTimer);
    mutationTimer = setTimeout(settle, 2000); // DOM 稳定 2 秒
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // 初始等 3 秒
  mutationTimer = setTimeout(settle, 3000);

  // 兜底 15 秒
  setTimeout(settle, MAX_WAIT);
}

function extract(selector) {
  try {
    if (selector) {
      const elements = document.querySelectorAll(selector);
      if (elements.length === 0) {
        return { data: null, error: `未找到匹配 "${selector}" 的元素` };
      }
      return {
        data: Array.from(elements)
          .map((el) => el.innerText || el.textContent)
          .join("\n---\n"),
        error: null,
      };
    }

    // 全文提取：移除干扰元素的克隆
    const clone = document.body.cloneNode(true);
    clone.querySelectorAll("script, style, noscript, svg, iframe").forEach((el) => el.remove());
    const text = clone.innerText || clone.textContent || "";

    if (!text || text.trim().length === 0) {
      return { data: null, error: "页面内容为空" };
    }
    return { data: text, error: null };
  } catch (e) {
    return { data: null, error: "提取出错: " + e.message };
  }
}
