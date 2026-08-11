"use client";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";

import CommandPanel from "@/components/CommandPanel";
import { colorFor, createDroneLayer, pickDrone, type ScreenHit } from "@/components/droneLayer";
import VideoModal from "@/components/VideoModal";
import VideoPlayer from "@/components/VideoPlayer";
import { CANVAS, groundGrid, ribbon, trailLineString } from "@/lib/geo";
import { API, LINK_CLASSES, classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const HOME: [number, number] = [8.5456, 47.3977]; // PX4 SITL 預設起飛點

// 刻意**不放底圖**：場域物件不存在於系統認知中，鏈路品質的空間分布由
// 實測軌跡自己揭露；離線（場域實測常態）也完全可用。
// 要加底圖時在 style.sources 加 raster source、layers 最前面插一層即可。

const CLS_MATCH = [
  "match", ["get", "cls"],
  ...LINK_CLASSES.flatMap((c) => [c.key, c.color]),
  "#898781",
] as any;

interface DroneVideo { id: string; name: string; video_url: string | null }

export default function MapView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const centeredRef = useRef(false);
  const [hasMission, setHasMission] = useState(false);
  const hitsRef = useRef<Map<string, ScreenHit>>(new Map());
  const [videoDrone, setVideoDrone] = useState<string | null>(null);
  const coordRef = useRef<HTMLDivElement>(null);

  // 檢視切換：地圖 ↔ 當前選擇機（側欄選的，未選＝主機）的即時影像
  const [view, setView] = useState<"map" | "video">("map");
  const [videoList, setVideoList] = useState<DroneVideo[] | null>(null);
  const selId = useUavStore((s) => s.selectedId ?? s.primaryId);
  const selName = useUavStore((s) => {
    const id = s.selectedId ?? s.primaryId;
    return id ? s.fleet[id]?.drone_name ?? id : null;
  });
  // 影像檢視才撈機清單：video_url 在無人機頁隨時可改，切進來時讀最新值
  useEffect(() => {
    if (view !== "video") return;
    fetch(`${API}/api/drones`).then((r) => r.json())
      .then(setVideoList).catch(() => setVideoList([]));
  }, [view]);


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
    });
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;

    map.on("load", async () => {
      // 地面基準：網格 50m 一格 + 起飛點雙圈錨點
      map.addSource("grid", { type: "geojson", data: groundGrid(HOME[1], HOME[0]) });
      map.addLayer({
        id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#232a31", "line-width": 1 },
      });
      map.addSource("home", {
        type: "geojson",
        data: { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: HOME } },
      });
      map.addLayer({
        id: "home-ring", type: "circle", source: "home",
        paint: {
          "circle-radius": 10, "circle-color": "transparent",
          "circle-stroke-width": 2, "circle-stroke-color": "#898781",
        },
      });
      map.addLayer({
        id: "home-dot", type: "circle", source: "home",
        paint: { "circle-radius": 3, "circle-color": "#898781" },
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
          "line-width": 2,
          "line-opacity": 0.7,
        },
      });

      // 實際路徑：懸浮在飛行高度的彩色平面絲帶（依 SINR 分級上色）
      map.addSource("path3d", { type: "geojson",
        data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "path3d", type: "fill-extrusion", source: "path3d",
        paint: {
          "fill-extrusion-color": CLS_MATCH,
          "fill-extrusion-height": ["get", "top"],
          "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.9,
        },
      });

      // 無人機本體：three.js 正圓球體，浮在實際高度。
      // 讀整個 fleet——群飛時幾台就畫幾台，顏色依出現順序取機隊色盤。
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
        if (id) setVideoDrone(id);
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
      try {
        let wps: any[] = [];
        const ra = await fetch(`${API}/api/missions/active`);
        if (ra.ok) wps = ((await ra.json()).waypoints ?? []);
        {
          wps = wps.filter((w: any) => w.lat && w.lon);
          if (wps.length >= 2) {
            // 預計路徑：窄灰絲帶浮在各段目標高度（比實際路徑窄且淡，主從分明）
            map.addSource("plan3d", {
              type: "geojson",
              data: ribbon(
                wps.map((w: any) => ({ lat: w.lat, lon: w.lon, alt: w.alt })),
                () => ({}), 1.0),
            });
            map.addLayer({
              id: "plan3d", type: "fill-extrusion", source: "plan3d",
              paint: {
                "fill-extrusion-color": "#8a94a3",
                "fill-extrusion-height": ["get", "top"],
                "fill-extrusion-base": ["get", "base"],
                "fill-extrusion-opacity": 0.35,
              },
            }, "path3d");
            // 地面投影：虛線＋航點圈
            map.addSource("plan-ground", {
              type: "geojson",
              data: {
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
              },
            });
            map.addLayer({
              id: "plan-ground-line", type: "line", source: "plan-ground",
              filter: ["!", ["has", "pt"]],
              paint: { "line-color": "#8a94a3", "line-width": 1.5,
                       "line-dasharray": [3, 3], "line-opacity": 0.6 },
            }, "trail-line");
            map.addLayer({
              id: "plan-ground-wp", type: "circle", source: "plan-ground",
              filter: ["has", "pt"],
              paint: { "circle-radius": 4, "circle-color": "transparent",
                       "circle-stroke-width": 1.5, "circle-stroke-color": "#8a94a3" },
            }, "trail-line");
            setHasMission(true);
          }
        }
      } catch { /* 無任務即無疊圖 */ }
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

        // 地面投影＝機別色（誰飛的）；空中絲帶＝SINR 分級（訊號如何）
        const trailEntries = Object.entries(s.trails);
        (map.getSource("trail") as maplibregl.GeoJSONSource | undefined)?.setData({
          type: "FeatureCollection",
          features: trailEntries
            .map(([id, tr]) => trailLineString(tr, { dcolor: colorFor(id) }))
            .filter((f): f is GeoJSON.Feature => f !== null),
        });

        // 絲帶隔 2 點取一段：5Hz 下段長約 2m，視覺連續
        (map.getSource("path3d") as maplibregl.GeoJSONSource | undefined)?.setData({
          type: "FeatureCollection",
          features: trailEntries.flatMap(([, tr]) =>
            ribbon(
              tr.filter((_, i) => i % 2 === 0),
              (_a, b) => ({ cls: b.sinr == null ? "unknown" : classifySinr(b.sinr).key }),
            ).features),
        });


      }),
    []
  );

  return (
    <div className="map-wrap">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      <div className="coord-read" ref={coordRef} />
      <div className="legend">
        <h4>鏈路品質（SINR）</h4>
        {LINK_CLASSES.map((c) => (
          <div className="row" key={c.key}>
            <span className="dot" style={{ background: c.color }} />
            {c.label}
          </div>
        ))}

        {hasMission && (
          <div className="row">
            <span className="dot" style={{ background: "#8a94a3", opacity: 0.6 }} />
            預計任務路徑（自機上讀回）
          </div>
        )}
        <div className="row">
          <span className="dot" style={{ background: "transparent", border: "1.5px solid #898781" }} />
          起飛點（地面基準）
        </div>
      </div>

      {/* 檢視切換鈕：地圖保持 mounted（maplibre 重建昂貴且會失去視角），
          影像用覆蓋層蓋上去；切回地圖即卸載播放器、停掉串流 */}
      <div className="view-toggle">
        <button className={view === "map" ? "on" : ""}
          onClick={() => setView("map")}>地圖</button>
        <button className={view === "video" ? "on" : ""}
          onClick={() => setView("video")}>影像</button>
      </div>

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

      <CommandPanel />

      {videoDrone && (
        <VideoModal
          droneId={videoDrone}
          name={useUavStore.getState().fleet[videoDrone]?.drone_name ?? videoDrone}
          color={colorFor(videoDrone)}
          onClose={() => setVideoDrone(null)}
        />
      )}
    </div>
  );
}
