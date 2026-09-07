import React from "react";

/** `**強調**` → `<b>`。
 *
 * 後端與機上代理的文案用 `**` 當強調記號（`⚠ 遙控器離線——此時**不得**起飛`），
 * 而畫面上沒有任何一處在解析 Markdown（ui-spec §0.3c）——直接印出來就是
 * 一串星號給使用者看。
 *
 * **歷史資料改不了**：那些字串已經寫進 `events.detail` 了，資訊頁要把它們
 * 一則一則翻出來讀，所以在**讀的這一端**把記號畫成粗體。
 *
 * 不成對就原樣不動：`**` 也可能是內容本身（`BATT_FS_*_ACT`、乘冪），
 * **猜錯的排版比沒有排版糟**——寧可讓那兩個星號留在畫面上。
 */
export function emph(text: string): React.ReactNode {
  if (!text.includes("**")) return text;
  const parts = text.split("**");
  if (parts.length % 2 === 0) return text;      // 記號不成對＝不動它
  return parts.map((p, i) =>
    i % 2 === 1
      ? <b key={i}>{p}</b>
      : <React.Fragment key={i}>{p}</React.Fragment>);
}

/** 同一件事，但目的地只吃字串（`title` 屬性、SVG `<title>`、toast 以外的
 * 純文字欄）。**那裡放不了 `<b>`，但也不該把星號印給使用者看**——
 * 記號成對就拿掉，不成對就原樣留著（同上：不猜）。 */
export function unemph(text: string): string {
  if (!text.includes("**")) return text;
  const parts = text.split("**");
  if (parts.length % 2 === 0) return text;
  return parts.join("");
}
