// 확장의 백그라운드 서비스 워커.
// 원터치 자동 흐름: URL 받기 → 백그라운드 탭 오픈 → 로드 대기 → 본문 추출 → 탭 닫기 → 서버 전송.

const SERVER = "http://127.0.0.1:8765";

// 페이지 컨텍스트에서 실행되어 본문을 뽑는다 (스크립트 주입용).
function extractArticleFromPage() {
  const strip = (s) => (s || "").replace(/\s+/g, " ").trim();
  const containers = [];
  document.querySelectorAll("article, main, [role=main]").forEach((el) => containers.push(el));
  if (containers.length === 0) containers.push(document.body);
  let best = { len: 0, paras: [] };
  for (const c of containers) {
    const paras = [];
    c.querySelectorAll("p").forEach((p) => {
      if (p.closest("script, style, nav, aside, footer, header")) return;
      const t = strip(p.innerText);
      if (t.length > 30) paras.push(t);
    });
    const total = paras.reduce((a, b) => a + b.length, 0);
    if (total > best.len) best = { len: total, paras };
  }
  return { url: location.href, title: strip(document.title), body: best.paras.join("\n\n") };
}

// 탭이 완전히 로드될 때까지 대기 (JS 렌더링 시간까지 여유)
function waitForTabLoad(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("페이지 로딩 타임아웃"));
    }, timeoutMs);
    const listener = (updatedTabId, info) => {
      if (updatedTabId === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        clearTimeout(timer);
        // JS/이미지 렌더링 시간 확보
        setTimeout(resolve, 2500);
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function processUrl({ url, category, model, note }) {
  let tabId = null;
  try {
    // 1) 백그라운드 탭으로 조용히 열기 (사용자 브라우저 세션 사용)
    const tab = await chrome.tabs.create({ url, active: false });
    tabId = tab.id;

    // 2) 로드 완료 대기
    await waitForTabLoad(tabId);

    // 3) 본문 추출
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: extractArticleFromPage,
    });
    const article = results?.[0]?.result;
    if (!article || (article.body || "").length < 300) {
      return { ok: false, error: `본문이 너무 짧습니다 (${(article?.body || "").length}자). 로그인이 필요하거나 파싱 실패일 수 있습니다.` };
    }

    // 4) blogbot 서버로 전송
    const resp = await fetch(`${SERVER}/api/from-browser`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: article.url,
        title: article.title,
        body: article.body,
        category,
        model,
        note,
      }),
    });
    const j = await resp.json();
    if (!resp.ok) return { ok: false, error: j.error || `HTTP ${resp.status}` };

    return { ok: true, job: j.job, chars: article.body.length, title: article.title };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  } finally {
    // 5) 백그라운드 탭 정리 (사용자를 방해하지 않음)
    if (tabId) chrome.tabs.remove(tabId).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.action !== "processUrl") return;
  processUrl(msg).then(sendResponse);
  return true; // async
});
