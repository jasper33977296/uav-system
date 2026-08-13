/** REST 回來的 JSONB 欄位是字串（asyncpg 預設），WS 路徑已經是物件。
 *
 * **為什麼要這個共用解析器**（2026-08-13，設計師掃出五處同形）：
 * 原本各處都寫成 `rows.map((r) => ({ ...r, x: JSON.parse(r.x) }))`。
 * `JSON.parse` 在 `map` 裡拋出時**整批 rows 一起沒了**，而外層一律接
 * `.catch(() => {})`——於是**一列髒資料 → 整個清單消失 → 畫面顯示空態**。
 * 實測：注入 3 筆事件其中 1 筆 detail 非法，事件流顯示「尚無事件」。
 *
 * 空事件流宣告的是「沒有異常發生」——**靜默失效會讓故障中的飛機看起來健康**。
 * 這類「失效冒充誠實空態」的缺陷可以活很久，因為它與我們刻意設計的誠實空態
 * 在畫面上完全同形。回歸測試在 scripts/uitest/empty_state.mjs。
 *
 * 本函式**只負責把失敗侷限在單列**，失敗後要顯示什麼由呼叫端決定——
 * 各處的正確答案不同（事件列要顯示狀態句、架次摘要要顯示「—」）。
 */
export type JsonbResult =
  | { ok: true; value: unknown }
  | { ok: false; raw: string };

export function parseJsonb(raw: unknown): JsonbResult {
  if (typeof raw !== "string") return { ok: true, value: raw };
  try {
    return { ok: true, value: JSON.parse(raw) };
  } catch {
    return { ok: false, raw };
  }
}

/** 事件 detail 專用：壞掉時保留原文並標記，**不丟掉那一列**。
 * 呈現規則（設計師 2026-08-13 裁定，ui-spec §0.2b）：
 *   - 主文顯示狀態句「無法解讀的訊息」、次要文字色——**不顯示壞掉的原文**，
 *     半截 JSON 放在內容欄位會看起來像內容，使用者會試圖從亂碼推測發生了什麼
 *   - **不加額外符號**：`?`／`⚠` 已被字典版本旗標佔用，兩種不同的缺口不能
 *     共用同一個記號（不得互相冒充）
 *   - `severity` 不受影響：它是獨立欄位，**critical 事件即使 detail 壞掉，
 *     紅點照樣要紅**——解析失敗是我方讀不懂內容，不是事件變不嚴重
 * 原文留在 `raw`，modal 的鍵值表看得到（看得懂的錯誤好過看不見的沉默）。
 */
export function eventDetail(raw: unknown): Record<string, unknown> {
  const r = parseJsonb(raw);
  if (r.ok) return (r.value ?? {}) as Record<string, unknown>;
  return { parse_failed: true, raw: r.raw };
}
