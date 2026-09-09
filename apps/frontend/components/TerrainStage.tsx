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
  /** 起飛點：位置是解鎖的地方，不由規劃決定，所以拖不動 */
  fixed?: boolean;
  /** 這一點是什麼。**由後端給**（`route_profile` 的 `kind`），不要在這裡猜 */
  kind?: "takeoff" | "wp" | "land";
  /** 系統補的（中繼點、進場點），不是操作員放的 */
  auto?: boolean;
}

const BLUE = 0x3987e5, RED = 0xe05e5e, PICK = 0xd97757, HOT = 0xf0eee6;
/** **綠色＝會接地的點**（起飛與降落是同一類事），形狀分是哪一種。
 *  不用橘色：橘在這套系統裡是互動 chrome 與「假設高度」的顏色，會撞。 */
const GROUND_PT = 0x0ca30c;

/** 滑鼠指到的東西。`kind:"leg"` 的 `i` 是「第 i 段」＝ wps[i-1] → wps[i]。 */
export interface StageHit { kind: "wp" | "leg"; i: number }
/** 這一格要顯示什麼由**呼叫端**決定：航段的長度、速度、來源、判定都住在
 *  規劃頁上，讓這個元件再查一次就會有兩份可能不同步的資料。 */
export interface StageTip { title: string; rows: [string, string][]; bad?: boolean }

