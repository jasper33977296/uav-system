"use client";
/** 規劃頁的 3D 地形舞台（issues/048 F1 落地）。
 *
 * **地形是真的**：maplibre 的 `raster-dem` 吃我們自己從 `.hgt` 產的圖磚
 * （`GET /api/terrain-rgb/{z}/{x}/{y}.png`，terrarium 編碼）。原型那張
 * canvas 是手繪的線框，這裡是真高程、真座標、可平移可旋轉可縮放。
 *
 * **航線用 three.js 畫在真高度上。** maplibre 自己的 `line` 圖層會把線
 * 貼到地面上——那正好把這一頁唯一要傳達的東西（離地空間）弄不見。所以走
 * 自訂圖層：頂點直接用 mercator 座標，相機矩陣就是 maplibre 給的那一個，
 * **不做任何旋轉或縮放的轉換**——那類轉換寫錯的時候，畫面上看起來只是
 * 「有點歪」，而不是壞掉。
 */
import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";
import * as THREE from "three";

import { API } from "@/lib/signal";

export interface StageWp {
  seq: number; lat: number; lon: number;
  /** 規劃高度（AMSL，公尺）——由呼叫端換算好，這裡不猜高度基準 */
  amsl: number;
  /** 地面高程（AMSL）；null＝那一點沒有地形資料，不畫垂線 */
  ground: number | null;
  bad: boolean;
}

const BLUE = 0x3987e5, RED = 0xe05e5e, PICK = 0xd97757;

