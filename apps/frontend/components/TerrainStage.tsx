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
  /** 這一點是操作員的第幾個點（後端給的 `src_i`）。null＝系統補的，
   *  或起飛點。**拖曳與逐點編輯都認它**——用畫面上的位置去數會數錯 */
  srcI?: number | null;
}

const BLUE = 0x3987e5, RED = 0xe05e5e, PICK = 0xd97757, HOT = 0xf0eee6;
/** **綠色＝會接地的點**（起飛與降落是同一類事），形狀分是哪一種。
 *  不用橘色：橘在這套系統裡是互動 chrome 與「假設高度」的顏色，會撞。 */
const GROUND_PT = 0x0ca30c;

/** 畫面上畫的圍欄。**規劃端的約束**——飛控不照它擋，那句話由呼叫端說。 */
export interface FenceShape {
  shape: "circle" | "polygon";
  /** circle：圓心（起飛點）與半徑 */
  center?: [number, number];      // [lat, lon]
  radius_m?: number;
  /** polygon：[[lat, lon], …] */
  points?: [number, number][];
}

/** 滑鼠指到的東西。`kind:"leg"` 的 `i` 是「第 i 段」＝ wps[i-1] → wps[i]。 */
export interface StageHit { kind: "wp" | "leg"; i: number }
/** 這一格要顯示什麼由**呼叫端**決定：航段的長度、速度、來源、判定都住在
 *  規劃頁上，讓這個元件再查一次就會有兩份可能不同步的資料。 */
export interface StageTip { title: string; rows: [string, string][]; bad?: boolean }

