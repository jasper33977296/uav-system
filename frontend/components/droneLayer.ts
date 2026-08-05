/** 無人機 3D 球體層：three.js custom layer。
 *
 * MapLibre 的 fill-extrusion 只能做柱體，真正的球體需要自帶 WebGL 場景。
 * 本層從一開始就是**多機結構**：以 drone_id 為鍵維護球體，getDrones() 回傳
 * 幾台就畫幾台——群飛時 backend 擴成多機（doc/architecture.md 的
 * dict[drone_id, LiveState]），這裡不需要任何改動。
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

/** 多機用色：第 1 台用系列藍，其後依序取用（色相彼此遠離） */
export const DRONE_PALETTE = ["#3987e5", "#c65fd1", "#2fb2a5", "#d1975f"];

// 機隊配色：依首次出現順序指派（主機先廣播 → 取第一色）。
// 球體、地面投影、選擇器圓點共用同一份對應，全站一致。
const _colorIdx = new Map<string, number>();
export function colorFor(id: string): string {
  if (!_colorIdx.has(id)) _colorIdx.set(id, _colorIdx.size);
  return DRONE_PALETTE[_colorIdx.get(id)! % DRONE_PALETTE.length];
}

interface Unit { scene: THREE.Scene; mat: THREE.MeshStandardMaterial }

export function createDroneLayer(
  layerId: string,
  getDrones: () => DronePos[],
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

      const seen = new Set<string>();
      for (const d of getDrones()) {
        seen.add(d.id);
        let u = units.get(d.id);
        if (!u) { u = makeUnit(d.color); units.set(d.id, u); }
        const mc = maplibregl.MercatorCoordinate.fromLngLat([d.lon, d.lat], d.alt);
        const s = mc.meterInMercatorCoordinateUnits() * (d.radiusM ?? 3);
        const model = new THREE.Matrix4()
          .makeTranslation(mc.x, mc.y, mc.z ?? 0)
          .scale(new THREE.Vector3(s, -s, s));   // Y 反向：mercator 與 three 的 Y 軸相反
        camera.projectionMatrix = proj.clone().multiply(model);
        renderer.resetState();
        renderer.render(u.scene, camera);
      }
      for (const [k, u] of units) {
        if (!seen.has(k)) { u.mat.dispose(); units.delete(k); }
      }
      map.triggerRepaint();
    },
  };
}
