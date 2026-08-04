"use client";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef } from "react";

import { API, LINK_CLASSES, classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const HOME: [number, number] = [8.5456, 47.3977]; // PX4 SITL 預設起飛點

// 地圖畫布色。目前刻意**不放底圖**（設計決定，2026-08-04）：
//  - SITL 場景在蘇黎世，OSM 街圖沒有研究意義，反而增加視覺噪音
//  - 不抓外部 tile → 地面站離線（場域實測常態）也完全可用
//  - 深色畫布讓四段上色的軌跡對比最好
// 之後要加底圖（例如台灣場域用國土測繪中心 NLSC 正射影像），
// 在 style.sources 加 raster source、layers **最前面**插 raster layer 即可，
// 其餘圖層順序不動。
const CANVAS = "#14181c";

/** 以中心點/半徑產生圓形 polygon（干擾區顯示用） */
function circlePolygon(lat: number, lon: number, radiusM: number): GeoJSON.Feature {
  const pts: [number, number][] = [];
  for (let i = 0; i <= 48; i++) {
    const a = (i / 48) * 2 * Math.PI;
    pts.push([
      lon + ((radiusM * Math.cos(a)) / 111320) / Math.cos((lat * Math.PI) / 180),
      lat + (radiusM * Math.sin(a)) / 110574,
    ]);
  }
  return { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [pts] } };
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
    // 無底圖時距離感只剩比例尺，必加（干擾區半徑 120m 這類尺度要對得上）
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;

    map.on("load", async () => {
      // 干擾區與基地台（靜態設定，由 API 載入）
      const [zones, cells] = await Promise.all([
        fetch(`${API}/api/zones`).then((r) => r.json()).catch(() => []),
        fetch(`${API}/api/cells`).then((r) => r.json()).catch(() => []),
      ]);

      map.addSource("zones", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: zones
            .filter((z: any) => z.enabled)
            .map((z: any) => circlePolygon(z.center_lat, z.center_lon, z.radius_m)),
        },
      });
      map.addLayer({
        id: "zones-fill", type: "fill", source: "zones",
        paint: { "fill-color": "#a01818", "fill-opacity": 0.14 },
      });
      map.addLayer({
        id: "zones-line", type: "line", source: "zones",
        paint: { "line-color": "#a01818", "line-width": 2, "line-dasharray": [2, 2] },
      });

      map.addSource("cells", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: cells.map((c: any) => ({
            type: "Feature",
            properties: { name: `${c.name} (PCI ${c.pci})` },
            geometry: { type: "Point", coordinates: [c.lon, c.lat] },
          })),
        },
      });
      map.addLayer({
        id: "cells-pt", type: "circle", source: "cells",
        paint: {
          "circle-radius": 6, "circle-color": "#2a78d6",
          "circle-stroke-width": 2, "circle-stroke-color": "#fcfcfb",
        },
      });

      // 飛行軌跡：依鏈路健康分級上色（門檻同 backend 事件門檻）
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
          // 描邊用畫布色：重疊的軌跡點之間出現「畫布縫」才分得開；
          // 白色 halo 在深色畫布上是發光效果，反而糊掉相鄰點的邊界
          "circle-stroke-width": 1.5,
          "circle-stroke-color": CANVAS,
        },
      });
    });

    return () => map.remove();
  }, []);

  // 即時更新：無人機位置 marker + 軌跡
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

        const src = map.getSource("trail") as maplibregl.GeoJSONSource | undefined;
        src?.setData({
          type: "FeatureCollection",
          features: s.trail.map((p) => ({
            type: "Feature",
            properties: { cls: p.sinr == null ? "unknown" : classifySinr(p.sinr).key },
            geometry: { type: "Point", coordinates: [p.lon, p.lat] },
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
          <span className="dot" style={{ background: "#a01818", opacity: 0.45 }} />
          干擾區
          <span className="dot" style={{ background: "#2a78d6" }} />
          gNB 基地台
        </div>
      </div>
    </div>
  );
}
