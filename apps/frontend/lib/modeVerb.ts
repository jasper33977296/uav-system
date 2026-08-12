/** 飛行模式的廠牌無關語意（ui-spec §0.2d）。
 *
 * **顯示層永遠用 `flight_mode` 原廠名**——機上原文不翻譯，本表只在混機
 * 時**加註**語意，不取代顯示。
 * **判斷層一律用 `mode_verb`**：狀態判定、事件摺疊、跨機一致性檢查。
 * **禁止比對 `flight_mode` 字串**——PX4 `HOLD` 與 ArduPilot `LOITER` 是同
 * 一件事，比字串在混機環境必錯（這正是本欄位存在的理由）。
 *
 * 值域由後端驅動的模式表推導（2026-08-12）：
 *   guided | hold | land | mission | position | rtl | null
 * **`null` 是常態不是異常**：PX4 起飛中（AUTO.TAKEOFF）、手動類模式、
 * ArduPilot SMART_RTL/AUTO_RTL 都是 null——後者刻意不對到 rtl，因為沿原
 * 路徑返航與直線返航行為不同，標成 rtl 等於宣告一個不成立的等價。
 * 因此 null 不得有任何「怪怪的」呈現（灰字/問號/警示）：起飛時每台 PX4
 * 都會是 null。
 */
export const MODE_VERB_TXT: Record<string, string> = {
  hold: "定點停懸",
  mission: "任務執行",
  rtl: "返航",
  land: "降落",
  position: "位置控制",
  guided: "外部導引",   // 僅 ArduPilot 會出現，PX4 無對等概念
};

/** 混機時的模式顯示：原廠名＋語意括注。
 * - `mixed=false`（單一廠牌）→ 只有原廠名，無歧義就不加字
 * - `verb` 為 null 或不在表上 → 不註記（不猜、不硬翻） */
export function modeLabel(
  flightMode: string | null | undefined,
  verb: string | null | undefined,
  mixed: boolean,
): string {
  const name = flightMode || "—";
  if (!mixed || !verb) return name;
  const txt = MODE_VERB_TXT[verb];
  return txt ? `${name}（${txt}）` : name;
}