export default function TerrainStage({ wps, sel, onSelect, tipFor, placing,
                                      onPlace, onMove, center, assumeM = null,
                                      onBuildings, fence = null, flyTo = null,
                                      fenceSel = -1, onFenceSelect, onFenceMove,
                                      exaggeration = 1 }: {
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
  fence?: FenceShape | null;
  /** 選中的圍欄頂點（多邊形）。-1＝沒有 */
  fenceSel?: number;
  onFenceSelect?: (i: number) => void;
  /** 拖曳圍欄頂點：跟航點一樣只動位置 */
  onFenceMove?: (i: number, lngLat: { lng: number; lat: number }) => void;
  /** 地址定位的結果。**只在 `n` 變的時候飛過去**——放點、拖點時不動鏡頭 */
  flyTo?: { lat: number; lon: number; n: number } | null;
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
  const fenceRef = useRef({ onFenceSelect, onFenceMove });
  fenceRef.current = { onFenceSelect, onFenceMove };
  const fenceDragRef = useRef<number | null>(null);
  const placeRefBox = useRef<{ current: {
    placing?: boolean; onPlace?: (l: { lng: number; lat: number }) => void } } | null>(null);
  if (placeRefBox.current) placeRefBox.current.current = { placing, onPlace };

  /** **每次改線就重建**：範圍跟著航線走，所以線一動要重問一次。
   *  去抖——拖一個點會產生幾十次變動。 */
  const bldOff = useRef<() => void>(() => {});
  useEffect(() => {
    const t = setTimeout(async () => {
      const m = mapRef.current;
      if (!m) return;
      const got = await fetchNear(wps);
      if (!got || !mapRef.current) return;
      onBuildings?.(got.list);
      // **問到了就一定要畫上去。** 原本這裡是「樣式還沒好就算了」，而既有
      // 航線開頁時只算這一次——地形與影像那時還在載，`isStyleLoaded()`
      // 是 false，於是那一頁的建物**永遠不會出現**（使用者 2026-09-09）
      bldOff.current();
      bldOff.current = whenReady(m, () =>
        paintBuildings(m, got.fc, blindHeight(wps, assumeM)));
    }, 320);
    return () => clearTimeout(t);
  }, [wps, assumeM, onBuildings]);
  useEffect(() => () => bldOff.current(), []);

  useEffect(() => {
    if (!box.current || mapRef.current) return;
    const first = wps.find((w) => w.lat && w.lon);
    const placeRef = { current: { placing, onPlace } };
    placeRefBox.current = placeRef;
    const map = new maplibregl.Map({
      container: box.current,
      center: center ?? (first ? [first.lon, first.lat] : [121.0459, 24.7734]),
      zoom: 17, pitch: FIT_PITCH, maxPitch: 78, bearing: -28,
      // **反鋸齒。** maplibre 預設 false，而 three.js 的自訂圖層畫在
      // 它建的那張 canvas 上——所以航線與標記的邊緣一直是階梯狀的。
      // 這是「標示太粗糙」最大的一項（使用者 2026-09-09）
      antialias: true,
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
    /** 點到線段的距離（px）。 */
    const segDist = (p: { x: number; y: number },
                     a: { x: number; y: number },
                     b: { x: number; y: number }) => {
      const dx = b.x - a.x, dy = b.y - a.y;
      const t = Math.max(0, Math.min(1,
        ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy || 1)));
      return Math.hypot(p.x - (a.x + dx * t), p.y - (a.y + dy * t));
    };
    /** 航點的命中半徑（px）。**比看起來的球大很多**：使用者 2026-09-09
     *  回報「選不到既有點位，一直在新增」。指的是同一件事在螢幕上要點得到，
     *  而不是幾何上要碰到。
     *
     *  **連垂線一起算**（見下）：標記畫在航點的高度上，而那根柱子往下延伸到
     *  地面——人眼認定「那個點在哪裡」涵蓋整根柱子，命中也該如此。 */
    const PICK_WP_PX = 30;
    const hitTest = (pt: { x: number; y: number }): StageHit | null => {
      const ws = dataRef.current.wps;
      const proj = projRef.current;
      if (!proj) return null;
      const at = (w: StageWp) => proj(w.lon, w.lat, w.amsl);
      // **系統補的點（中繼、進場）不參與命中**：它們沿線每 30 m 一個，
      // 用這個半徑會把整條線都變成「點不到地面」——而且選中它們也沒用，
      // 下一次重算就換一批
      let best: { i: number; d: number } | null = null;
      for (let i = 0; i < ws.length; i++) {
        const w = ws[i];
        if (!w.lat || !w.lon || w.auto) continue;
        const p = at(w);
        if (!p) continue;
        // **連那根垂線一起算。** 標記畫在航點的高度上，而滑鼠點的是地面
        // ——兩者在螢幕上差一個高度，越高差越多（實測 12 m 的點差 43 px，
        // 而半徑才 22）。點在「那一根柱子」上任何一處都算點到它，
        // 這也正好是人眼認定「那個點在哪裡」的方式
        const g = w.ground == null ? null : proj(w.lon, w.lat, w.ground);
        const d = g ? segDist(pt, p, g) : Math.hypot(p.x - pt.x, p.y - pt.y);
        // 重疊時取**最近的那一個**，不是第一個碰到的
        if (d < PICK_WP_PX && (best === null || d < best.d)) best = { i, d };
      }
      if (best) return { kind: "wp", i: best.i };
      for (let i = 1; i < ws.length; i++) {
        const a = at(ws[i - 1]), b = at(ws[i]);
        if (a && b && segDist(pt, a, b) < 9) return { kind: "leg", i };
      }
      return null;
    };
    /** 圍欄頂點畫在 maplibre 的圖層上，不在 three.js 那一層，所以用它自己的
     *  `queryRenderedFeatures` 查。**頂點優先於航點**：頂點在邊界上、很小，
     *  兩者疊在一起時人要點的多半是頂點 */
    const hitFence = (pt: { x: number; y: number }): number | null => {
      if (!map.getLayer("fence-pt")) return null;
      const fs = map.queryRenderedFeatures(
        [[pt.x - 10, pt.y - 10], [pt.x + 10, pt.y + 10]], { layers: ["fence-pt"] });
      let best: { i: number; d: number } | null = null;
      for (const f of fs) {
        const i = f.properties?.i;
        if (typeof i !== "number") continue;
        const [lo, la] = (f.geometry as GeoJSON.Point).coordinates;
        const p = map.project([lo, la]);
        const d = Math.hypot(p.x - pt.x, p.y - pt.y);
        if (!best || d < best.d) best = { i, d };
      }
      return best?.i ?? null;
    };
    map.on("click", (e) => {
      const pl = placeRefBox.current?.current;
      const fv = hitFence(e.point);
      if (fv != null) { fenceRef.current.onFenceSelect?.(fv); return; }
      const h = hitTest(e.point);
      // **放點模式下也要先看有沒有點到既有航點。** 原本這裡直接放點就 return，
      // 於是點在一個已經在那裡的點上只會在它旁邊再疊一個——那個點永遠選不到，
      // 也就刪不掉（使用者 2026-09-09）。航段不擋放點：它很長，擋了會很難放
      if (h?.kind === "wp") { onSelect(h.i); return; }
      if (pl?.placing && pl.onPlace) {
        // **在座標空間比距離，不在螢幕空間。**
        //
        // 訂正（2026-09-09）：我一度以為「滑鼠點的地方」與「標記畫出來的
        // 地方」差 20–25 px，還把它寫進 commit。**那個測量是錯的**——我用
        // 「有沒有跳出提示框」當成命中的指標，而掃描步長是 10 px，
        // 第一個命中落在 +20 就被讀成偏移 20 px。改成 4 px 步長重掃，
        // 命中從 +4 就開始；再用 `map.unproject` 與投影函式直接對，
        // **實際只差 4 px**（點擊 559,307；標記 558,303），沒有系統性偏移。
        //
        // 那這裡為什麼還是用座標空間？因為它**不依賴投影函式當下是不是最新的**
        // ——那個函式每一幀由自訂圖層重設，而點擊處理器只是讀 ref。
        // 螢幕空間那條路仍然留著（選取、拖曳、hover 都走它）。
        const mpp = 156543.03392 * Math.cos(e.lngLat.lat * Math.PI / 180)
          / Math.pow(2, map.getZoom());
        const near = mpp * PICK_WP_PX;           // 像素容忍換算成公尺
        let hit: number | null = null, bd = Infinity;
        dataRef.current.wps.forEach((w, i) => {
          if (!w.lat || !w.lon || w.auto) return;
          const dy = (w.lat - e.lngLat.lat) * 110574;
          const dx = (w.lon - e.lngLat.lng) * 111320
            * Math.cos(e.lngLat.lat * Math.PI / 180);
          const d = Math.hypot(dx, dy);
          if (d < near && d < bd) { hit = i; bd = d; }
        });
        if (hit !== null) { onSelect(hit); return; }
        pl.onPlace(e.lngLat);
        return;
      }
      if (h) onSelect(h.i);
    });
    map.on("mousedown", (e) => {
      const fv = hitFence(e.point);
      if (fv != null && fenceRef.current.onFenceMove) {
        fenceDragRef.current = fv;
        map.dragPan.disable(); map.dragRotate.disable();
        fenceRef.current.onFenceSelect?.(fv);
        e.preventDefault();
        return;
      }
      const h = hitTest(e.point);
      if (h?.kind === "wp" && !dataRef.current.wps[h.i]?.fixed && moveRef.current) {
        dragRef.current = h.i;
        map.dragPan.disable(); map.dragRotate.disable();
        onSelect(h.i);
        e.preventDefault();
      }
    });
    const endDrag = () => {
      if (dragRef.current == null && fenceDragRef.current == null) return;
      dragRef.current = null;
      fenceDragRef.current = null;
      map.dragPan.enable(); map.dragRotate.enable();
    };
    map.on("mouseup", endDrag);
    map.on("dragend", endDrag);
    map.on("mousemove", (e) => {
      if (fenceDragRef.current != null) {
        fenceRef.current.onFenceMove?.(fenceDragRef.current, e.lngLat);
        return;
      }
      if (hitFence(e.point) != null) {
        map.getCanvas().style.cursor = "grab";
        setTip(null);
        return;
      }
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

  useEffect(() => {
    const m = mapRef.current;
    if (!m) return;
    return whenReady(m, () => paintFence(m, fence, fenceSel));
  }, [fence, fenceSel]);

  useEffect(() => {
    if (!flyTo) return;
    mapRef.current?.flyTo({ center: [flyTo.lon, flyTo.lat], zoom: 17, essential: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flyTo?.n]);

  return (
    <div className={`stage3d-wrap${placing ? " placing" : ""}`}>
      {/* **圖例直接回答「哪個是起飛點」。** 形狀本身有分，但那要先知道
          規則才讀得出來——使用者 2026-09-09：「我還是不知道哪個是起飛」。 */}
      <div className="stage-legend">
        <span><svg width="14" height="14" viewBox="0 0 14 14">
          <line x1="1" y1="7" x2="13" y2="7" stroke="#0ca30c" strokeWidth="1.6"/>
          <line x1="7" y1="1" x2="7" y2="13" stroke="#0ca30c" strokeWidth="1.6"/>
          <circle cx="7" cy="7" r="4" fill="none" stroke="#0ca30c" strokeWidth="2.4"/>
        </svg>起飛點</span>
        <span><svg width="14" height="14" viewBox="0 0 14 14">
          <circle cx="7" cy="7" r="4.2" fill="#3987e5"/></svg>航點</span>
        <span><svg width="14" height="14" viewBox="0 0 14 14">
          <path d="M7,2 L12,11 H2 Z" fill="#0ca30c"/></svg>降落點</span>
      </div>
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

/** **地圖真的畫得動了才做這件事**，回一個取消訂閱。
 *
 *  只等 `load` 不夠：樣式改過（加圖磚來源、開地形）之後 `isStyleLoaded()`
 *  會有一段時間是 false，而那時 `load` 早就發過了——開頁時只畫一次的東西
 *  （既有航線的建物、航線自帶的圍欄）就這樣安靜地不見。
 *  也要等 `route3d`：樣式一開始只有底色，那時 `isStyleLoaded()` 就是 true，
 *  先畫上去的話隨後補的正射影像會蓋在它上面（`addLayer` 沒給 before 就是
 *  最上層）。`idle` ＝畫完而且沒有待辦，那一刻一定成。 */
function whenReady(m: maplibregl.Map, fn: () => void): () => void {
  const ok = () => m.isStyleLoaded() && !!m.getLayer("route3d");
  if (ok()) { fn(); return () => {}; }
  const off = () => { m.off("idle", retry); m.off("styledata", retry); };
  const retry = () => { if (!ok()) return; off(); fn(); };
  m.on("idle", retry);
  m.on("styledata", retry);
  return off;
}

/** 圍欄畫成地面上的一圈虛線——**它是平面範圍**，高度上限由剖面圖那條線講。
 *  圓形用 64 邊形近似：地圖上看不出差別，而且與多邊形共用同一組圖層。 */
function fenceGeo(f: FenceShape | null, sel = -1): GeoJSON.FeatureCollection {
  let ring: [number, number][] = [];
  if (f?.shape === "circle" && f.center && f.radius_m) {
    const [lat, lon] = f.center, r = f.radius_m;
    const dLat = r / 110574, dLon = r / (111320 * Math.cos(lat * Math.PI / 180));
    for (let i = 0; i <= 64; i++) {
      const a = (i / 64) * Math.PI * 2;
      ring.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
    }
  } else if (f?.shape === "polygon" && (f.points?.length ?? 0) >= 3) {
    ring = f.points!.map(([la, lo]) => [lo, la] as [number, number]);
    ring.push(ring[0]);
  }
  const feats: GeoJSON.Feature[] = ring.length
    ? [{ type: "Feature", properties: {},
         geometry: { type: "Polygon", coordinates: [ring] } }] : [];
  // 頂點單獨畫出來——**還沒滿三點時多邊形不成立**，但使用者要看得到
  // 自己點了哪幾下，不然前兩下像沒有反應
  if (f?.shape === "polygon") {
    (f.points ?? []).forEach(([la, lo], i) =>
      feats.push({ type: "Feature", properties: { i, sel: i === sel },
                   geometry: { type: "Point", coordinates: [lo, la] } }));
  }
  return { type: "FeatureCollection", features: feats };
}

function paintFence(map: maplibregl.Map, f: FenceShape | null, sel = -1) {
  const data = fenceGeo(f, sel);
  const src = map.getSource("fence") as maplibregl.GeoJSONSource | undefined;
  if (src) { src.setData(data); return; }
  const before = map.getLayer("route3d") ? "route3d" : undefined;
  map.addSource("fence", { type: "geojson", data });
  map.addLayer({ id: "fence-fill", type: "fill", source: "fence",
    paint: { "fill-color": "#fab219", "fill-opacity": 0.06 } }, before);
  map.addLayer({ id: "fence-line", type: "line", source: "fence",
    paint: { "line-color": "#fab219", "line-width": 2,
             "line-dasharray": [3, 2] } }, before);
  map.addLayer({ id: "fence-pt", type: "circle", source: "fence",
    filter: ["==", ["geometry-type"], "Point"],
    // 頂點要抓得到，所以比線粗；選中的加一圈亮邊
    paint: { "circle-radius": ["case", ["get", "sel"], 7, 5.5],
             "circle-color": "#fab219",
             "circle-stroke-width": ["case", ["get", "sel"], 2.5, 1],
             "circle-stroke-color": ["case", ["get", "sel"], "#f0eee6", "#1b1a17"] } },
    before);
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
      // 細一點、圓一點：0.9 m ／ 6 段的管子在 1× 下是一條有稜有角的粗帶
      const geo = new THREE.TubeGeometry(curve, 1, 0.55 * mScale, 12, false);
      const hot = hover?.kind === "leg" && hover.i === i;
      group.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
        color: hot ? HOT : b.bad ? RED : BLUE })));
    }
    // 垂線＝離地空間。**沒有地形資料就不畫**，畫一根到 0 m 的線是騙人
    pts.forEach((w, i) => {
      if (w.ground == null) return;
      const curve = new THREE.LineCurve3(v(w.lon, w.lat, w.amsl), v(w.lon, w.lat, w.ground));
      group.add(new THREE.Mesh(
        new THREE.TubeGeometry(curve, 1, 0.2 * mScale, 8, false),
        new THREE.MeshBasicMaterial({ color: w.bad ? RED : BLUE,
          transparent: true, opacity: 0.55 })));
      const hot = hover?.kind === "wp" && hover.i === i;
      const at = v(w.lon, w.lat, w.amsl);
      // **三種點要一眼分得出來**（使用者 2026-09-09）：分不出來時
      // 「放一個點卻有線」看起來像 bug，其實那條線是起飛點連過去的。
      // `auto` 的（中繼點、進場點）畫小一點：它們是真的航點，但不是人放的
      const big = i === sel || hot;
      // **選取不搶顏色。** 原本選中就整顆變成 PICK（橘），於是「這是起飛點」
      // 那個綠色被蓋掉——而起飛點預設就會被選中，等於永遠看不到它的類別
      // （使用者 2026-09-09 連續問了兩次「哪個是起飛點」）。改成：
      // **顏色永遠是類別，選取加一圈光暈**。
      const col = w.bad ? RED : BLUE;
      const gcol = GROUND_PT;
      const r = (big ? 1.7 : w.auto ? 0.7 : 1.1) * mScale;
      // **接地點畫得比航點大**：它們是這條航線的兩端，而且航段的管子很粗
      // （0.9 m），跟航點一樣大的話在 1× 下讀不出形狀
      const gr = r * 1.35;
      if (w.kind === "takeoff") {
        // 空心環 ＋ 地面十字。**環是空的**：那個形狀順便說它拖不動
        const ring = new THREE.Mesh(
          new THREE.TorusGeometry(gr * 1.6, gr * 0.34, 10, 40),
          new THREE.MeshBasicMaterial({ color: gcol }));
        ring.position.copy(at);
        group.add(ring);
        if (w.ground != null) {
          const g0 = v(w.lon, w.lat, w.ground);
          const arm = gr * 3.0;
          for (const [dx, dy] of [[1, 0], [0, 1]] as const) {
            const a = g0.clone(), b2 = g0.clone();
            a.x -= arm * dx; a.y -= arm * dy;
            b2.x += arm * dx; b2.y += arm * dy;
            group.add(new THREE.Mesh(
              new THREE.TubeGeometry(new THREE.LineCurve3(a, b2), 1, gr * 0.22, 8, false),
              new THREE.MeshBasicMaterial({ color: gcol })));
          }
        }
      } else if (w.kind === "land") {
        // **圓錐站在地上**（使用者 2026-09-09）：three.js 的錐體軸是 +Y，
        // 而這個場景的上方是 +Z——原本只翻了 180°，錐體其實是**橫躺**的。
        // 轉 90° 讓軸站起來，再把底面壓到地面高度：降落點是地面上的一個
        // 位置，那個圓面貼在哪裡就是飛機會落在哪裡
        const ch = gr * 3.2;
        const cone = new THREE.Mesh(
          new THREE.ConeGeometry(gr * 1.55, ch, 18),
          new THREE.MeshBasicMaterial({ color: gcol }));
        cone.rotation.x = Math.PI / 2;
        const base = w.ground != null ? v(w.lon, w.lat, w.ground) : at.clone();
        cone.position.copy(base);
        cone.position.z += ch / 2;          // 幾何的原點在腰上，抬半個高度才貼地
        group.add(cone);
      } else {
        const sp = new THREE.Mesh(new THREE.SphereGeometry(r, 20, 14),
          new THREE.MeshBasicMaterial({ color: col }));
        sp.position.copy(at);
        group.add(sp);
      }
      if (i === sel || hot) {
        // 光暈：選取用實色、滑過用半透明。**它不遮住底下的顏色**
        const halo = new THREE.Mesh(
          new THREE.TorusGeometry(r * 2.3, r * 0.22, 8, 32),
          new THREE.MeshBasicMaterial({
            color: i === sel ? PICK : HOT,
            transparent: i !== sel, opacity: 0.6 }));
        halo.position.copy(at);
        group.add(halo);
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
