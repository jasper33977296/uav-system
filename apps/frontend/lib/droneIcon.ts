/** 2D 四旋翼俯視圖示（ui-spec §2.4b，機頭指標於 §2.4c 移除）。
 *
 * 白色剪影＋暗色描邊，以 deck IconLayer 的 mask=false 直接用（描邊要保留
 * 原色，故不走 mask tint，改為每機各自產一張帶識別色的 data URI）。
 * 影像底圖上的可辨性靠描邊與陰影（軌跡不加描邊——那靠暗化 overlay）。
 *
 * **對稱造型、不隨 heading 旋轉**（§2.4c 使用者裁定：「對無人機而言哪邊是
 * 頭不重要，方向由軌跡承載」）——§2.4b 的「隨 heading 旋轉」與機頭指標
 * 一併作廢。旋轉語意退場後，heading 缺值與否也不再影響造型。
 */

const SIZE = 64;   // 畫布邊長（icon 圖素），實際顯示大小由 getSize 決定

function svg(color: string, halo: boolean): string {
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
  // 白色外圈（回放游標用）：暗色描邊在深底上與軌跡分不開——游標必須
  // 一眼可辨為「現在在看的時刻」，而不是軌跡上的一個點
  const haloG = halo
    ? `<g stroke="rgba(255,255,255,0.9)" stroke-width="7" fill="none"
         stroke-linecap="round">${armLines}</g>
       <g stroke="rgba(255,255,255,0.9)" stroke-width="6" fill="none">
         ${arms}<rect x="24" y="24" width="16" height="16" rx="4"/></g>`
    : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${SIZE}" height="${SIZE}"
    viewBox="0 0 64 64">
    ${haloG}
    <g stroke="#141310" stroke-width="3.5" fill="none" stroke-linecap="round">
      ${armLines}
    </g>
    <g stroke="#141310" stroke-width="3" fill="${color}">
      ${arms}
      <rect x="24" y="24" width="16" height="16" rx="4"/>
    </g>
  </svg>`;
}

const cache = new Map<string, string>();

/** 回傳可直接餵給 IconLayer 的 data URI（同色同造型只產一次）。
 * halo＝白色外圈，回放游標用（與軌跡視覺可分，§2.4b 配套要求）。 */
export function droneIconUrl(color: string, halo = false): string {
  const key = `${color}|${halo}`;
  let url = cache.get(key);
  if (!url) {
    url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg(color, halo))}`;
    cache.set(key, url);
  }
  return url;
}

export const DRONE_ICON_SIZE = SIZE;
