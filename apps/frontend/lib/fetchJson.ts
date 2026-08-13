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
