/** 路徑方向箭頭圖示（回放頁，deck IconLayer 用）。
 *
 * 造型是**凹尾飛鏢**而非正三角：正三角在小尺寸下（本圖示常在 7–14px）
 * 三個角一樣尖，看不出哪邊是頭；凹尾一眼就分得出前後。
 * 白底＋暗描邊——箭頭要壓在分級色（綠/黃/橙/紅）的航跡線上，
 * 純白會在淺色段消失，純暗會在深色段消失，描邊是兩邊都活的做法。
 */

const SIZE = 64;   // 圖素邊長；實際顯示大小由 IconLayer 的 getSize 決定

// 朝上（北）為 0°：呼叫端用 getAngle 轉到航向
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${SIZE}" height="${SIZE}"
  viewBox="0 0 64 64">
  <path d="M32 6 L56 56 L32 43 L8 56 Z" fill="#e8eaed" stroke="#141310"
        stroke-width="5" stroke-linejoin="round"/>
</svg>`;

export const ARROW_ICON_SIZE = SIZE;
export const arrowIconUrl =
  `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
