// blogbot 대시보드 페이지에서만 실행되는 브리지.
// 페이지 <-> 확장 background 사이에서 메시지를 릴레이한다.

// 페이지가 언제 로드됐든 확장을 감지할 수 있도록,
// 즉시 그리고 DOMContentLoaded 시점에도 ready 신호를 보낸다.
function announce() {
  window.postMessage({ type: "blogbot-ext-ready" }, window.location.origin);
}
announce();
document.addEventListener("DOMContentLoaded", announce);

// 페이지가 "너 있어?"라고 물어오면 다시 ready 응답.
// 페이지 스크립트 등록 타이밍이 어긋나는 경우를 위한 안전장치.
window.addEventListener("message", (ev) => {
  if (ev.source !== window || !ev.data) return;
  if (ev.data.type === "blogbot-ext-check") announce();

  if (ev.data.type === "blogbot-ext-process") {
    const requestId = ev.data.requestId;
    chrome.runtime.sendMessage(
      {
        action: "processUrl",
        url: ev.data.url,
        category: ev.data.category,
        model: ev.data.model,
        note: ev.data.note,
      },
      (resp) => {
        window.postMessage(
          { type: "blogbot-ext-result", requestId, ...(resp || { ok: false, error: "확장 응답 없음" }) },
          window.location.origin
        );
      }
    );
  }
});
