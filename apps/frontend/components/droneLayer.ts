/** 無人機 3D 球體層：three.js custom layer。
 *
 * MapLibre 的 fill-extrusion 只能做柱體，真正的球體需要自帶 WebGL 場景。
 * 本層從一開始就是**多機結構**：以 drone_id 為鍵維護球體，getDrones() 回傳
 * 幾台就畫幾台——多機（013 編隊/多 SITL）時 backend 廣播幾台的遙測
 * （dict[drone_id, LiveState]）這裡就畫幾台，不需要任何改動。
 *
 * 座標精度：mercator 座標是 0..1 的小數，直接餵給 float32 的 GPU 會在高
 * zoom 抖動。照 MapLibre 官方 three.js 範例的做法，把平移縮放併進
 * projection matrix（JS 端 double 精度），每台各自 render 一次。
 */
import maplibregl from "maplibre-gl";
import * as THREE from "three";

export interface DronePos {
  id: string;
  lat: number;
  lon: number;
  alt: number;
  color: string;
  radiusM?: number;
}

/** 識別用色（design-tokens v1 重定）：沿用圖表序列前 3 槽——原 6 色在新
    暖畫布驗證失敗（藍↔紫 protan ΔE 4.8），前 3 槽 all-pairs 通過。
    identity 與 status（綠/黃/橘/紅＝SINR 分級）依舊不互相冒充。 */
export const DRONE_PALETTE = ["#3987e5", "#d95926", "#199e70"];
const DRONE_OVERFLOW = "#8f8b80";   // 第 4 機起色相循環停止（=muted）：
  // 多台同灰，**顏色不再是識別依據**。識別由常駐機身文字承擔——機隊 chip
  // 與 3D 球體標籤（MapView 的 drone-labels TextLayer），ui-spec §0.1 硬規則。
  // （舊註解曾寫「識別靠常駐 sysid chip」，但該 chip 在「選中機統一」那批
  //  已移除、註解沒跟著改——紙上緩解讓人以為已處理而停止檢查，2026-08-12 修正）

// 機隊配色：依首次出現順序指派（主機先廣播 → 取第一色）。
// 球體、地面投影、選擇器圓點共用同一份對應，全站一致。
const _colorIdx = new Map<string, number>();
export function colorFor(id: string): string {
  if (!_colorIdx.has(id)) _colorIdx.set(id, _colorIdx.size);
  const i = _colorIdx.get(id)!;
  return i < DRONE_PALETTE.length ? DRONE_PALETTE[i] : DRONE_OVERFLOW;
}

interface Unit { scene: THREE.Scene; mat: THREE.MeshStandardMaterial }

/** 每台機在畫布上的位置與半徑（CSS px），render 時更新——
    自訂層不在 queryRenderedFeatures 的世界裡，點擊命中得自己投影。 */
export interface ScreenHit { x: number; y: number; r: number }

export function pickDrone(
  hits: Map<string, ScreenHit>, x: number, y: number, slopPx = 8,
): string | null {
  let best: string | null = null;
  let bestD = Infinity;
  for (const [id, h] of hits) {
    const d = Math.hypot(x - h.x, y - h.y);
    if (d <= h.r + slopPx && d < bestD) { best = id; bestD = d; }
  }
  return best;
}

