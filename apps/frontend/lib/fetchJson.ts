/** GET JSON：HTTP 4xx/5xx 一律拋，不讓「取得失敗」變成「沒有資料」。
 *
 * **為什麼需要這個**（2026-08-13，ui-spec §0.2e）：`fetch` **不會**因 HTTP
 * 錯誤而 reject，而錯誤回應的 body（`{"detail": ...}`）是合法 JSON。於是
 * `fetch(...).then(r => r.json()).then(d => setRows(d.link ?? []))` 這種寫法
 * 在後端 500 時得到空陣列，畫面**把我方的取得失敗說成對方沒有資料**：
 *
 *   後端 500 → 事件流顯示「尚無事件」＝**宣告飛機一切正常**。
 *
 * 這是本專案排序最前面的那種謊（失效落在「比真實狀況更令人安心」那一側），
 * 而且它與誠實空態在畫面上完全同形，沒有人會去追問一個合法又常見的狀態。
 * 實測掃到 29 個取用點不檢查 `r.ok`。
 *
 * **呼叫端的責任**：拋出後不得靜默吞掉——要嘛顯示「無法取得…」，要嘛
 * 讓既有的錯誤區塊接手。**三種狀態（載入中／取得失敗／真的沒有）各自要有
 * 自己的話**，把其中兩種塞進同一句等於兩個都沒宣告。
 */
export class HttpError extends Error {
  constructor(public status: number, public url: string) {
    super(`HTTP ${status}`);
    this.name = "HttpError";
  }
}

export async function getJson<T = unknown>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new HttpError(r.status, url);
  return (await r.json()) as T;
}

/** HTTP 錯誤回應的 `detail` → **一定是字串**。
 *
 * **為什麼需要這個**：FastAPI 的 422（欄位驗證失敗）回的 `detail` 是一個
 * **物件陣列**，不是字串。前端各處都寫 `setErr(body.detail)` 然後 `{err}`
 * 丟進 JSX——React 遇到物件會拋 "Objects are not valid as a React child"，
 * 整頁白畫面。2026-08-26 實際發生：一份 QGC 的 .plan 因為
 * `plannedHomePosition` 的高度是 null 而被擋下，使用者看到的不是「這個欄位
 * 不合法」，是**前端整個崩潰**。
 *
 * 錯誤處理本身把畫面弄壞，比原本那個錯誤嚴重得多。
 */
export function errText(detail: unknown, fallback: string): string {
  if (detail == null) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI 422：[{loc: [...], msg: "...", type: "..."}]
    const parts = detail.map((d: any) => {
      if (typeof d === "string") return d;
      const loc = Array.isArray(d?.loc) ? d.loc.filter(
        (x: unknown) => x !== "body").join(".") : "";
      return loc ? `${loc}：${d?.msg ?? JSON.stringify(d)}` : (d?.msg ?? JSON.stringify(d));
    });
    return parts.join("；") || fallback;
  }
  if (typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    if (typeof d.msg === "string") return d.msg;
    return JSON.stringify(detail);
  }
  return String(detail);
}
