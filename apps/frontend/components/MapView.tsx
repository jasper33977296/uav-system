"use client";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";

import CommandPanel from "@/components/CommandPanel";
import { colorFor, createDroneLayer, pickDrone, type ScreenHit } from "@/components/droneLayer";
import SimpleHud from "@/components/SimpleHud";
import VideoModal from "@/components/VideoModal";
import VideoPlayer from "@/components/VideoPlayer";
import { pathsLayer, routeLayer, type RouteRun } from "@/lib/deckRoute";
import { basePreview, separatePreview, unifiedPreview, type Wp } from "@/lib/formation";
import { CANVAS, groundGrid, ribbon, trailLineString } from "@/lib/geo";
import { API, LINK_CLASSES } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const HOME: [number, number] = [8.5456, 47.3977]; // PX4 SITL 預設起飛點

// 刻意**不放底圖**：場域物件不存在於系統認知中，鏈路品質的空間分布由
// 實測軌跡自己揭露；離線（場域實測常態）也完全可用。
// 要加底圖時在 style.sources 加 raster source、layers 最前面插一層即可。

interface DroneVideo { id: string; name: string; video_url: string | null }

export default function MapView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const centeredRef = useRef(false);
  const hitsRef = useRef<Map<string, ScreenHit>>(new Map());
  const [videoDrone, setVideoDrone] = useState<string | null>(null);
  const coordRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const refreshPlanRef = useRef<() => void>(() => {});
  // 任務開始成功（CommandPanel 自動 activate）→ 疊圖即刻重刷
  const planReq = useUavStore((s) => s.planReq);
  useEffect(() => { refreshPlanRef.current(); }, [planReq]);
  const ribbonGateRef = useRef({ t: 0, n: -1 });   // 地面投影重建節流

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
    fetch(`${API}/api/drones`).then((r) => r.json())
      .then(setVideoList).catch(() => setVideoList([]));
  }, [view, selId]);
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
      center: HOME,
      zoom: 15,
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

    map.on("load", async () => {
      // 地面基準：網格 50m 一格 + 起飛點雙圈錨點
      map.addSource("grid", { type: "geojson", data: groundGrid(HOME[1], HOME[0]) });
      map.addLayer({
        id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#262624", "line-width": 1 },
      });
      map.addSource("home", {
        type: "geojson",
        data: { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: HOME } },
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
          "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1, 16, 2, 20, 3.5],
          "line-opacity": 0.7,
        },
      });

      // 實際路徑：deck.gl PathLayer（route-render-tool-eval 定案，取代
      // fill-extrusion 絲帶）——interleaved 模式與 maplibre 同一 GL context，
      // 與 three.js 球體自訂層共存（interop 是選型條件 3，落地後實測）
      const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
      map.addControl(overlay as unknown as maplibregl.IControl);
      overlayRef.current = overlay;

      // 無人機本體：three.js 正圓球體，浮在實際高度。
      // 讀整個 fleet——多機時幾台就畫幾台，顏色依出現順序取機隊色盤。
      map.addLayer(createDroneLayer("drones", () =>
        Object.entries(useUavStore.getState().fleet)
          .filter(([, t]) => t.lat != null && t.lon != null)
          .map(([id, t]) => ({
            id, lat: t.lat!, lon: t.lon!, alt: t.alt_rel ?? 0, color: colorFor(id),
          })), hitsRef.current));

      // 點擊機體 → 即時畫面 modal（自訂層不在 queryRenderedFeatures 裡，
      // 用 render 時算好的螢幕位置自行命中）
      map.on("click", (e) => {
        const id = pickDrone(hitsRef.current, e.point.x, e.point.y);
        if (!id) return;
        // 編隊模式：點機體球＝切焦點（看誰）；單機模式照舊開即時影像
        const st = useUavStore.getState();
        if (st.formation) st.select(id);
        else setVideoDrone(id);
      });
      map.on("mousemove", (e) => {
        map.getCanvas().style.cursor =
          pickDrone(hitsRef.current, e.point.x, e.point.y) ? "pointer" : "";
        // 游標經緯度（issue 017 P1）：直接寫 DOM——60Hz 的 mousemove
        // 走 React state 會整個元件重渲染
        if (coordRef.current) {
          coordRef.current.textContent =
            `${e.lngLat.lat.toFixed(6)}, ${e.lngLat.lng.toFixed(6)}`;
        }
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

      const refreshPlan = async () => {
        try {
          let wps: any[] = [];
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
  useEffect(
    () =>
      useUavStore.subscribe((s) => {
        const map = mapRef.current;
        const t = s.live;
        if (!map || !t || t.lat == null || t.lon == null) return;

        if (!markerRef.current) {
          const el = document.createElement("div");
          el.className = "drone-marker";
          markerRef.current = new maplibregl.Marker({ element: el, rotationAlignment: "map" })
            .setLngLat([t.lon, t.lat])
            .addTo(map);
        }
        markerRef.current.setLngLat([t.lon, t.lat]).setRotation(t.heading ?? 0);

        if (!centeredRef.current) {
          centeredRef.current = true;
          map.jumpTo({ center: [t.lon, t.lat], zoom: 16 });
        }

        // 球體層自己每幀從 store 讀位置（triggerRepaint 驅動），不需在此餵資料

        // 空中航跡：deck.gl PathLayer——attribute 更新是同幀 GPU buffer
        // 寫入（無 setData 整源替換的閃爍），5Hz 直更不需節流。
        // 編隊預覽層一併掛上（非編隊時為空陣列，零成本）
        overlayRef.current?.setProps({ layers: [
          routeLayer("route3d", s.trails),
          pathsLayer("formation-preview", previewRef.current),
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

      }),
    []
  );

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
              style={{ background: colorFor(id) }}
              onClick={() => {
                // 編隊：點色點＝toggle 目標集（指揮誰）；單機：切焦點
                const st = useUavStore.getState();
                if (st.formation) st.toggleTarget(id);
                else st.select(id);
              }} />
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