export function createDroneLayer(
  layerId: string,
  getDrones: () => DronePos[],
  hits?: Map<string, ScreenHit>,
): maplibregl.CustomLayerInterface {
  let renderer: THREE.WebGLRenderer | null = null;
  let map: maplibregl.Map | null = null;
  const camera = new THREE.Camera();
  const units = new Map<string, Unit>();
  const geom = new THREE.SphereGeometry(1, 24, 16);

  function makeUnit(color: string): Unit {
    const scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const dir = new THREE.DirectionalLight(0xffffff, 1.6);
    dir.position.set(0.4, -0.6, 1);
    scene.add(dir);
    const mat = new THREE.MeshStandardMaterial({
      color, roughness: 0.35, metalness: 0.15,
    });
    scene.add(new THREE.Mesh(geom, mat));
    return { scene, mat };
  }

  return {
    id: layerId,
    type: "custom",
    renderingMode: "3d",
    onAdd(m, gl) {
      map = m;
      renderer = new THREE.WebGLRenderer({
        canvas: m.getCanvas(), context: gl, antialias: true,
      });
      renderer.autoClear = false;
    },
    render(_gl, args) {
      if (!renderer || !map) return;
      const matrix = (args as unknown as { defaultProjectionData?: { mainMatrix: number[] } })
        ?.defaultProjectionData?.mainMatrix ?? (args as unknown as number[]);
      const proj = new THREE.Matrix4().fromArray(matrix as number[]);

      const canvas = map.getCanvas();
      const cw = canvas.clientWidth, ch = canvas.clientHeight;
      const seen = new Set<string>();
      for (const d of getDrones()) {
        seen.add(d.id);
        let u = units.get(d.id);
        if (!u) { u = makeUnit(d.color); units.set(d.id, u); }
        const mc = maplibregl.MercatorCoordinate.fromLngLat([d.lon, d.lat], d.alt);
        let s = mc.meterInMercatorCoordinateUnits() * (d.radiusM ?? 3);

        // 縮放自適應（使用者第四輪＋第六輪定案）：**螢幕半徑只由 zoom 決定**
        // ——同縮放比例下旋轉/傾角/球在畫面何處都不改變大小。第一版夾限沿
        // 單軸量測會隨旋轉呼吸；第二版奇異值修了量測，但透視下球偏離旋轉
        // 中心時深度仍隨旋轉變、未夾限區間尺寸照樣變（使用者二次回報）。
        // 目標半徑改由 zoom 的地面 m/px 推得（旋轉不變），Jacobian 奇異值
        // 只用來把目標半徑準確換算回世界尺度
        const c4 = new THREE.Vector4(mc.x, mc.y, mc.z ?? 0, 1).applyMatrix4(proj);
        let cx = 0, cy = 0, r = 0;
        const visible = c4.w > 0;
        if (visible) {
          cx = (c4.x / c4.w + 1) / 2 * cw;
          cy = (1 - c4.y / c4.w) / 2 * ch;
          const px = (dx: number, dy: number, dz: number): [number, number] => {
            const v = new THREE.Vector4(mc.x + dx, mc.y + dy, (mc.z ?? 0) + dz, 1)
              .applyMatrix4(proj);
            return [(v.x / v.w + 1) / 2 * cw - cx, (1 - v.y / v.w) / 2 * ch - cy];
          };
          const cols = [px(s, 0, 0), px(0, s, 0), px(0, 0, s)];
          const a = cols.reduce((t, c) => t + c[0] * c[0], 0);
          const c2 = cols.reduce((t, c) => t + c[1] * c[1], 0);
          const b = cols.reduce((t, c) => t + c[0] * c[1], 0);
          const r0 = Math.sqrt(
            (a + c2) / 2 + Math.sqrt(((a - c2) / 2) ** 2 + b * b));
          // 目標半徑＝zoom 的地面 m/px（512-tile 尺度）換算實體半徑後夾限：
          // 只含 zoom 與緯度，旋轉/傾角/深度皆不影響
          const mppZoom = (78271.517 * Math.cos((d.lat * Math.PI) / 180))
            / Math.pow(2, map.getZoom());
          r = Math.min(16, Math.max(7, (d.radiusM ?? 3) / mppZoom));
          if (r0 > 0.01) s *= r / r0;   // 以目標螢幕半徑回算世界尺度
        }

        const model = new THREE.Matrix4()
          .makeTranslation(mc.x, mc.y, mc.z ?? 0)
          .scale(new THREE.Vector3(s, -s, s));   // Y 反向：mercator 與 three 的 Y 軸相反
        camera.projectionMatrix = proj.clone().multiply(model);
        renderer.resetState();
        renderer.render(u.scene, camera);

        if (hits) {
          if (visible) hits.set(d.id, { x: cx, y: cy, r });
          else hits.delete(d.id);   // 在相機後方
        }
      }
      for (const [k, u] of units) {
        if (!seen.has(k)) { u.mat.dispose(); units.delete(k); hits?.delete(k); }
      }
      map.triggerRepaint();
    },
  };
}
