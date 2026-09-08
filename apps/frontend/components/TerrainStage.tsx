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
import { useEffect, useRef, useState } from "react";
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

const BLUE = 0x3987e5, RED = 0xe05e5e, PICK = 0xd97757, HOT = 0xf0eee6;

/** 滑鼠指到的東西。`kind:"leg"` 的 `i` 是「第 i 段」＝ wps[i-1] → wps[i]。 */
export interface StageHit { kind: "wp" | "leg"; i: number }
/** 這一格要顯示什麼由**呼叫端**決定：航段的長度、速度、來源、判定都住在
 *  規劃頁上，讓這個元件再查一次就會有兩份可能不同步的資料。 */
export interface StageTip { title: string; rows: [string, string][]; bad?: boolean }

export default function TerrainStage({ wps, sel, onSelect, tipFor,
                                      exaggeration = 1 }: {
  wps: StageWp[]; sel: number; onSelect: (i: number) => void;
  tipFor?: (h: StageHit) => StageTip | null;
  exaggeration?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [tip, setTip] = useState<{ t: StageTip; x: number; y: number } | null>(null);
  const dataRef = useRef({ wps, sel, hover: null as StageHit | null, dirty: true });
  if (dataRef.current.wps !== wps || dataRef.current.sel !== sel) {
    dataRef.current.dirty = true;
  }
  dataRef.current.wps = wps;
  dataRef.current.sel = sel;
  const tipRef = useRef(tipFor);
  tipRef.current = tipFor;
  const projRef = useRef<Projector | null>(null);

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
      map.addLayer(makeRouteLayer(map, dataRef, (p) => { projRef.current = p; }));
      fitRoute(map, dataRef.current.wps);
      // 先粗估一次鏡頭，再**量畫面上實際落在哪裡**去修（見 frameRoute）
      map.once("render", () => frameRoute(map, dataRef, projRef));
    });

    /* three.js 的自訂圖層沒有 maplibre 的 `queryRenderedFeatures`，
       所以自己把航點與航段投影回螢幕來比距離。**航點優先於航段**：
       兩者重疊時人要點的幾乎一定是航點。 */
    const hitTest = (pt: { x: number; y: number }): StageHit | null => {
      const ws = dataRef.current.wps;
      const proj = projRef.current;
      if (!proj) return null;
      const at = (w: StageWp) => proj(w.lon, w.lat, w.amsl);
      for (let i = 0; i < ws.length; i++) {
        if (!ws[i].lat || !ws[i].lon) continue;
        const p = at(ws[i]);
        if (p && Math.hypot(p.x - pt.x, p.y - pt.y) < 14) return { kind: "wp", i };
      }
      for (let i = 1; i < ws.length; i++) {
        const a = at(ws[i - 1]), b = at(ws[i]);
        if (!a || !b) continue;
        const dx = b.x - a.x, dy = b.y - a.y;
        const t = Math.max(0, Math.min(1,
          ((pt.x - a.x) * dx + (pt.y - a.y) * dy) / (dx * dx + dy * dy || 1)));
        if (Math.hypot(pt.x - (a.x + dx * t), pt.y - (a.y + dy * t)) < 9)
          return { kind: "leg", i };
      }
      return null;
    };
    map.on("click", (e) => {
      const h = hitTest(e.point);
      if (h) onSelect(h.kind === "wp" ? h.i : h.i);
    });
    map.on("mousemove", (e) => {
      const h = hitTest(e.point);
      const prev = dataRef.current.hover;
      if (JSON.stringify(h) !== JSON.stringify(prev)) {
        dataRef.current.hover = h;
        dataRef.current.dirty = true;          // 換顏色要重建一次
        map.getCanvas().style.cursor = h ? "pointer" : "";
      }
      const t = h && tipRef.current ? tipRef.current(h) : null;
      setTip(t ? { t, x: e.point.x, y: e.point.y } : null);
    });
    map.on("mouseout", () => {
      dataRef.current.hover = null; dataRef.current.dirty = true;
      map.getCanvas().style.cursor = ""; setTip(null);
    });
    return () => { map.remove(); mapRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { dataRef.current.dirty = true; mapRef.current?.triggerRepaint(); },
    [wps, sel]);

  return (
    <div className="stage3d-wrap">
      <div ref={box} className="stage3d" />
      {tip && (
        <div className="stage-tip"
          style={{ left: Math.min(tip.x + 14, 9999), top: Math.max(tip.y - 8, 4) }}>
          <b>{tip.t.title}</b>
          {tip.t.rows.map(([k, v]) => (
            <div key={k}><span className="k">{k}</span>{v}</div>
          ))}
          {tip.t.bad && <div className="bad">低空帶速：擋上傳</div>}
        </div>
      )}
    </div>
  );
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

