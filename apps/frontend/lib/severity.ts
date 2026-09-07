/** 事件嚴重度的正規化。
 *
 * **後端同時寫過 `warn` 與 `warning`**（實測 2026-09-07：`events` 表裡 292 列
 * 是 `warn`——90 列 `driver_disagreement`、147 列 `rc_link`、其餘是回傳遺失與
 * sysid 重指派）。而畫面上的對照表只認 `warning`，查不到的鍵一律退回灰色的
 * 「資訊」——**那 292 則警告在畫面上長得跟正常事件一模一樣**，資訊頁的
 * 「嚴重度」下拉也永遠選不到它們。
 *
 * 這正是 ui-spec §0.2b 要防的那一類：**非正常冒充正常**。而且它比 §0.2e 的
 * 空態問題更難察覺——事件確實出現在清單上，只是顏色說錯了話。
 *
 * 寫入端已在 `db.insert_event` 正規化（2026-09-07），但**歷史那 292 列不動**
 * （改寫既有事件等於改寫紀錄），所以讀的這一端必須自己認得舊值。
 *
 * **認不得的值退回 `info` 是有代價的**：新的高嚴重度字串會被畫成資訊。
 * 所以這裡照枚舉列，新增值時同時改這裡與後端——不做「猜字串」的通融。
 */
export type Sev = "critical" | "warning" | "info";

export function normSev(s: string | null | undefined): Sev {
  switch (s) {
    case "critical":
      return "critical";
    case "warning":
    case "warn":
      return "warning";
    default:
      return "info";
  }
}

/** 嚴重度色（狀態色盤，與軌跡分級色同一套語意來源）。 */
export const SEV_DOT: Record<Sev, string> = {
  critical: "#a01818", warning: "#fab219", info: "#8f8b80",
};
export const SEV_TEXT: Record<Sev, string> = {
  critical: "危急", warning: "警告", info: "資訊",
};
