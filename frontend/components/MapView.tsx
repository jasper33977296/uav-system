"use client";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef } from "react";

import { LINK_CLASSES, classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const HOME: [number, number] = [8.5456, 47.3977]; // PX4 SITL 預設起飛點

// 地圖畫布色。刻意**不放底圖**（設計決定，2026-08-04）：
//  - 場域物件（基地台、干擾區等）不存在於系統認知中——系統對干擾無先驗知識，
//    鏈路品質的空間分布由實測軌跡自己揭露（那是研究產出物，不是輸入）
//  - 不抓外部 tile → 地面站離線（場域實測常態）也完全可用
// 之後要加底圖（例如 NLSC 正射影像），在 style.sources 加 raster source、
// layers 最前面插 raster layer 即可，其餘圖層順序不動。
const CANVAS = "#14181c";
const DRONE_COLOR = "#3987e5";

const M_LAT = 110574;                       // 一度緯度的公尺數
const mLon = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

/** 地面網格：無底圖時的地面基準。每 stepM 一條線，覆蓋 ±halfM。 */
function groundGrid(lat: number, lon: number, halfM = 400, stepM = 50): GeoJSON.FeatureCollection {
  const feats: GeoJSON.Feature[] = [];
  const line = (a: [number, number], b: [number, number]): GeoJSON.Feature => ({
    type: "Feature", properties: {},
    geometry: { type: "LineString", coordinates: [a, b] },
  });
  for (let m = -halfM; m <= halfM; m += stepM) {
    feats.push(line([lon + m / mLon(lat), lat - halfM / M_LAT],
                    [lon + m / mLon(lat), lat + halfM / M_LAT]));
    feats.push(line([lon - halfM / mLon(lat), lat + m / M_LAT],
                    [lon + halfM / mLon(lat), lat + m / M_LAT]));
  }
  return { type: "FeatureCollection", features: feats };
}

/** 以點為中心的正多邊形（3D 柱底／機體底面） */
function ngonAt(lat: number, lon: number, halfM: number, n = 4): GeoJSON.Polygon {
  const pts: [number, number][] = [];
  for (let i = 0; i <= n; i++) {
    const a = (i / n) * 2 * Math.PI + Math.PI / n;
    pts.push([lon + (halfM * Math.cos(a)) / mLon(lat), lat + (halfM * Math.sin(a)) / M_LAT]);
  }
  return { type: "Polygon", coordinates: [pts] };
}

/** 無人機 3D 本體：八角柱近似球體，浮在實際飛行高度。
    MapLibre 沒有球體 primitive，fill-extrusion 八角柱在這個尺寸讀起來就是
    一顆懸浮機體——升降時整顆上下移動，高度差直接可見。 */
function droneBall(lat: number, lon: number, alt: number): GeoJSON.Feature {
  const r = 3.2;
  return {
    type: "Feature",
    properties: { base: Math.max(alt - r, 0), top: Math.max(alt + r, r) },
    geometry: ngonAt(lat, lon, r, 8),
  };
}

export default function MapView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const centeredRef = useRef(false);

  useEffect(() => {
    const map = new maplibregl.Map({
      container: containerRef.current!,
      center: HOME,
      zoom: 15,
      pitch: 55,                 // 3D 傾斜視角：高度差才看得出來（右鍵拖曳可調）
      maxPitch: 75,
      style: {
        version: 8,
        sources: {
          // ← 底圖插槽：需要時在此加 raster source（見檔頭 CANVAS 註解）
        },
        layers: [
          { id: "canvas", type: "background", paint: { "background-color": CANVAS } },
        ],
      },
    });
    // 無底圖時距離感只剩比例尺，必加
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;

    map.on("load", () => {
      // 地面基準：無底圖時「地在哪裡」由網格回答，50m 一格（配合比例尺讀距離）
      map.addSource("grid", { type: "geojson", data: groundGrid(HOME[1], HOME[0]) });
      map.addLayer({
        id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#232a31", "line-width": 1 },
      });
      // 起飛點（雙圈標記）：地面錨點
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

      // 地面軌跡（= 3D 軌跡的投影）：依鏈路健康分級上色
      map.addSource("trail", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "trail-pt", type: "circle", source: "trail",
        paint: {
          "circle-radius": 4,
          "circle-color": [
            "match", ["get", "cls"],
            ...LINK_CLASSES.flatMap((c) => [c.key, c.color]),
            "#898781",
          ] as any,
          // 不描邊：5Hz 推送下點距不到 1px，描邊會把相鄰點的填色蓋掉
          "circle-stroke-width": 0,
        },
      });

      // 3D 軌跡柱：柱頂是航跡、柱底是投影，高度差在傾斜視角下直接可讀
      map.addSource("trail3d", { type: "geojson",
        data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "trail3d", type: "fill-extrusion", source: "trail3d",
        paint: {
          "fill-extrusion-color": [
            "match", ["get", "cls"],
            ...LINK_CLASSES.flatMap((c) => [c.key, c.color]),
            "#898781",
          ] as any,
          "fill-extrusion-height": ["get", "h"],
          "fill-extrusion-base": 0,
          "fill-extrusion-opacity": 0.55,
        },
      });

      // 無人機 3D 本體：浮在實際高度，升降直接可見
      map.addSource("drone3d", { type: "geojson",
        data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "drone3d", type: "fill-extrusion", source: "drone3d",
        paint: {
          "fill-extrusion-color": DRONE_COLOR,
          "fill-extrusion-height": ["get", "top"],
          "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.95,
        },
      });
    });

    return () => map.remove();
  }, []);

  // 即時更新：無人機 3D 本體 + 地面航向投影 + 軌跡
  useEffect(
    () =>
      useUavStore.subscribe((s) => {
        const map = mapRef.current;
        const t = s.live;
        if (!map || !t || t.lat == null || t.lon == null) return;

        // 地面投影三角形表示航向（3D 機體不易表達朝向，兩者互補）
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

        (map.getSource("drone3d") as maplibregl.GeoJSONSource | undefined)?.setData({
          type: "FeatureCollection",
          features: [droneBall(t.lat, t.lon, t.alt_rel ?? 0)],
        });

        (map.getSource("trail") as maplibregl.GeoJSONSource | undefined)?.setData({
          type: "FeatureCollection",
          features: s.trail.map((p) => ({
            type: "Feature",
            properties: { cls: p.sinr == null ? "unknown" : classifySinr(p.sinr).key },
            geometry: { type: "Point", coordinates: [p.lon, p.lat] },
          })),
        });

        // 3D 柱隔 3 點取一柱：5Hz 下柱距約 3m，視覺已連續，幾何量省 2/3
        (map.getSource("trail3d") as maplibregl.GeoJSONSource | undefined)?.setData({
          type: "FeatureCollection",
          features: s.trail
            .filter((_, i) => i % 3 === 0)
            .map((p) => ({
              type: "Feature",
              properties: {
                cls: p.sinr == null ? "unknown" : classifySinr(p.sinr).key,
                h: p.alt ?? 0,
              },
              geometry: ngonAt(p.lat, p.lon, 1.6),
            })),
        });
      }),
    []
  );

  return (
    <div className="map-wrap">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      <div className="legend">
        <h4>鏈路品質（SINR）</h4>
        {LINK_CLASSES.map((c) => (
          <div className="row" key={c.key}>
            <span className="dot" style={{ background: c.color }} />
            {c.label}
          </div>
        ))}
        <div className="row">
          <span className="dot" style={{ background: DRONE_COLOR }} />
          無人機（懸浮於實際高度）
        </div>
        <div className="row">
          <span className="dot" style={{ background: "transparent", border: "1.5px solid #898781" }} />
          起飛點（地面基準）
        </div>
      </div>
    </div>
  );
}
