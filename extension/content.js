// 跳过 about:blank（标签页创建后会先加载 blank，再导航到目标 URL）
if (location.href !== "about:blank") {

// 向 background 查询：当前标签页是否有关联的爬取任务
chrome.runtime.sendMessage({ type: "getTask" }, (response) => {
  if (!response) return;

  const { taskId, selector, mode } = response;
  const isLite = mode === "lite";
  const isSearch = selector === "__search__";
  console.log(`[OpenCrawl] 任务 ${taskId.slice(0, 8)} ${isLite ? "(lite)" : ""} ${isSearch ? "(search)" : ""}`);

  waitForRender(isLite, isSearch, () => {
    console.log("[OpenCrawl] 页面就绪，开始提取...");

    let result;
    if (isSearch) {
      result = extractSearchResults();
    } else {
      result = extract(selector);
    }

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

function waitForRender(isLite, isSearch, callback) {
  let mutationTimer = null;
  let settled = false;

  // 搜索：等久一点让搜索引擎完全加载；lite：最快；full：最完整
  const STABLE_DELAY = isSearch ? 1500 : isLite ? 500 : 2000;
  const INITIAL_WAIT = isSearch ? 3000 : isLite ? 1000 : 3000;
  const MAX_WAIT = isSearch ? 12000 : isLite ? 8000 : 15000;

  function settle() {
    if (settled) return;
    settled = true;
    observer.disconnect();
    if (mutationTimer) clearTimeout(mutationTimer);
    callback();
  }

  const observer = new MutationObserver(() => {
    if (mutationTimer) clearTimeout(mutationTimer);
    mutationTimer = setTimeout(settle, STABLE_DELAY);
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }

  mutationTimer = setTimeout(settle, INITIAL_WAIT);
  setTimeout(settle, MAX_WAIT);
}

// ============ 搜索结果解析 ============
function extractSearchResults() {
  const host = location.hostname;
  let results = [];

  try {
    if (host.includes("duckduckgo.com")) {
      results = parseDDG();
    } else if (host.includes("bing.com")) {
      results = parseBing();
    } else if (host.includes("google.com")) {
      results = parseGoogle();
    } else if (host.includes("baidu.com")) {
      results = parseBaidu();
    }
  } catch (e) {
    return { data: null, error: "搜索结果解析失败: " + e.message };
  }

  if (results.length === 0) {
    return { data: null, error: "未找到搜索结果" };
  }

  // 返回 JSON 字符串，兼容 Brave Search 格式
  return { data: JSON.stringify(results), error: null };
}

function parseDDG() {
  const results = [];
  // DDG HTML 版本
  document.querySelectorAll(".result").forEach((el) => {
    const a = el.querySelector(".result__a");
    const snippet = el.querySelector(".result__snippet");
    if (!a) return;

    let url = a.href || "";
    // DDG 重定向 URL 解析
    if (url.includes("uddg=")) {
      try {
        const m = url.match(/uddg=([^&]+)/);
        if (m) url = decodeURIComponent(m[1]);
      } catch (e) {}
    }

    results.push({
      title: a.innerText?.trim() || "",
      url: url,
      description: snippet?.innerText?.trim() || "",
    });
  });
  return results;
}

function parseBing() {
  const results = [];
  const seen = new Set();

  // 策略1: 经典 DOM（b_algo）
  document.querySelectorAll("#b_results .b_algo, #b_results .b_ans").forEach((el) => {
    const a = el.querySelector("h2 a, h3 a");
    if (!a) return;
    const title = a.innerText?.trim();
    if (!title || seen.has(title)) return;

    let url = a.href || "";
    try {
      const u = new URL(url);
      const real = u.searchParams.get("u");
      if (real) url = atob(real.replace(/^a1/, ""));
    } catch (e) {}

    if (url.includes("bing.com/aclick")) return;

    let desc = "";
    const snippet = el.querySelector("p, .b_caption p, .b_algoSlug, .b_lineclamp2");
    desc = snippet?.innerText?.trim() || "";
    if (!desc) {
      el.querySelectorAll("span, p, div").forEach(s => {
        const t = s.innerText?.trim();
        if (t && t.length > desc.length && t !== title) desc = t;
      });
    }

    seen.add(title);
    results.push({ title, url, description: desc });
  });

  // 策略2: 补充遗漏（基于 h2 查找，兼容动态 class）
  {
    document.querySelectorAll("h2 a[href]").forEach((a) => {
      const title = a.innerText?.trim();
      if (!title || seen.has(title)) return;
      // 跳过非搜索结果
      if (title.startsWith("Related searches") || title.startsWith("Videos of") || title.startsWith("People also")) return;

      let url = a.href || "";
      if (url.includes("bing.com/aclick") || url.includes("bing.com/search") || url.includes("bing.com/videos") || url.includes("javascript:")) return;

      try {
        const u = new URL(url);
        const real = u.searchParams.get("u");
        if (real) url = atob(real.replace(/^a1/, ""));
      } catch (e) {}

      // 向上找描述
      let desc = "";
      const container = a.closest("li") || a.closest("div[class]")?.parentElement;
      if (container) {
        container.querySelectorAll("p, span").forEach(s => {
          const t = s.innerText?.trim();
          if (t && t.length > desc.length && t !== title && t.length > 20) desc = t;
        });
      }

      seen.add(title);
      results.push({ title, url, description: desc });
    });
  }

  return results;
}

function parseGoogle() {
  const results = [];
  const seen = new Set();

  // 策略：找所有 h3，往上找包含链接的祖先
  document.querySelectorAll("#rso h3, #search h3, .MjjYud h3").forEach((h3) => {
    const title = h3.innerText?.trim();
    if (!title || seen.has(title)) return;

    // 向上找最近的 <a> 祖先或兄弟
    let a = h3.closest("a") || h3.parentElement?.querySelector("a[href]");
    if (!a) {
      // 再向上一层找
      const container = h3.closest("[data-hveid]") || h3.closest(".MjjYud") || h3.parentElement?.parentElement;
      if (container) a = container.querySelector("a[href]");
    }
    if (!a) return;

    const url = a.href || "";
    if (!url.startsWith("http") || url.includes("google.com/search") || url.includes("google.com/url")) return;

    // snippet: 在 h3 的同级或父容器中找描述文本
    let desc = "";
    const container = h3.closest("[data-hveid]") || h3.closest(".MjjYud") || h3.parentElement?.parentElement?.parentElement;
    if (container) {
      // 找非标题的文本块
      const spans = container.querySelectorAll("[data-sncf], .VwiC3b, .IsZvec, .lEBKkf, [style*='-webkit-line-clamp']");
      for (const s of spans) {
        const t = s.innerText?.trim();
        if (t && t !== title && t.length > 20) { desc = t; break; }
      }
      if (!desc) {
        // 兜底：找所有 span 里最长的文本
        const allSpans = container.querySelectorAll("span");
        let longest = "";
        allSpans.forEach(s => {
          const t = s.innerText?.trim();
          if (t && t !== title && t.length > longest.length && t.length > 20) longest = t;
        });
        desc = longest;
      }
    }

    seen.add(title);
    results.push({ title, url, description: desc });
  });
  return results;
}

function parseBaidu() {
  const results = [];
  document.querySelectorAll("#content_left .c-container").forEach((el) => {
    const a = el.querySelector("h3 a");
    if (!a) return;

    // 百度真实 URL：从 data-log 或 mu 属性中提取
    let url = "";
    const mu = el.getAttribute("mu");
    if (mu && mu.startsWith("http")) {
      url = mu;
    } else {
      // data-log 里可能有真实 URL
      try {
        const log = JSON.parse(el.getAttribute("data-log") || "{}");
        if (log.mu) url = log.mu;
      } catch (e) {}
    }
    // 兜底用 a.href（百度重定向链接）
    if (!url) url = a.href || "";

    // 多种 snippet 选择器
    const snippet = el.querySelector(".c-abstract, .c-span-last .content-right_8Zs40, .c-gap-top-small span, [class*='content-right']");
    const desc = snippet?.innerText?.trim() || "";

    const title = a.innerText?.trim() || "";
    if (!title) return;

    results.push({ title, url, description: desc });
  });
  return results;
}

// ============ 普通页面提取 ============
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

} // end if (location.href !== "about:blank")
