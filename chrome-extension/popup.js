// 현재 활성 탭에서 실행되어 본문을 뽑는 함수. 페이지 컨텍스트에서 돌아간다.
function extractArticleFromPage() {
  const strip = (s) => (s || "").replace(/\s+/g, " ").trim();

  // 1) 이상적 컨테이너 우선순위: <article>, <main>, 가장 큰 <div>
  const containers = [];
  document.querySelectorAll("article, main, [role=main]").forEach((el) => containers.push(el));
  if (containers.length === 0) containers.push(document.body);

  // 2) 각 컨테이너의 <p> 텍스트를 모아 가장 긴 것 선택
  let best = { len: 0, paras: [] };
  for (const c of containers) {
    const paras = [];
    c.querySelectorAll("p").forEach((p) => {
      // script, style, nav 안쪽은 무시
      if (p.closest("script, style, nav, aside, footer, header")) return;
      const t = strip(p.innerText);
      if (t.length > 30) paras.push(t);
    });
    const totalLen = paras.reduce((a, b) => a + b.length, 0);
    if (totalLen > best.len) best = { len: totalLen, paras };
  }

  return {
    url: location.href,
    title: strip(document.title),
    body: best.paras.join("\n\n"),
  };
}

const $ = (s) => document.querySelector(s);
const log = (msg, cls) => {
  const el = $("#log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
};

const SERVER = "http://127.0.0.1:8765";

async function loadModels() {
  try {
    const c = await (await fetch(`${SERVER}/api/config`)).json();
    const sel = $("#model");
    sel.innerHTML = (c.models || [])
      .map((m) => `<option value="${m.id}" ${m.id === c.model ? "selected" : ""}>${m.label}</option>`)
      .join("");
  } catch (e) {
    log(`⚠ blogbot 서버(${SERVER})에 연결할 수 없습니다. 'python -m blogbot web'을 먼저 실행하세요.`, "err");
    $("#go").disabled = true;
  }
}
loadModels();

$("#go").addEventListener("click", async () => {
  const btn = $("#go");
  btn.disabled = true;
  $("#log").innerHTML = "";
  $("#preview").style.display = "none";
  try {
    log("1) 페이지에서 본문 추출 중...");
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) throw new Error("활성 탭을 찾을 수 없습니다.");
    const [{ result: article }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractArticleFromPage,
    });
    if (!article || (article.body || "").length < 300) {
      throw new Error(`본문이 너무 짧습니다 (${(article?.body || "").length}자). 기사 페이지에서 다시 시도하세요.`);
    }
    log(`✓ 본문 ${article.body.length}자 확보`);
    log(`  제목: ${article.title.slice(0, 60)}`);

    log("2) blogbot 서버로 전송 중...");
    const resp = await fetch(`${SERVER}/api/from-browser`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: article.url,
        title: article.title,
        body: article.body,
        category: $("#cat").value,
        model: $("#model").value,
        note: $("#note").value,
      }),
    });
    const initial = await resp.json();
    if (!resp.ok) throw new Error(initial.error || "서버 오류");
    const jobId = initial.job;
    log(`✓ 작업 시작 (id: ${jobId})`);

    // 진행 폴링
    log("3) 글 생성 중... (1~2분)");
    let lastLen = 0;
    while (true) {
      await new Promise((r) => setTimeout(r, 1500));
      const j = await (await fetch(`${SERVER}/api/jobs/${jobId}`)).json();
      const newLines = j.log.slice(lastLen);
      lastLen = j.log.length;
      newLines.forEach((l) => log("  " + l.slice(0, 120)));
      if (j.status === "done" && j.result) {
        const url = `${SERVER}/preview/${j.result.draft}/`;
        const a = $("#preview");
        a.href = url;
        a.style.display = "block";
        a.textContent = `✓ 완료: ${j.result.title.slice(0, 40)} — 미리보기 열기`;
        break;
      }
      if (j.status === "error") { log("✗ 실패", "err"); break; }
    }
  } catch (e) {
    log(`✗ ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});
