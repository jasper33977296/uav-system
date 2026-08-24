"use client";
import { IconLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";

import BasemapToggle from "@/components/BasemapToggle";
import CommandPanel from "@/components/CommandPanel";
import { colorFor } from "@/components/droneLayer";
import SimpleHud from "@/components/SimpleHud";
import VideoModal from "@/components/VideoModal";
import VideoPlayer from "@/components/VideoPlayer";
import { useBasemap } from "@/lib/basemap";
import { pathsLayer, rgba, routeLayer, type RouteRun } from "@/lib/deckRoute";
import { DRONE_ICON_SIZE, droneIconUrl } from "@/lib/droneIcon";
import { lodFactor } from "@/lib/droneMesh";
import { droneMeshLayers } from "@/lib/droneMeshLayer";
import { basePreview, separatePreview, unifiedPreview, type Wp } from "@/lib/formation";
import { CANVAS, groundGrid, ribbon, trailLineString } from "@/lib/geo";
import { getJson } from "@/lib/fetchJson";
import { API, LINK_CLASSES } from "@/lib/signal";
import { firstFleetPos, useUavStore } from "@/lib/store";


// 刻意**不放底圖**：場域物件不存在於系統認知中，鏈路品質的空間分布由
// 實測軌跡自己揭露；離線（場域實測常態）也完全可用。
// 要加底圖時在 style.sources 加 raster source、layers 最前面插一層即可。

interface DroneVideo { id: string; name: string; video_url: string | null }

export default function MapView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const centeredRef = useRef(false);
  const [videoDrone, setVideoDrone] = useState<string | null>(null);
  const coordRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const refreshPlanRef = useRef<() => void>(() => {});
  // 任務開始成功（CommandPanel 自動 activate）→ 疊圖即刻重刷
  const planReq = useUavStore((s) => s.planReq);
  useEffect(() => { refreshPlanRef.current(); }, [planReq]);
  const ribbonGateRef = useRef({ t: 0, n: -1 });   // 地面投影重建節流
  const pitchRef = useRef(55);   // 機體圖示的俯角尺寸補償用（見 IconLayer）
  const zoomRef = useRef(15);    // §2.4d LOD 換手與標籤同位判定用

  // simple-first：專業面板抽屜；任務控制面板恆顯（自收合，ui-spec §2）
  const panelOpen = useUavStore((s) => s.panelOpen);
  // 機隊點：只訂閱成員 id 字串（fleet 物件 5Hz 換新，直接訂會整頁重渲染）
  const fleetIds = useUavStore((s) => Object.keys(s.fleet).join(","));
  const dotIds = fleetIds ? fleetIds.split(",") : [];

  // 013-A 編隊：色點/機體球互動分支＋N 條誠實預覽（前端試算，013-B 改讀
  // 後端 materialized assignments）
  const formationOn = useUavStore((s) => s.formation);
  const targetKey = useUavStore((s) => s.targetIds.join(","));
  const fCfg = useUavStore((s) => s.formationCfg);
  const draftGroup = useUavStore((s) => s.draftGroup);
  const previewRef = useRef<RouteRun[]>([]);
  const wpCacheRef = useRef(new Map<string, Wp[]>());
  useEffect(() => {
    let stop = false;
    (async () => {
      if (!formationOn) { previewRef.current = []; return; }
      const st = useUavStore.getState();
      const targets = st.targetIds.filter((id) => st.fleet[id]);
      const getWps = async (mid: string): Promise<Wp[]> => {
        if (!wpCacheRef.current.has(mid)) {
          const d = await fetch(`${API}/api/missions/${mid}/waypoints`)
            .then((r) => r.json()).catch(() => null);
          wpCacheRef.current.set(mid, d?.waypoints ?? []);
        }
        return wpCacheRef.current.get(mid)!;
      };
      let runs: RouteRun[] = [];
      if (draftGroup) {
        // draft 已建：讀後端 materialized assignments（單一真相——實際會
        // 上傳到機的那 N 條，含分層高度），前端試算退場
        const per = [];
        for (const a of draftGroup.assignments) {
          per.push({ id: a.drone_id, color: colorFor(a.drone_id),
                     wps: await getWps(a.mission_id) });
        }
        runs = separatePreview(per);
        if (draftGroup.mode === "unified" && fCfg.base) {
          runs = [...basePreview(await getWps(fCfg.base)), ...runs];
        }
      } else if (fCfg.mode === "unified" && fCfg.base) {
        const wps = await getWps(fCfg.base);
        runs = [
          ...basePreview(wps),
          ...unifiedPreview(wps,
            targets.map((id) => ({ id, color: colorFor(id) })), fCfg.spacing),
        ];
      } else if (fCfg.mode === "separate") {
        const per = [];
        for (const id of targets) {
          const mid = fCfg.assign[id];
          if (mid) per.push({ id, color: colorFor(id), wps: await getWps(mid) });
        }
        runs = separatePreview(per);
      }
      if (!stop) previewRef.current = runs;
    })();
    return () => { stop = true; };
  }, [formationOn, fCfg, targetKey, draftGroup]);

  // 檢視切換：地圖 ↔ 當前選擇機（側欄選的，未選＝主機）的即時影像
  const [view, setView] = useState<"map" | "video">("map");
  const [videoList, setVideoList] = useState<DroneVideo[] | null>(null);
  const selId = useUavStore((s) => s.selectedId ?? s.primaryId);
  const selName = useUavStore((s) => {
    const id = s.selectedId ?? s.primaryId;
    return id ? s.fleet[id]?.drone_name || id : null;
  });
  // §2.9 PiP 常駐需要影像源清單：開頁與換選中機時讀最新值（video_url
  // 在無人機頁隨時可改；切進全幅檢視時也重讀一次）
  useEffect(() => {
    // HTTP 錯誤走 catch（見 lib/fetchJson.ts）——空清單＝「沒有影像源」是
    // 一個宣告，取不到時不該由我方替後端宣告
    getJson<DroneVideo[]>(`${API}/api/drones`)
      .then(setVideoList).catch(() => setVideoList([]));
  }, [view, selId]);
  // §2.4b 底圖：狀態與圖層安裝抽到 lib/basemap（與回放頁共用同一實作）
  const base = useBasemap();

  // PiP 收合態（§2.9：小窗可收合成 📹 鈕）；換機自動展開回小窗
  const [pipHidden, setPipHidden] = useState(false);
  useEffect(() => { setPipHidden(false); }, [selId]);
  const selUrl = videoList?.find((d) => d.id === selId)?.video_url ?? null;

  // §2.9 PiP 自由拖曳（使用者現場反饋）：拖窗身移動、<5px 視為點擊（放大）、
  // 邊界夾限（四邊 8px、上界避開導覽列）、位置記憶（key 獨立於面板）、
  // 不做吸附避讓——使用者拖到哪就是哪（與任務控制面板同哲學）
  const pipRef = useRef<HTMLDivElement>(null);
  const [pipPos, setPipPos] = useState<{ x: number; y: number } | null>(null);
  const pipPosRef = useRef(pipPos);
  useEffect(() => { pipPosRef.current = pipPos; }, [pipPos]);
  const clampPip = (p: { x: number; y: number }) => {
    const w = pipRef.current?.offsetWidth ?? 280;
    const h = pipRef.current?.offsetHeight ?? 158;
    return {
      x: Math.max(8, Math.min(p.x, window.innerWidth - w - 8)),
      y: Math.max(56, Math.min(p.y, window.innerHeight - h - 8)),
    };
  };
  useEffect(() => {
    try {
      // 載入時夾限一次：視窗縮小後舊記憶座標可能已在畫面外
      const saved = localStorage.getItem("video-pip-pos");
      if (saved) setPipPos(clampPip(JSON.parse(saved)));
    } catch { /* 壞值用預設位置 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const pipDrag = useRef<{ dx: number; dy: number; sx: number; sy: number;
    moved: boolean } | null>(null);
  function pipDown(e: React.PointerEvent) {
    if ((e.target as Element).closest(".pip-hide")) return;   // 收合鈕不啟動拖曳
    const r = pipRef.current!.getBoundingClientRect();
    pipDrag.current = { dx: e.clientX - r.left, dy: e.clientY - r.top,
      sx: e.clientX, sy: e.clientY, moved: false };
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
  }
  function pipMove(e: React.PointerEvent) {
    const d = pipDrag.current;
    if (!d) return;
    if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 5) return;
    d.moved = true;
    setPipPos(clampPip({ x: e.clientX - d.dx, y: e.clientY - d.dy }));
  }
  function pipUp() {
    const d = pipDrag.current;
    pipDrag.current = null;
    if (!d) return;
    if (!d.moved) setView("video");                            // 點擊＝放大
    else if (pipPosRef.current) {
      localStorage.setItem("video-pip-pos", JSON.stringify(pipPosRef.current));
    }
  }


  useEffect(() => {
    const map = new maplibregl.Map({
      container: containerRef.current!,
      // 初始中心取機隊實際座標；沒有遙測時用世界視野（不假裝知道在哪），
      // 收到第一筆座標即 jumpTo（見 centeredRef）
      center: firstFleetPos() ?? [0, 20],
      zoom: firstFleetPos() ? 15 : 1.5,
      pitch: 55,                 // 3D 傾斜視角（右鍵拖曳可調）
      maxPitch: 75,
      style: {
        version: 8,
        sources: {
          // ← 底圖插槽
        },
        layers: [
          { id: "canvas", type: "background", paint: { "background-color": CANVAS } },
        ],
      },
      attributionControl: false,   // 左下讓給圖例＋HUD（ui-spec §2）——與比例尺同右下
    });
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;
    // 測試把手（**僅開發模式**）：§2.4d 驗收要求俯角×縮放的像素量測，
    // headless 需精確設定視角；production build 不掛、不改產品行為
    if (process.env.NODE_ENV !== "production") {
      (window as unknown as { __map?: maplibregl.Map }).__map = map;
    }

    map.on("load", async () => {
      base.install(map);

      // 地面基準：網格 50m 一格 + 起飛點雙圈錨點
      // 網格/起飛點錨在**實際**出生點：先建空 source，收到第一筆座標再填
      // （原本錨死在 SITL 舊出生點，機隊搬家後整組參考線就在別的洲）
      const EMPTY_FC: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
      map.addSource("grid", { type: "geojson", data: EMPTY_FC });
      map.addLayer({
        id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#262624", "line-width": 1 },
      });
      map.addSource("home", {
        type: "geojson",
        data: EMPTY_FC,
      });
      map.addLayer({
        id: "home-ring", type: "circle", source: "home",
        paint: {
          "circle-radius": 10, "circle-color": "transparent",
          "circle-stroke-width": 2, "circle-stroke-color": "#8f8b80",
        },
      });
      map.addLayer({
        id: "home-dot", type: "circle", source: "home",
        paint: { "circle-radius": 3, "circle-color": "#8f8b80" },
      });

      // 地面投影：實際軌跡的垂直投影。**顏色＝機別色**（分層編碼：
      // 空中絲帶編碼訊號品質、地面投影與球體編碼「這是哪台飛的」）。
      // 用連續線而非逐點圓點——1Hz/5Hz 的點列是一串顆粒，線才平滑。
      map.addSource("trail", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "trail-line", type: "line", source: "trail",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["get", "dcolor"],
          // 縮放自適應：兩端有界、中間連續（同 pathsLayer 原則）
          // §2.4c 加粗 1–3.5→2–5px：原本太細又被空中絲帶壓過，遠看等同
          // 消失。判準是任何 zoom 下都要能一眼分辨空中絲帶與地面投影
          "line-width": ["interpolate", ["linear"], ["zoom"], 12, 2, 16, 3.5, 20, 5],
          "line-opacity": 0.7,
        },
      });

      // 任務疊圖：**只**畫任務庫的啟用路徑（路徑管理頁「顯示於即時頁」）。
      // 使用者的顯隱選擇是唯一真相——全部隱藏就什麼都不畫，
      // 不退回機上任務（那個備援曾讓「隱藏」失效，見 2026-08-05 修正；
      // 機上任務仍可在路徑管理頁「從機上讀回」取得）。
      // sources 常駐、資料可重刷（§4 v3：任務開始成功自動 activate 後即刻
      // 浮現，不用重整頁面）。舊寫法的 beforeId "path3d" 在 deck 遷移後
      // 已不存在、addLayer 拋錯被 try/catch 靜默吞掉——疊圖自遷移起其實
      // 沒畫，這次一併修正
      const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
      map.addSource("plan3d", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "plan3d", type: "fill-extrusion", source: "plan3d",
        paint: {
          "fill-extrusion-color": "#8f8b80",
          "fill-extrusion-height": ["get", "top"],
          "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.35,
        },
      });
      map.addSource("plan-ground", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "plan-ground-line", type: "line", source: "plan-ground",
        filter: ["!", ["has", "pt"]],
        paint: { "line-color": "#8f8b80", "line-width": 1.5,
                 "line-dasharray": [3, 3], "line-opacity": 0.6 },
      }, "trail-line");
      map.addLayer({
        id: "plan-ground-wp", type: "circle", source: "plan-ground",
        filter: ["has", "pt"],
        paint: { "circle-radius": 4, "circle-color": "transparent",
                 "circle-stroke-width": 1.5, "circle-stroke-color": "#8f8b80" },
      }, "trail-line");

      // ⚠ 繪製順序（§2.4c）：**計畫路徑必須在 deck overlay 之前建立**。
      // interleaved overlay 掛上時插在「當下最上層」，之後才 addLayer 的
      // maplibre 圖層會蓋在 deck 之上——先前計畫路徑正是建在 overlay 之後，
      // 於是灰色「應該飛的線」蓋住實際軌跡，連機體圖示都會被蓋（違反
      // §2.4c「圖示永不被覆蓋」）。**產出（實測軌跡）不得被輸入（計畫）遮蔽。**
      // 實際路徑：deck.gl PathLayer（route-render-tool-eval 定案，取代
      // fill-extrusion 絲帶）——interleaved 模式與 maplibre 同一 GL context，
      // 與 three.js 球體自訂層共存（interop 是選型條件 3，落地後實測）
      // 視角追蹤：LOD 換手與標籤偏移都要當下的俯角/縮放（圖層在遙測
      // 更新時重建，這裡只更新讀數）
      pitchRef.current = map.getPitch();
      zoomRef.current = map.getZoom();
      map.on("pitch", () => { pitchRef.current = map.getPitch(); });
      map.on("zoom", () => { zoomRef.current = map.getZoom(); });

      const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
      map.addControl(overlay as unknown as maplibregl.IControl);
      overlayRef.current = overlay;

      // 機體表示自 §2.4b 起改為 2D 四旋翼俯視圖示（deck IconLayer，見
      // setProps 處）：俯視剪影能表達朝向、在正射影像底圖上也辨識得出來，
      // 球體做不到（正圓沒有朝向）。three.js 球體層程式碼保留未掛載——
      // 若 2D 效果不佳要回退，把下面這行解註即可（PM 定案：3D 模型不做）
      // map.addLayer(createDroneLayer("drones", () => …));

      // 點擊機體改由 deck IconLayer 的 onClick 承接（圖示層是 pickable，
      // 不再需要自算螢幕命中——那是 three.js 自訂層時代的做法）
      map.on("mousemove", (e) => {
        // 游標經緯度（issue 017 P1）：直接寫 DOM——60Hz 的 mousemove
        // 走 React state 會整個元件重渲染
        if (coordRef.current) {
          coordRef.current.textContent =
            `${e.lngLat.lat.toFixed(6)}, ${e.lngLat.lng.toFixed(6)}`;
        }
      });

      const refreshPlan = async () => {
        try {
          let wps: any[] = [];
          // 沒有啟用中的航線時後端回 404——那是正常態不是錯誤（!ok 就清空
          // 疊圖）。console 會看到一則 404，不必追
          const ra = await fetch(`${API}/api/missions/active`);
          if (ra.ok) wps = ((await ra.json()).waypoints ?? []).filter((w: any) => w.lat && w.lon);
          const has = wps.length >= 2;
          (map.getSource("plan3d") as maplibregl.GeoJSONSource | undefined)?.setData(
            has ? ribbon(wps.map((w: any) => ({ lat: w.lat, lon: w.lon, alt: w.alt })),
                         () => ({}), 1.0) : EMPTY);
          (map.getSource("plan-ground") as maplibregl.GeoJSONSource | undefined)?.setData(
            has ? {
              type: "FeatureCollection",
              features: [
                { type: "Feature", properties: {}, geometry: {
                  type: "LineString",
                  coordinates: wps.map((w: any) => [w.lon, w.lat]) } },
                ...wps.map((w: any) => ({
                  type: "Feature" as const, properties: { pt: 1 },
                  geometry: { type: "Point" as const, coordinates: [w.lon, w.lat] },
                })),
              ],
            } : EMPTY);
        } catch { /* 無任務即空疊圖 */ }
      };
      refreshPlanRef.current = refreshPlan;
      refreshPlan();
    });

    return () => map.remove();
  }, []);

  // 即時更新：懸浮機體 + 航向投影 + 絲帶
  useEffect(() => {
    const paint = (s: ReturnType<typeof useUavStore.getState>) => {
      {
        const map = mapRef.current;
        const t = s.live;
        if (!map) return;

        // 舊的主機方向標（三角 marker）自 §2.4b 起移除：機體圖示本身已
        // 帶朝向，兩個朝向指示同時出現＝同一資訊兩個住所，且它只畫主機、
        // 與多機圖示不一致（heading 缺值時還會固定指北——假朝向）

        // **位置守衛只包這一段。** 原本它擋在整個 callback 開頭，於是主機沒有
        // 定位時（GPS 未鎖、剛連上、或室內）連別台機的軌跡與圖示都一起停更——
        // 一台機的缺值癱瘓整張地圖。置中確實需要座標，圖層更新不需要。
        if (t && t.lat != null && t.lon != null && !centeredRef.current) {
          centeredRef.current = true;
          map.jumpTo({ center: [t.lon, t.lat], zoom: 16 });
          // 參考線同時錨定到這台機的位置（第一筆座標＝本次的現場）
          (map.getSource("grid") as maplibregl.GeoJSONSource | undefined)
            ?.setData(groundGrid(t.lat, t.lon));
          (map.getSource("home") as maplibregl.GeoJSONSource | undefined)?.setData({
            type: "Feature", properties: {},
            geometry: { type: "Point", coordinates: [t.lon, t.lat] },
          } as GeoJSON.Feature);
        }

        // 球體層自己每幀從 store 讀位置（triggerRepaint 驅動），不需在此餵資料

        // 空中航跡：deck.gl PathLayer——attribute 更新是同幀 GPU buffer
        // 寫入（無 setData 整源替換的閃爍），5Hz 直更不需節流。
        // 編隊預覽層一併掛上（非編隊時為空陣列，零成本）
        // 斷線閃爍的相位。用 Date.now() 而不是累加計數：即使重繪節奏不穩
        // （store 更新與本地時鐘交錯觸發），閃爍節奏仍由真實時間決定，
        // 不會忽快忽慢。
        const blink = 0.5 + 0.5 * Math.sin(Date.now() / 260);
        // 透明度隨相位起伏但**不歸零**：閃到全透明會有半個週期完全看不出
        // 異常，而「看不出異常」正是這個標示要防的事
        const lostColor: [number, number, number, number] =
          [255, 69, 58, Math.round(110 + 145 * blink)];

        overlayRef.current?.setProps({ layers: [
          ...routeLayer("route3d", s.trails),
          ...pathsLayer("formation-preview", previewRef.current),
          // §2.4d 3D 機體：近距真 mesh（有實體高度）、遠距交回 2D 圖示，
          // 門檻區間交叉淡出；五條硬約束的實作見 lib/droneMeshLayer
          ...droneMeshLayers(
            Object.entries(s.fleet).map(([id, t]) => ({
              id, lat: t.lat, lon: t.lon, alt_rel: t.alt_rel })),
            zoomRef.current, (id) => rgba(colorFor(id))),
          // 斷線標記（使用者定案 2026-08-24）：紅色外框閃爍。
          // **排在機體圖示之前**＝畫在它下面，不遮蔽本體——找得到機在哪
          // 永遠優先於任何標示（§2.4c 硬規則的同一條理由）。
          // 只標「曾連上但現在斷線」的機：從未連上的根本不會進 fleet
          // （後端廣播已擋，見 issues/036）。
          new ScatterplotLayer({
            id: "drone-lost-ring",
            data: Object.entries(s.fleet)
              .filter(([, t]) => t.connected === false
                && t.lat != null && t.lon != null)
              .map(([id, t]) => ({
                id,
                pos: [t.lon!, t.lat!, t.alt_rel ?? 0] as [number, number, number],
              })),
            getPosition: (d: { pos: [number, number, number] }) => d.pos,
            stroked: true, filled: false,
            radiusUnits: "pixels" as const, lineWidthUnits: "pixels" as const,
            getRadius: 30 + 12 * blink,
            getLineWidth: 2 + 2 * blink,
            getLineColor: lostColor,
            updateTriggers: {
              getRadius: blink, getLineWidth: blink, getLineColor: blink,
              getPosition: fleetIds,
            },
          }),
          // 機體圖示：2D 四旋翼俯視剪影、著識別色、**對稱不旋轉**
          // （§2.4c 使用者裁定：方向由軌跡承載）。billboard:false＝貼地平面，
          // 傾斜視角下與軌跡同一透視。
          // **圖示與標籤必須排在所有軌跡/投影/計畫路徑層之後**（§2.4c 硬規則）：
          // deck 依陣列順序繪製，後者在上——找不到機在哪＝監控失效，
          // 任何美觀考量都不得凌駕
          new IconLayer({
            id: "drone-icons",
            // 與 3D mesh 互補淡出（共用 lodFactor，門檻只有一份不漂移）
            opacity: 1 - lodFactor(zoomRef.current),
            data: Object.entries(s.fleet)
              .filter(([, t]) => t.lat != null && t.lon != null)
              .map(([id, t]) => ({
                id,
                pos: [t.lon!, t.lat!, t.alt_rel ?? 0] as [number, number, number],
                url: droneIconUrl(colorFor(id)),
              })),
            getPosition: (d: { pos: [number, number, number] }) => d.pos,
            getIcon: (d: { url: string }) => ({
              url: d.url, width: DRONE_ICON_SIZE, height: DRONE_ICON_SIZE,
              anchorX: DRONE_ICON_SIZE / 2, anchorY: DRONE_ICON_SIZE / 2, mask: false,
            }),
            getSize: 44, sizeUnits: "pixels", billboard: false, pickable: true,
            // 俯角尺寸補償（§2.4b 裁定）：貼地圖示在傾斜視角下被透視壓縮
            // （短軸 ×cos(pitch)），放大以維持輪廓可辨。**不改 billboard**
            // ——那會讓機頭方向不再對應地面方位，朝向淪為裝飾。
            // 用 cos^-0.75 而非完全補償 cos^-1：完全補償會把圖示橫向撐得
            // 過寬，部分補償在「看得出四旋翼」與「不過胖」之間取平衡
            sizeScale: Math.min(2.2, Math.pow(
              Math.max(0.2, Math.cos((pitchRef.current * Math.PI) / 180)), -0.75)),
            onHover: (info) => {
              const c = mapRef.current?.getCanvas();
              if (c) c.style.cursor = info.object ? "pointer" : "";
            },
            onClick: (info) => {
              const id = (info.object as { id: string } | null)?.id;
              if (!id) return true;
              const st = useUavStore.getState();
              if (st.formation) st.select(id); else setVideoDrone(id);
              return true;
            },
            updateTriggers: { getIcon: fleetIds, getPosition: fleetIds },
          }),
          // 機身標籤（ui-spec §0.1 硬規則）：色點/球體一律配常駐文字——
          // 第 4 台起色盤耗盡全落同一灰，只有顏色時兩台完全無法分辨；
          // 且球體連 hover 都沒有。文字對任意機數都成立。
          new TextLayer({
            id: "drone-labels",
            // 同位判定改**螢幕距離**：原本用固定 20m，但 20m 在高縮放下是
            // 上百像素、根本不會疊，卻仍被堆疊而把標籤推遠。真正該問的是
            // 「在畫面上會不會疊到」
            data: (() => {
              const list = Object.entries(s.fleet)
                .filter(([, t]) => t.lat != null && t.lon != null)
                .sort(([a], [b]) => (a < b ? -1 : 1));   // 順序穩定不跳動
              const mpp = (156543.03392 * Math.cos(((list[0]?.[1].lat ?? 25)
                * Math.PI) / 180)) / Math.pow(2, zoomRef.current);
              const NEAR_PX = 30;   // 兩標籤在畫面上會互相疊到的距離
              return list.map(([id, t], i) => ({
                id, name: t.drone_name || id.slice(0, 6),
                // **與機體同一個 3D 位置**：原本寫 alt_rel + 6，那個 6 是
                // **公尺**（球體時代遺留）——標籤浮在機體上方 6 公尺，高度差
                // 在俯視投影為零、平視放到最大，正是使用者說的「俯視很近、
                // 平視很遠」。垂直距離一律交給 getPixelOffset（螢幕像素）
                pos: [t.lon!, t.lat!, t.alt_rel ?? 0] as [number, number, number],
                color: rgba(colorFor(id)),
                slot: list.slice(0, i).filter(([, o]) =>
                  Math.hypot((o.lat! - t.lat!) * 110574,
                    (o.lon! - t.lon!) * 111320 * Math.cos(t.lat! * Math.PI / 180))
                    / mpp < NEAR_PX
                ).length,
              }));
            })(),
            getPosition: (d: { pos: [number, number, number] }) => d.pos,
            getText: (d: { name: string }) => d.name,
            getColor: (d: { color: [number, number, number, number] }) => d.color,
            getSize: 13,
            getTextAnchor: "middle" as const,
            // 同位者上下交錯（偶數在上、奇數在下）：四機同位時原本最上面
            // 那個離機體 45px（使用者說「太遠」的真身），交錯後減半
            getAlignmentBaseline: (d: { slot: number }) =>
              (d.slot % 2 === 0 ? "bottom" : "top") as "bottom" | "top",
            getPixelOffset: (d: { slot: number }) => {
              // 讓開**當下實際畫出來的本體**：遠距是 2D 圖示（貼地、螢幕
              // 高度隨俯角壓縮），近距是 3D mesh（較高）——取兩者較大者，
              // 各俯角的視覺縫才一致
              const rad = (pitchRef.current * Math.PI) / 180;
              const iconScale = Math.min(2.2, Math.pow(
                Math.max(0.2, Math.cos(rad)), -0.75));
              const iconHalf = Math.max(10, 22 * iconScale * Math.cos(rad));
              const lod = lodFactor(zoomRef.current);
              const meshHalf = 34 * (0.35 + 0.65 * Math.cos(rad)) * lod;
              const rank = Math.floor(d.slot / 2);
              const dist = Math.max(iconHalf, meshHalf) + 4 + rank * 12;
              return [0, d.slot % 2 === 0 ? -dist : dist];
            },
            outlineWidth: 2.5,
            outlineColor: [27, 26, 23, 255],   // 暖畫布底色描邊，任何背景可讀
            fontSettings: { sdf: true },
            // ⚠ getPixelOffset 依賴**俯角與縮放**（外部狀態），不列進
            // updateTriggers 的話 deck 會沿用第一次算出的值、改視角不重算
            updateTriggers: {
              getPosition: fleetIds, getText: fleetIds,
              getAlignmentBaseline: fleetIds,
              getPixelOffset: `${fleetIds}|${Math.round(pitchRef.current)}`
                + `|${zoomRef.current.toFixed(1)}`,
            },
          }),
        ] });

        // 地面投影仍是 maplibre setData（整源替換）：節流 1Hz 且僅樣本
        // 增加時重建。機體 marker 與置中不節流——位置要跟得上 5Hz
        const total = Object.values(s.trails).reduce((a, tr) => a + tr.length, 0);
        const now = performance.now();
        const gate = ribbonGateRef.current;
        if (total === gate.n || now - gate.t < 1000) return;
        ribbonGateRef.current = { t: now, n: total };

        // 地面投影＝機別色（誰飛的）；空中航跡＝SINR 分級（訊號如何）
        const trailEntries = Object.entries(s.trails);
        (map.getSource("trail") as maplibregl.GeoJSONSource | undefined)?.setData({
          type: "FeatureCollection",
          features: trailEntries
            .map(([id, tr]) => trailLineString(tr, { dcolor: colorFor(id) }))
            .filter((f): f is GeoJSON.Feature => f !== null),
        });
      }
    };

    const unsub = useUavStore.subscribe(paint);
    // **斷線閃爍必須由本地時鐘驅動。** 機一斷線，遙測就停了＝store 不再變動
    // ＝畫面不會重繪，於是「已經斷線」這件事會因為斷線本身而顯示不出來。
    // 這是 ui-spec §0.2g 那條通則的又一個實例：**故障的偵測不得依賴故障的
    // 那條路徑**。只在真的有機斷線時才跑，沒有斷線機時零成本。
    const timer = setInterval(() => {
      const s = useUavStore.getState();
      if (Object.values(s.fleet).some((t) => t.connected === false)) paint(s);
    }, 120);
    return () => { unsub(); clearInterval(timer); };
  }, []);

  return (
    <div className="map-wrap">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      <div className="coord-read" ref={coordRef} />

      {/* simple-first：左上機隊色點（>1 機才出現，點擊切換選中機） */}
      {dotIds.length > 1 && (
        <div className="fleet-dots">
          {dotIds.map((id) => (
            <button key={id}
              className={`${id === selId ? "on" : ""}`
                + ` ${formationOn && targetKey.split(",").includes(id) ? "tgt" : ""}`}
              title={useUavStore.getState().fleet[id]?.drone_name || id}
              onClick={() => {
                // 編隊：點色點＝toggle 目標集（指揮誰）；單機：切焦點
                const st = useUavStore.getState();
                if (st.formation) st.toggleTarget(id);
                else st.select(id);
              }}>
              {/* §0.1：色點一律配機身文字——3 台內是冗餘保險，超過 3 台
                  色盤耗盡（第 4 台起同一灰）時文字是唯一識別依據 */}
              <span className="dot" style={{ background: colorFor(id) }} />
              {useUavStore.getState().fleet[id]?.drone_name || id.slice(0, 6)}
            </button>
          ))}
        </div>
      )}
      {/* 右上三件套＝單一 flex 容器右對齊（批 2a 驗收 blocker 修正：
          固定偏移會撞面板寬度，flex 讓面板收合/展開時左鄰自然讓位）。
          面板被拖走（fixed 定位）後，其餘兩件自動靠右補位 */}
      <div className="top-stack">
        <button className={`drawer-btn ${panelOpen ? "on" : ""}`} title="詳細數值面板"
          onClick={() => useUavStore.getState().setPanelOpen(!panelOpen)}>▤</button>
        {/* §2.9 檢視收斂：進影像＝點 PiP 放大（不另存兩套切換）；
            全幅時只剩「地圖」返回鈕 */}
        {view === "video" && (
          <div className="view-toggle">
            <button onClick={() => setView("map")}>地圖</button>
          </div>
        )}
        {/* 右欄只剩任務控制（使用者二次修訂：事件卡併入 ▤ 抽屜） */}
        <CommandPanel />
      </div>

      {/* 軌跡顏色圖例：回歸左下常駐（ui-spec §2 使用者定案），HUD 上方 */}
      <div className="legend">
        <h4>訊號品質</h4>
        {/* 底圖切換住圖例卡內（§2.4b）：圖例本就是「地圖上有什麼」的說明處 */}
        {LINK_CLASSES.map((c) => (
          <div className="row" key={c.key}>
            <span className="dot" style={{ background: c.color }} />
            {c.label}
          </div>
        ))}
        <div className="row">
          <span className="dot" style={{ background: "#8f8b80", opacity: 0.6 }} />
          預計任務路徑
        </div>
        <div className="row">
          <span className="dot" style={{ background: "transparent", border: "1.5px solid #8f8b80" }} />
          起飛點
        </div>
        <BasemapToggle on={base.on} set={base.set}
          offline={base.offline} outside={base.outside} />
      </div>

      {/* 圖例已併入專業抽屜的訊號品質卡（單一住所，第五輪）——地圖上不再常駐 */}

      {/* §2.9 即時影像 PiP：選中機有影像源→右下 16:9 小窗（比例尺上方），
          跟隨選中機自動換源；無源不畫。點小窗＝放大為全幅（既有影像檢視
          收斂為放大態）；可收合成 📹 鈕 */}
      {view === "map" && selId && selUrl && !pipHidden && (
        <div className="video-pip" ref={pipRef} title="點擊放大／拖曳移動"
          style={pipPos ? { position: "fixed", left: pipPos.x, top: pipPos.y,
            right: "auto", bottom: "auto" } : undefined}
          onPointerDown={pipDown} onPointerMove={pipMove}
          onPointerUp={pipUp} onPointerCancel={pipUp}>
          <VideoPlayer key={`pip:${selId}:${selUrl}`} url={selUrl}
            controls={false} />
          {/* 機身識別徽章：§2.9 明文「不得因簡約原則移除」——真相機階段
              三台畫面可能極相似，這是唯一辨識依據 */}
          <div className="video-tile-label">
            <span className="dot" style={{ background: colorFor(selId) }} />
            {selName}
          </div>
          <button className="pip-hide" title="收合影像小窗"
            onClick={(e) => { e.stopPropagation(); setPipHidden(true); }}>—</button>
        </div>
      )}
      {view === "map" && selId && selUrl && pipHidden && (
        <button className="pip-restore" title="展開影像小窗"
          onClick={() => setPipHidden(false)}>📹</button>
      )}

      {/* 影像檢視（§2.9 放大態）：地圖保持 mounted（maplibre 重建昂貴且會
          失去視角），影像用覆蓋層蓋上去；切回地圖即卸載播放器、停掉串流 */}
      {view === "video" && (
        <div className="video-overlay">
          {selId == null ? (
            <div className="video-empty"><p>尚未收到無人機遙測。</p></div>
          ) : videoList === null ? (
            <div className="video-empty"><p>連線中…</p></div>
          ) : (() => {
            const url = videoList.find((d) => d.id === selId)?.video_url;
            return url ? (
              <VideoPlayer key={`${selId}:${url}`} url={url} />
            ) : (
              <div className="video-empty">
                <p>{selName} 尚未設定影像串流位址。</p>
                <p className="hint-line">
                  到「無人機」頁按「影像」設定 video_url（WHEP／MJPEG／MP4）。
                </p>
              </div>
            );
          })()}
          {selId && (
            <div className="video-tile-label">
              <span className="dot" style={{ background: colorFor(selId) }} />
              {selName}
            </div>
          )}
        </div>
      )}

      {/* simple-first HUD（數值列＋toast＋事件單行）；任務控制面板在 top-stack */}
      <SimpleHud />

      {videoDrone && (
        <VideoModal
          droneId={videoDrone}
          name={useUavStore.getState().fleet[videoDrone]?.drone_name || videoDrone}
          color={colorFor(videoDrone)}
          onClose={() => setVideoDrone(null)}
        />
      )}
    </div>
  );
}
