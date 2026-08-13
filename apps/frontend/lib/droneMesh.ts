/** 程序化四旋翼 mesh（ui-spec §2.4d）——**不載入任何外部模型檔**。
 *
 * ⚠ **尚未接線**：§2.4d 的設計方案（billboard／3D 模型／混合）還沒經使用者
 * 選定，PM 裁示設計未核准前不進實作路徑。本檔是預先備好的幾何，**目前沒有
 * 任何呼叫端**；若最終選用 3D 模型，接回 MapView 只需一個 SimpleMeshLayer
 * （相依 @deck.gl/mesh-layers 已在 package.json，但**容器的 node_modules 是
 * 獨立 volume，需重建映像才拿得到**——直接 import 會讓整站 500，實測過）。
 * 若選了別的方案，本檔應整個刪除，不要留著當「好像有這功能」。
 *
 * 離線約束（沿用 droneIcon.ts 的 data URI 前例）：場域實測常在沒有網路的
 * 環境，模型必須跟程式一起進 bundle。這裡直接用三角形陣列建幾何，
 * 不引 glTF/OBJ/CDN。
 *
 * 造型：中央機身（扁盒）＋四支斜臂＋四個旋翼盤，全部對稱——
 * **不加機頭指標**（§2.4c 使用者裁定：無人機哪邊是頭不重要，方向由軌跡
 * 承載），故本 mesh 在任何朝向下看起來都一致，也就不需要 heading。
 *
 * 座標：以公尺為單位、原點在機身中心、+Z 向上。SimpleMeshLayer 以
 * sizeScale 換算到世界尺度。
 */

/** deck.gl SimpleMeshLayer 接受的幾何格式（attributes + indices） */
export interface MeshData {
  attributes: {
    POSITION: { value: Float32Array; size: 3 };
    NORMAL: { value: Float32Array; size: 3 };
  };
  indices: { value: Uint16Array; size: 1 };
}

/** 建一個軸對齊盒（cx,cy,cz 中心；sx,sy,sz 半邊長）→ 推進頂點陣列 */
function pushBox(
  pos: number[], nrm: number[], idx: number[],
  cx: number, cy: number, cz: number, sx: number, sy: number, sz: number,
) {
  const base = pos.length / 3;
  // 六面各自獨立頂點（法線才不會被平均掉，邊緣清楚）
  const faces: [number[], number[]][] = [
    [[1, 0, 0], [1, 1, 1, 1, -1, 1, 1, -1, -1, 1, 1, -1]],
    [[-1, 0, 0], [-1, 1, -1, -1, -1, -1, -1, -1, 1, -1, 1, 1]],
    [[0, 1, 0], [-1, 1, 1, 1, 1, 1, 1, 1, -1, -1, 1, -1]],
    [[0, -1, 0], [-1, -1, -1, 1, -1, -1, 1, -1, 1, -1, -1, 1]],
    [[0, 0, 1], [-1, -1, 1, 1, -1, 1, 1, 1, 1, -1, 1, 1]],
    [[0, 0, -1], [-1, 1, -1, 1, 1, -1, 1, -1, -1, -1, -1, -1]],
  ];
  let f = 0;
  for (const [n, verts] of faces) {
    for (let i = 0; i < 4; i++) {
      pos.push(cx + verts[i * 3] * sx, cy + verts[i * 3 + 1] * sy,
        cz + verts[i * 3 + 2] * sz);
      nrm.push(n[0], n[1], n[2]);
    }
    const b = base + f * 4;
    idx.push(b, b + 1, b + 2, b, b + 2, b + 3);
    f++;
  }
}

/** 建一個圓柱（機身/旋翼盤共用）：上下兩面＋側環——側環讓它在低俯角下
 * 仍有實體高度（2D 圖示的核心差別就在這裡） */
