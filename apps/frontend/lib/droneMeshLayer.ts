/** 3D 機體圖層（ui-spec §2.4d）——近距真 mesh、遠距交回 2D 圖示。
 *
 * ⚠ **本模組目前沒有任何呼叫端**，且**必須維持如此**直到前端映像重建、
 * 容器的 node_modules volume 拿到 `@deck.gl/mesh-layers` 為止——
 * webpack 只編譯從頁面可達的模組，未被 import 就不會進 build，
 * 所以本檔存在不影響執行；**一旦有人 import 而容器沒有該套件，整站 500**
 * （2026-08-13 實測，服務中斷兩分鐘）。後端重建映像後，在 MapView 加
 * 一行 `...droneMeshLayers(...)` 即接上。
 *
 * 硬約束（§2.4d，每條都對應到下面的具體實作）：
 *   (a) 幾何程序化建構、零外部請求 → 用 lib/droneMesh 的自建 attributes，
 *       **不用 ScenegraphLayer/glTF/CDN**（守的是幾何從哪來，不是用哪個套件）
 *   (b) 識別色與文字標籤規則不變 → getColor 用同一份 colorFor
 *   (c) 無 heading 不假旋轉、本體不加機頭 → 不設 getOrientation，
 *       mesh 本身四向對稱（§2.4c 使用者裁定：方向由軌跡承載）
 *   (d) 不得被覆蓋 → depthTest:false，且呼叫端須排在所有軌跡層之後
 *   (e) 最小可見尺寸不得小於 2D 圖示 → sizeScale 由螢幕像素反推，見下
 */
import { SimpleMeshLayer } from "@deck.gl/mesh-layers";

import { droneMesh, lodFactor, MESH_RADIUS_M } from "@/lib/droneMesh";

interface FleetPos {
  id: string;
  lat: number | null;
  lon: number | null;
  alt_rel: number | null;
}

// 2D 圖示的視覺半徑（getSize 44 的一半）——mesh 不得比它小
const ICON_RADIUS_PX = 22;

/** 依 zoom 產生 3D 機體層；LOD 係數為 0（縮得很遠）時回空陣列不浪費 GPU。
 * 回傳陣列以便呼叫端 `...` 展開，與 pathsLayer 的用法一致。 */
export function droneMeshLayers(
  fleet: FleetPos[],
  zoom: number,
  colorOf: (id: string) => [number, number, number, number],
) {
  const t = lodFactor(zoom);
  const data = fleet.filter((d) => d.lat != null && d.lon != null);
  if (t <= 0 || !data.length) return [];

  // 最小尺寸保證（約束 e）：把 mesh 外接半徑換算成螢幕像素，不足就整體
  // 放大到與 2D 圖示同級。近距時自然尺寸勝出（scale 回到 1）——那時
  // mesh 才是「實際大小的無人機」，這正是 3D 模型的意義
  const lat0 = data[0].lat!;
  const metersPerPx = (156543.03392 * Math.cos((lat0 * Math.PI) / 180))
    / Math.pow(2, zoom);
  const scale = Math.max(1, (ICON_RADIUS_PX * metersPerPx) / MESH_RADIUS_M);

  return [new SimpleMeshLayer<FleetPos>({
    id: "drone-mesh",
    data,
    mesh: droneMesh(),
    getPosition: (d) => [d.lon!, d.lat!, d.alt_rel ?? 0],
    getColor: (d) => colorOf(d.id),
    sizeScale: scale,
    opacity: t,                       // 與 2D 圖示互補淡出（呼叫端用 1-t）
    // 約束 (d)：mesh 有深度，與同在 3D 空間的軌跡絲帶會互相穿插——
    // depthCompare:"always"＝不做深度比較（luma.gl v9 的寫法，舊 depthTest
    // 已移除），配合排在軌跡之後，機體必定可見。
    // 「找不到機在哪＝監控失效」，任何美觀考量都不得凌駕
    parameters: { depthCompare: "always" as const },
    updateTriggers: {
      getPosition: data.map((d) => d.id).join(","),
      getColor: data.map((d) => d.id).join(","),
    },
  })];
}