export default function TerrainStage({ wps, sel, onSelect, exaggeration = 1 }: {
  wps: StageWp[]; sel: number; onSelect: (i: number) => void;
  exaggeration?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const dataRef = useRef({ wps, sel });
  dataRef.current = { wps, sel };

  useEffect(() => {
    if (!box.current || mapRef.current) return;
    const first = wps.find((w) => w.lat && w.lon);
    const map = new maplibregl.Map({
      container: box.current,
      center: first ? [first.lon, first.lat] : [121.0459, 24.7734],
      zoom: 17, pitch: FIT_PITCH, maxPitch: 78, bearing: -28,
      // 滾輪縮放要按住 Ctrl：這一頁下面還有剖面與表格，捲頁比縮放常用
      cooperativeGestures: true,
      locale: {
        "CooperativeGesturesHandler.WindowsHelpText": "按住 Ctrl 並滾動以縮放",
        "CooperativeGesturesHandler.MacHelpText": "按住 ⌘ 並滾動以縮放",
      },
      style: {
        version: 8, sources: {}, layers: [
          { id: "canvas", type: "background", paint: { "background-color": "#1b1a17" } },
        ],
      },
      attributionControl: false,
    });
    mapRef.current = map;
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");

    map.on("load", () => {
      map.addSource("dem", {
        type: "raster-dem", tiles: [`${API}/api/terrain-rgb/{z}/{x}/{y}.png`],
        tileSize: 256, encoding: "terrarium", maxzoom: 15,
        // **這一區沒有 DEM 的時候端點回 404**，maplibre 會安靜地跳過那些
        // 圖磚——地面就會是平的。那不是 bug，但畫面上要說得出來，
        // 所以呼叫端拿得到 `onTerrainMiss`（見下）
      });
      map.setTerrain({ source: "dem", exaggeration });
      map.addLayer({
        id: "hillshade", type: "hillshade", source: "dem",
        // 這個場地的起伏只有兩公尺——陰影對比拉高一點才看得出地形的形狀，
        // 但**不動高程**：誇張的是光影，不是資料
        paint: { "hillshade-shadow-color": "#0e0d0b",
                 "hillshade-highlight-color": "#8a8474",
                 "hillshade-exaggeration": 0.9 },
      });
      map.addLayer(makeRouteLayer(map, dataRef));
      fitRoute(map, dataRef.current.wps);
    });

    // 點選最近的航點：three.js 的自訂圖層沒有 maplibre 的
    // `queryRenderedFeatures`，所以自己把航點投影回螢幕來比距離
    map.on("click", (e) => {
      const { wps: ws } = dataRef.current;
      let best = -1, bd = 26;
      ws.forEach((w, i) => {
        if (!w.lat || !w.lon) return;
        const p = map.project([w.lon, w.lat]);
        const d = Math.hypot(p.x - e.point.x, p.y - e.point.y);
        if (d < bd) { bd = d; best = i; }
      });
      if (best >= 0) onSelect(best);
    });
    return () => { map.remove(); mapRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { mapRef.current?.triggerRepaint(); }, [wps, sel]);

  return <div ref={box} className="stage3d" />;
}

/** 把鏡頭對準整條航線。
 *
 * **先用俯視算取景，再套回俯角**：maplibre 帶 pitch 的 `fitBounds` 算得
 * 極保守（它把傾斜後掃到的整片地都算進去），直接用會把航線縮成畫面角落的
 * 一小撮——第一版就是這樣，地形填滿整個畫面而航線只有指甲大。
 * 這個作法與 `FieldMap` 一致。
 */
//: 起始俯角。**壓得比一般 3D 地圖低**（即時頁是 55）：帶地形的鏡頭在
//: 大俯角下視線會擦過前方的坡，螢幕正中央顯示的是那個近處坡頂，
//: 真正的中心點被推到畫面上方——這個場地航線走在 121–123 m 的平帶上，
//: 而 200 m 內的地形升到 147 m，擦得很嚴重。使用者要看的是航線，
//: 不是地平線；轉到哪個角度是他自己的事（拖曳可以自由調）。
const FIT_PITCH = 36;

function fitRoute(map: maplibregl.Map, wps: StageWp[]) {
  const pts = wps.filter((w) => w.lat && w.lon);
  if (pts.length < 2) return;
  const b = new maplibregl.LngLatBounds();
  pts.forEach((w) => b.extend([w.lon, w.lat]));
  const bearing = map.getBearing();
  map.setPitch(0);
  const cam = map.cameraForBounds(b, { padding: 70, bearing });
  map.setPitch(FIT_PITCH);
  // **鏡頭要高過周圍的地。** 這個場地航線走在 121–123 m 的平帶上，而 200 m
  // 內的地形升到 147 m——貼著地面看過去，那 25 m 的坡會把整條航線擋掉
  // （第一版就是這樣：地形滿版、航線完全不見）。退一格再壓低俯角。
  if (cam) map.jumpTo({ center: cam.center, zoom: (cam.zoom ?? 17) - 1.1,
                        pitch: FIT_PITCH, bearing });
}

/** three.js 自訂圖層：航線畫在真高度上，每個航點往地面垂一根線。 */
function makeRouteLayer(map: maplibregl.Map,
                        dataRef: { current: { wps: StageWp[]; sel: number } }):
                        maplibregl.CustomLayerInterface {
  let renderer: THREE.WebGLRenderer;
  const camera = new THREE.Camera();
  const scene = new THREE.Scene();
  scene.add(new THREE.AmbientLight(0xffffff, 2.2));
  let ref = maplibregl.MercatorCoordinate.fromLngLat([0, 0], 0);
  let group = new THREE.Group();
  scene.add(group);

  const rebuild = () => {
    group.clear();
    const { wps, sel } = dataRef.current;
    const pts = wps.filter((w) => w.lat && w.lon);
    if (!pts.length) return;
    ref = maplibregl.MercatorCoordinate.fromLngLat([pts[0].lon, pts[0].lat], 0);
    const mScale = ref.meterInMercatorCoordinateUnits();
    const v = (lon: number, lat: number, amsl: number) => {
      const m = maplibregl.MercatorCoordinate.fromLngLat([lon, lat], amsl);
      return new THREE.Vector3(m.x - ref.x, m.y - ref.y, m.z - ref.z);
    };
    // 航段：粗細用公尺算，縮放時看起來才是同一條線
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      const curve = new THREE.LineCurve3(v(a.lon, a.lat, a.amsl), v(b.lon, b.lat, b.amsl));
      const geo = new THREE.TubeGeometry(curve, 1, 0.9 * mScale, 6, false);
      group.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
        color: b.bad ? RED : BLUE })));
    }
    // 垂線＝離地空間。**沒有地形資料就不畫**，畫一根到 0 m 的線是騙人
    pts.forEach((w, i) => {
      if (w.ground == null) return;
      const curve = new THREE.LineCurve3(v(w.lon, w.lat, w.amsl), v(w.lon, w.lat, w.ground));
      group.add(new THREE.Mesh(
        new THREE.TubeGeometry(curve, 1, 0.35 * mScale, 5, false),
        new THREE.MeshBasicMaterial({ color: w.bad ? RED : BLUE,
          transparent: true, opacity: 0.55 })));
      const s = new THREE.Mesh(
        new THREE.SphereGeometry((i === dataRef.current.sel ? 1.7 : 1.1) * mScale, 12, 8),
        new THREE.MeshBasicMaterial({ color: i === sel ? PICK : (w.bad ? RED : BLUE) }));
      s.position.copy(v(w.lon, w.lat, w.amsl));
      group.add(s);
    });
  };

  return {
    id: "route3d", type: "custom", renderingMode: "3d",
    onAdd(_m, gl) {
      renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl });
      renderer.autoClear = false;
      rebuild();
    },
    render(_gl, args: unknown) {
      rebuild();
      // **頂點就是 mercator 座標（相對第一個航點）**，所以相機矩陣只要
      // 把 maplibre 給的那一個再平移回去就好——不做旋轉、不做縮放。
      // 那類轉換寫錯時畫面只是「有點歪」，而不是壞掉，最難發現
      const mat = (args as { defaultProjectionData?: { mainMatrix: number[] } })
        ?.defaultProjectionData?.mainMatrix ?? (args as number[]);
      camera.projectionMatrix = new THREE.Matrix4().fromArray(mat as number[])
        .multiply(new THREE.Matrix4().makeTranslation(ref.x, ref.y, ref.z));
      renderer.resetState();
      renderer.render(scene, camera);
      map.triggerRepaint();
    },
  };
}