function pushDisc(
  pos: number[], nrm: number[], idx: number[],
  cx: number, cy: number, cz: number, r: number, h: number, seg = 12,
) {
  // 側環（先畫，法線朝外）
  const ringBase = pos.length / 3;
  for (let i = 0; i <= seg; i++) {
    const a = (i / seg) * Math.PI * 2;
    const nx = Math.cos(a), ny = Math.sin(a);
    pos.push(cx + nx * r, cy + ny * r, cz + h);
    nrm.push(nx, ny, 0);
    pos.push(cx + nx * r, cy + ny * r, cz - h);
    nrm.push(nx, ny, 0);
  }
  for (let i = 0; i < seg; i++) {
    const b0 = ringBase + i * 2;
    idx.push(b0, b0 + 1, b0 + 3, b0, b0 + 3, b0 + 2);
  }
  for (const [zSign, nz] of [[1, 1], [-1, -1]] as const) {
    const center = pos.length / 3;
    pos.push(cx, cy, cz + zSign * h);
    nrm.push(0, 0, nz);
    for (let i = 0; i <= seg; i++) {
      const a = (i / seg) * Math.PI * 2;
      pos.push(cx + Math.cos(a) * r, cy + Math.sin(a) * r, cz + zSign * h);
      nrm.push(0, 0, nz);
    }
    for (let i = 0; i < seg; i++) {
      // 上下面繞向相反，法線才朝外
      if (zSign > 0) idx.push(center, center + 1 + i, center + 2 + i);
      else idx.push(center, center + 2 + i, center + 1 + i);
    }
  }
}

let cached: MeshData | null = null;

/** 四旋翼 mesh（單例——幾何固定，每機只換位置/顏色/尺度） */
export function droneMesh(): MeshData {
  if (cached) return cached;
  const pos: number[] = [], nrm: number[] = [], idx: number[] = [];
  // 機身：圓柱（半徑 0.62m、半高 0.22m）——側環給它實體厚度，
  // 低俯角時看得出「這是一個有高度的物體」而不是貼地的圖片
  pushDisc(pos, nrm, idx, 0, 0, 0, 0.62, 0.22, 16);
  const ARM = 1.55;                  // 旋翼中心距機身中心
  for (const deg of [45, 135, 225, 315]) {
    const r = (deg * Math.PI) / 180;
    const ax = Math.cos(r), ay = Math.sin(r);
    // 斜臂：沿對角線的細長盒（用中點與半長近似，四臂對稱不需旋轉矩陣）
    const mx = (ax * ARM) / 2, my = (ay * ARM) / 2;
    pushBox(pos, nrm, idx, mx, my, 0,
      Math.abs(ax) * ARM * 0.5 + 0.12, Math.abs(ay) * ARM * 0.5 + 0.12, 0.08);
    // 旋翼盤：浮在臂端上方
    pushDisc(pos, nrm, idx, ax * ARM, ay * ARM, 0.18, 0.62, 0.05);
  }
  cached = {
    attributes: {
      POSITION: { value: new Float32Array(pos), size: 3 },
      NORMAL: { value: new Float32Array(nrm), size: 3 },
    },
    indices: { value: new Uint16Array(idx), size: 1 },
  };
  return cached;
}

/** mesh 的外接半徑（公尺，未縮放）——LOD 換手時用來對齊 2D 圖示視覺大小 */
export const MESH_RADIUS_M = 1.55 + 0.62;

/** LOD 係數（§2.4d）：0＝完全用 2D 圖示、1＝完全用 3D mesh，中間交叉淡出。
 * 兩邊共用同一個函式，避免門檻各寫一份而漂移（換手就會「跳」或「兩個都淡」）。 */
export const LOD_Z_LOW = 16, LOD_Z_HIGH = 17.5;
export function lodFactor(zoom: number): number {
  return Math.max(0, Math.min(1, (zoom - LOD_Z_LOW) / (LOD_Z_HIGH - LOD_Z_LOW)));
}