/** 取景的第二步：**量出航線在畫面上實際落在哪裡**，修一次，沒變好就退回去。
 *
 * 為什麼需要第二步：帶地形又帶俯角時，maplibre 的「中心」是地面上的一個點，
 * 而航線畫在一百多公尺高——`cameraForBounds` 算出來的鏡頭會把航線推到畫面
 * 上方。與其推導那個偏差，不如量它。
 *
 * **為什麼不迭代**：第一版寫成「追著誤差修到收斂」，結果整個畫面飛掉。
 * 原因是帶俯角時「平移 N 像素」與「畫面上移動 N 像素」**不是線性關係**，
 * 追著跑會過衝、然後發散。改成：量一次、帶阻尼修一次、再量一次，
 * **沒有變好就把鏡頭還原**。修不好的時候讓使用者自己拖，比把畫面弄丟好。
 */
function frameRoute(map: maplibregl.Map, dataRef: { current: StageData },
                    projRef: { current: Projector | null }) {
  const measure = () => {
    const proj = projRef.current;
    if (!proj) return null;
    const pts = dataRef.current.wps
      .filter((w) => w.lat && w.lon)
      .map((w) => proj(w.lon, w.lat, w.amsl))
      .filter((p): p is { x: number; y: number } => !!p);
    if (pts.length < 2) return null;
    const cvs = map.getCanvas(), W = cvs.clientWidth, H = cvs.clientHeight;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = Math.min(...ys), y1 = Math.max(...ys);
    return {
      dx: (x0 + x1) / 2 - W / 2, dy: (y0 + y1) / 2 - H / 2,
      err: Math.hypot((x0 + x1) / 2 - W / 2, (y0 + y1) / 2 - H / 2),
  // 航線佔畫面四成（使用者 2026-09-08 從六成調下來）：留白是給地形看的
      // ——一開頁就要看得出「航線在什麼地形裡」，那是這一頁的重點之一
      want: Math.min(W * 0.4 / Math.max(1, x1 - x0),
                     H * 0.4 / Math.max(1, y1 - y0)),
    };
  };
  /** 一個分數同時管「置中」與「大小」——只看其中一個會出現
   *  「置中了但塞爆畫面」這種結果。 */
  const score = (m: NonNullable<ReturnType<typeof measure>>) =>
    m.err / 200 + Math.abs(Math.log2(m.want));

  const m0 = measure();
  if (!m0) return;
  let best = { center: map.getCenter(), zoom: map.getZoom(), s: score(m0) };
  let step = 0.9;

  /** **有護欄的搜尋**：每一步只有分數變好才留下，否則退回上一個最好的
   *  並把步長減半。
   *
   *  為什麼不用開環修正（前兩版都試過，都歪）：帶俯角時「平移 N 像素」
   *  與「畫面上移動 N 像素」不是線性關係，**縮放也不是**——把東西移到
   *  畫面中央本身就會讓它看起來變大（離鏡頭變近）。追著誤差一次算完，
   *  第一版直接把畫面飛掉，第二版置中了卻塞爆。
   *
   *  這裡最壞情況是「沒改善、退回原狀」，而那正是可接受的下限：
   *  修不好就讓使用者自己拖。 */
  const tryStep = (iter: number) => {
    if (iter > 6 || step < 0.12 || best.s < 0.12) return;
    const m = measure();
    if (!m) return;
    map.panBy([m.dx * step, m.dy * step], { duration: 0 });
    const dz = Math.max(-1.5, Math.min(1.5, Math.log2(m.want))) * step;
    if (Math.abs(dz) >= 0.02)
      map.setZoom(Math.max(13, Math.min(19, map.getZoom() + dz)));
    map.once("render", () => {
      const m2 = measure();
      const s2 = m2 ? score(m2) : Infinity;
      if (s2 < best.s) {
        best = { center: map.getCenter(), zoom: map.getZoom(), s: s2 };
      } else {
        map.jumpTo({ center: best.center, zoom: best.zoom,
                     pitch: map.getPitch(), bearing: map.getBearing() });
        step *= 0.5;
      }
      map.once("render", () => tryStep(iter + 1));
    });
  };
  tryStep(0);
}