export default function TerrainStage({ wps, sel, onSelect, tipFor, placing,
                                      onPlace, onMove, center, assumeM = null,
                                      onBuildings, exaggeration = 1 }: {
  wps: StageWp[]; sel: number; onSelect: (i: number) => void;
  tipFor?: (h: StageHit) => StageTip | null;
  /** 放點模式：點地形＝加一個航點（maplibre 自己有 3px 的 clickTolerance，
   *  所以拖曳轉視角不會誤放） */
  placing?: boolean;
  onPlace?: (lngLat: { lng: number; lat: number }) => void;
  /** 拖曳航點：**只移動位置**，高度由右欄的滑桿或數字決定 */
  onMove?: (i: number, lngLat: { lng: number; lat: number }) => void;
  center?: [number, number];
  /** 未量測建物的假設高度。null ＝不假設，那時柱子畫成「一定包住航線」
   *  的高度——讀出來是「這裡有東西」，不是某個公尺數 */
  assumeM?: number | null;
  /** 沿線的建物清單（含長寬高）。**規劃頁不自己再查一次**——那就會有
   *  兩份可能不同步的資料（§9-F） */
  onBuildings?: (bs: BuildingFeat[]) => void;
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
  const assumeRef = useRef<number | null>(assumeM);
  assumeRef.current = assumeM;
  const moveRef = useRef(onMove);
  moveRef.current = onMove;
  const dragRef = useRef<number | null>(null);
  const placeRefBox = useRef<{ current: {
    placing?: boolean; onPlace?: (l: { lng: number; lat: number }) => void } } | null>(null);
  if (placeRefBox.current) placeRefBox.current.current = { placing, onPlace };

  /** **每次改線就重建**：範圍跟著航線走，所以線一動要重問一次。
   *  去抖——拖一個點會產生幾十次變動。 */
  useEffect(() => {
    const t = setTimeout(async () => {
      const m = mapRef.current;
      if (!m || !m.isStyleLoaded()) return;
      const got = await fetchNear(wps);
      if (!got || !mapRef.current) return;
      paintBuildings(m, got.fc, blindHeight(wps, assumeM));
      onBuildings?.(got.list);
    }, 320);
    return () => clearTimeout(t);
  }, [wps, assumeM, onBuildings]);

  useEffect(() => {
    if (!box.current || mapRef.current) return;
    const first = wps.find((w) => w.lat && w.lon);
    const placeRef = { current: { placing, onPlace } };
    placeRefBox.current = placeRef;
    const map = new maplibregl.Map({
      container: box.current,
      center: center ?? (first ? [first.lon, first.lat] : [121.0459, 24.7734]),
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
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");

    map.on("load", () => {
      map.addSource("dem", {
        type: "raster-dem", tiles: [`${API}/api/terrain-rgb/{z}/{x}/{y}.png`],
        tileSize: 256, encoding: "terrarium", maxzoom: 15,
        // **這一區沒有 DEM 的時候端點回 404**，maplibre 會安靜地跳過那些
        // 圖磚——地面就會是平的。那不是 bug，但畫面上要說得出來，
        // 所以呼叫端拿得到 `onTerrainMiss`（見下）
      });
      map.setTerrain({ source: "dem", exaggeration });
      // 正射影像（NLSC PHOTO2）。走自己的端點而不是直連 NLSC：現場離線，
      // 圖磚要能從 data/ortho 供出來（scripts/fetch-ortho.py 先抓）
      map.addSource("ortho", {
        type: "raster", tiles: [`${API}/api/ortho/{z}/{x}/{y}.jpg`],
        tileSize: 256, maxzoom: 19,
        attribution: "© 內政部國土測繪中心",
      });
      map.addLayer({ id: "ortho", type: "raster", source: "ortho",
        paint: { "raster-opacity": 0.9 } });
      map.addLayer({
        id: "hillshade", type: "hillshade", source: "dem",
        // 影像蓋上去之後陰影只用來讓地形的起伏還看得出來，不搶戲
        maxzoom: 22,
        // 這個場地的起伏只有兩公尺——陰影對比拉高一點才看得出地形的形狀，
        // 但**不動高程**：誇張的是光影，不是資料
        paint: { "hillshade-shadow-color": "#0e0d0b",
                 "hillshade-highlight-color": "#8a8474",
                 "hillshade-exaggeration": 0.35 },
      });

      map.addLayer(makeRouteLayer(map, dataRef, (p) => { projRef.current = p; }));
      fitRoute(map, dataRef.current.wps);
      // 先粗估一次鏡頭，再**量畫面上實際落在哪裡**去修（見 frameRoute）
      // **只在開頁時取景一次**：放點模式下每加一個點就重新取景，
      // 畫面會在使用者手底下跳
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
      const pl = placeRefBox.current?.current;
      if (pl?.placing && pl.onPlace) { pl.onPlace(e.lngLat); return; }
      const h = hitTest(e.point);
      if (h) onSelect(h.i);
    });
    map.on("mousedown", (e) => {
      const h = hitTest(e.point);
      if (h?.kind === "wp" && !dataRef.current.wps[h.i]?.fixed && moveRef.current) {
        dragRef.current = h.i;
        map.dragPan.disable(); map.dragRotate.disable();
        onSelect(h.i);
        e.preventDefault();
      }
    });
    const endDrag = () => {
      if (dragRef.current == null) return;
      dragRef.current = null;
      map.dragPan.enable(); map.dragRotate.enable();
    };
    map.on("mouseup", endDrag);
    map.on("dragend", endDrag);
    map.on("mousemove", (e) => {
      if (dragRef.current != null) {
        moveRef.current?.(dragRef.current, e.lngLat);
        return;
      }
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
    <div className={`stage3d-wrap${placing ? " placing" : ""}`}>
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
 * `map.project` 只吃經緯度、拿不到高度，所以命中測試一定要走這裡——
 * 用同一個矩陣算，才保證「要指的地方」跟「眼睛看到的地方」是同一件事。
 */
/** 建物：有量過高度的拉成實體，**沒量過的是另一種東西**。
 *
 * 沒量過的不能畫成一個高度——那等於替它猜一個數字，而猜到的與量到的
 * 是兩件事（doc/field-3d-model-design.md §9-A）。所以它是一根半透明的
 * 橘色柱子，**高度取「這條航線最高點再加一截」**：它一定包住航線，
 * 讀出來的是「這裡有東西、你飛不過去」，而不是某個公尺數。
 *
 * **範圍跟著航線走**（使用者裁定 2026-09-09）：只建離線 `BUFFER_M` 以內的，
 * 而且**每次改線就重建**——固定方框會把根本不會飛過去的整排樓也建出來。
 */
const BLIND_OVER_M = 25;
const BUFFER_M = 30;

function blindHeight(wps: StageWp[], assume: number | null): number {
  if (assume != null) return assume;
  const pts = wps.filter((w) => w.lat && w.lon);
  if (!pts.length) return BLIND_OVER_M;
  return Math.max(...pts.map(
    (w) => (w.ground == null ? 0 : w.amsl - w.ground)), 0) + BLIND_OVER_M;
}

export interface BuildingFeat {
  id: string; name: string | null; kind: string;
  height_m: number | null; height_source: string; known: boolean;
  dist_m: number; length_m: number | null; width_m: number | null;
  area_m2: number | null; bearing_deg: number | null;
}

async function fetchNear(wps: StageWp[]): Promise<{ fc: unknown; list: BuildingFeat[] } | null> {
  const pts = wps.filter((w) => w.lat && w.lon).map((w) => [w.lat, w.lon]);
  if (pts.length < 1) return null;
  const r = await fetch(`${API}/api/buildings/near`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ points: pts, buffer_m: BUFFER_M }),
  });
  if (!r.ok) return null;
  const fc = await r.json();
  return { fc, list: (fc.features ?? []).map((f: { properties: BuildingFeat }) => f.properties) };
}

function paintBuildings(map: maplibregl.Map, fc: unknown, blindH: number) {
  const before = map.getLayer("route3d") ? "route3d" : undefined;
  const src = map.getSource("buildings") as maplibregl.GeoJSONSource | undefined;
  if (src) { src.setData(fc as GeoJSON.FeatureCollection); return; }
  map.addSource("buildings", { type: "geojson", data: fc as GeoJSON.FeatureCollection });
  map.addLayer({
    id: "buildings", type: "fill-extrusion", source: "buildings",
    filter: ["==", ["get", "known"], true],
    paint: {
      "fill-extrusion-height": ["get", "height_m"], "fill-extrusion-base": 0,
      "fill-extrusion-color": "#8d8579", "fill-extrusion-opacity": 0.85,
    },
  }, before);
  map.addLayer({
    id: "buildings-blind", type: "fill-extrusion", source: "buildings",
    filter: ["==", ["get", "known"], false],
    paint: {
      "fill-extrusion-height": blindH, "fill-extrusion-base": 0,
      "fill-extrusion-color": "#c98a2b", "fill-extrusion-opacity": 0.35,
    },
  }, before);
}

/** 畫面中心的地面高度：地形開著的時候，相機矩陣的原點就在這個高度上。 */
function camElevM(map: maplibregl.Map): number {
  const e = (map as unknown as { transform?: { elevation?: number } })
    .transform?.elevation;
  return typeof e === "number" ? e : 0;
}

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
      const at = v(w.lon, w.lat, w.amsl);
      // **三種點要一眼分得出來**（使用者 2026-09-09）：分不出來時
      // 「放一個點卻有線」看起來像 bug，其實那條線是起飛點連過去的。
      // `auto` 的（中繼點、進場點）畫小一點：它們是真的航點，但不是人放的
      const big = i === sel || hot;
      const col = i === sel ? PICK : hot ? HOT : w.bad ? RED : BLUE;
      const gcol = i === sel ? PICK : hot ? HOT : GROUND_PT;
      const r = (big ? 1.7 : w.auto ? 0.7 : 1.1) * mScale;
      if (w.kind === "takeoff") {
        // 空心環 ＋ 地面十字。**環是空的**：那個形狀順便說它拖不動
        const ring = new THREE.Mesh(
          new THREE.TorusGeometry(r * 1.5, r * 0.42, 6, 20),
          new THREE.MeshBasicMaterial({ color: gcol }));
        ring.position.copy(at);
        group.add(ring);
        if (w.ground != null) {
          const g0 = v(w.lon, w.lat, w.ground);
          const arm = r * 2.6;
          for (const [dx, dy] of [[1, 0], [0, 1]] as const) {
            const a = g0.clone(), b2 = g0.clone();
            a.x -= arm * dx; a.y -= arm * dy;
            b2.x += arm * dx; b2.y += arm * dy;
            group.add(new THREE.Mesh(
              new THREE.TubeGeometry(new THREE.LineCurve3(a, b2), 1, r * 0.3, 4, false),
              new THREE.MeshBasicMaterial({ color: gcol })));
          }
        }
      } else if (w.kind === "land") {
        // 向下三角＝往這裡下來
        const cone = new THREE.Mesh(
          new THREE.ConeGeometry(r * 1.5, r * 3, 4),
          new THREE.MeshBasicMaterial({ color: gcol }));
        cone.rotation.x = Math.PI;          // 尖端朝下
        cone.position.copy(at);
        group.add(cone);
      } else {
        const sp = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 8),
          new THREE.MeshBasicMaterial({ color: col }));
        sp.position.copy(at);
        group.add(sp);
      }
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
      // **開了地形之後，矩陣裡的 z=0 是「畫面中心的地面高度」，不是海平面。**
      // 直接餵海拔會讓整條航線浮在地圖上方 `transform.elevation` 公尺
      // ——本場域 123 m，在 z17 就是一百六十個像素，整條線看起來像飄的。
      // 中心高度會隨著平移改變，所以每一幀扣，不是建幾何時扣
      const dz = camElevM(map) * ref.meterInMercatorCoordinateUnits();
      camera.projectionMatrix = full.clone()
        .multiply(new THREE.Matrix4().makeTranslation(ref.x, ref.y, ref.z - dz));
      // 命中測試要用同一個矩陣（見 `Projector` 的說明）
      setProjector((lon, lat, amsl) => {
        const m = maplibregl.MercatorCoordinate.fromLngLat([lon, lat], amsl);
        const v = new THREE.Vector4(m.x, m.y, m.z - dz, 1).applyMatrix4(full);
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
