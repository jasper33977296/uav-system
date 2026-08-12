/** 2D 四旋翼俯視圖示（ui-spec §2.4b）。
 *
 * 白色剪影＋暗色描邊，以 deck IconLayer 的 mask=false 直接用（描邊要保留
 * 原色，故不走 mask tint，改為每機各自產一張帶識別色的 data URI）。
 * 影像底圖上的可辨性靠描邊與陰影（軌跡不加描邊——那靠暗化 overlay）。
 *
 * 兩種造型：
 *  - nosed：機頭有指向標記，**僅在 heading 有值時使用**（旋轉才有意義）
 *  - plain：對稱造型，heading 缺值時使用——不畫一個永遠指北的假朝向
 */

const SIZE = 64;   // 畫布邊長（icon 圖素），實際顯示大小由 getSize 決定

function svg(color: string, nosed: boolean): string {
  const arms = [45, 135, 225, 315].map((deg) => {
    const r = (deg * Math.PI) / 180;
    const x = 32 + Math.cos(r) * 19, y = 32 + Math.sin(r) * 19;
    return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="8.5"/>`;
  }).join("");
  const armLines = [45, 135, 225, 315].map((deg) => {
    const r = (deg * Math.PI) / 180;
    const x = 32 + Math.cos(r) * 19, y = 32 + Math.sin(r) * 19;
    return `<line x1="32" y1="32" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`;
  }).join("");
  // 機頭指向：SVG 的 -y 方向＝圖示「上」，deck getAngle 轉到實際 heading
  const nose = nosed
    ? `<polygon points="32,4 27,14 37,14" fill="${color}" stroke="#141310"
         stroke-width="2" stroke-linejoin="round"/>`
    : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${SIZE}" height="${SIZE}"
    viewBox="0 0 64 64">
    <g stroke="#141310" stroke-width="3.5" fill="none" stroke-linecap="round">
      ${armLines}
    </g>
    <g stroke="#141310" stroke-width="3" fill="${color}">
      ${arms}
      <rect x="24" y="24" width="16" height="16" rx="4"/>
    </g>
    ${nose}
  </svg>`;
}

const cache = new Map<string, string>();

/** 回傳可直接餵給 IconLayer 的 data URI（同色同造型只產一次） */
export function droneIconUrl(color: string, nosed: boolean): string {
  const key = `${color}|${nosed}`;
  let url = cache.get(key);
  if (!url) {
    url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg(color, nosed))}`;
    cache.set(key, url);
  }
  return url;
}

export const DRONE_ICON_SIZE = SIZE;