/** three.js 自訂圖層：航線畫在真高度上，每個航點往地面垂一根線。 */
interface StageData { wps: StageWp[]; sel: number; hover: StageHit | null; dirty: boolean }

/** 用**畫圖用的那個矩陣**把 (lon, lat, 海拔) 投影回螢幕像素。
 *
 * **不能用 `map.project`**：它回的是那個經緯度在**海平面**的位置，而航線畫在
 * 一百多公尺高——實測差了 160 px，等於「要指的地方」跟「眼睛看到的地方」
 * 對不上。用同一個矩陣算，才保證命中測試與畫面是同一件事。
 */
type Projector = (lon: number, lat: number, amsl: number) =>
  { x: number; y: number } | null;

function makeRouteLayer(map: maplibregl.Map, dataRef: { current: StageData },
                        setProjector: (p: Projector) => void):
                        maplibregl.CustomLayerInterface {
  let renderer: THREE.WebGLRenderer;
  const camera = new THREE.Camera();
  const scene = new THREE.Scene();
  scene.add(new THREE.AmbientLight(0xffffff, 2.2));
  let ref = maplibregl.MercatorCoordinate.fromLngLat([0, 0], 0);
  let group = new THREE.Group();
  scene.add(group);

  const rebuild = () => {
    // **只在資料變動時重建。** 原本每一幀都重新配置 TubeGeometry——
    // 一秒六十次的幾何配置，在這種只會偶爾改一個數字的畫面上毫無理由
    if (!dataRef.current.dirty) return;
    dataRef.current.dirty = false;
    group.clear();
    const { wps, sel, hover } = dataRef.current;
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
      const hot = hover?.kind === "leg" && hover.i === i;
      group.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
        color: hot ? HOT : b.bad ? RED : BLUE })));
    }
    // 垂線＝離地空間。**沒有地形資料就不畫**，畫一根到 0 m 的線是騙人
    pts.forEach((w, i) => {
      if (w.ground == null) return;
      const curve = new THREE.LineCurve3(v(w.lon, w.lat, w.amsl), v(w.lon, w.lat, w.ground));
      group.add(new THREE.Mesh(
        new THREE.TubeGeometry(curve, 1, 0.35 * mScale, 5, false),
        new THREE.MeshBasicMaterial({ color: w.bad ? RED : BLUE,
          transparent: true, opacity: 0.55 })));
      const hot = hover?.kind === "wp" && hover.i === i;
      const s = new THREE.Mesh(
        new THREE.SphereGeometry((i === sel || hot ? 1.7 : 1.1) * mScale, 12, 8),
        new THREE.MeshBasicMaterial({
          color: i === sel ? PICK : hot ? HOT : w.bad ? RED : BLUE }));
      s.position.copy(v(w.lon, w.lat, w.amsl));
      group.add(s);
    });
  };

  return {
    id: "route3d", type: "custom", renderingMode: "3d",
    onAdd(_m, gl) {
      renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl });
      renderer.autoClear = false;
      dataRef.current.dirty = true;
      rebuild();
    },
    render(_gl, args: unknown) {
      rebuild();
      // **頂點就是 mercator 座標（相對第一個航點）**，所以相機矩陣只要
      // 把 maplibre 給的那一個再平移回去就好——不做旋轉、不做縮放。
      // 那類轉換寫錯時畫面只是「有點歪」，而不是壞掉，最難發現
      const mat = (args as { defaultProjectionData?: { mainMatrix: number[] } })
        ?.defaultProjectionData?.mainMatrix ?? (args as number[]);
      const full = new THREE.Matrix4().fromArray(mat as number[]);
      camera.projectionMatrix = full.clone()
        .multiply(new THREE.Matrix4().makeTranslation(ref.x, ref.y, ref.z));
      // 命中測試要用同一個矩陣（見 `Projector` 的說明）
      setProjector((lon, lat, amsl) => {
        const m = maplibregl.MercatorCoordinate.fromLngLat([lon, lat], amsl);
        const v = new THREE.Vector4(m.x, m.y, m.z, 1).applyMatrix4(full);
        if (v.w <= 0) return null;                    // 在鏡頭後面
        const cvs = map.getCanvas();
        const w = cvs.clientWidth, h = cvs.clientHeight;
        return { x: (v.x / v.w * 0.5 + 0.5) * w, y: (0.5 - v.y / v.w * 0.5) * h };
      });
      renderer.resetState();
      renderer.render(scene, camera);
      map.triggerRepaint();
    },
  };
}
