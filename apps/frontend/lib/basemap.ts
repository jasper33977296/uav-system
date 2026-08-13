"use client";
/** 底圖（NLSC 正射影像，ui-spec §2.4b）——即時頁與回放頁共用。
 *
 * 三種狀態各自對應不同事實，不可混用：
 *   開啟且有圖  → 影像＋暖色暗化
 *   離線        → 退回深色畫布，標「底圖離線」（瓦片抓不到）
 *   涵蓋範圍外  → 退回深色畫布，標「此區無影像（圖資僅涵蓋臺灣）」
 *                 ——境外座標伺服器回 200 但給空白磚，不說會被當成故障
 *
 * CVD 註記：影像背景是非均勻底色，現行分級色盤僅在 #1b1a17 純色上驗過，
 * 影像背景之對比**尚未驗證**（規格已標，取樣需臺灣座標）。
 */
import type maplibregl from "maplibre-gl";
import { useCallback, useEffect, useState } from "react";

import { CANVAS } from "./geo";

const TILES = "https://wmts.nlsc.gov.tw/wmts/PHOTO2/default/GoogleMapsCompatible/{z}/{y}/{x}";
// 臺灣本島＋離島的寬鬆包絡（涵蓋範圍偵測用，非精確邊界）
const inTaiwan = (lng: number, lat: number) =>
  lng > 118 && lng < 123.5 && lat > 20 && lat < 26.5;

export function useBasemap() {
  const [on, setOn] = useState(false);
  const [offline, setOffline] = useState(false);
  const [outside, setOutside] = useState(false);
  const [mapRef, setMapRef] = useState<maplibregl.Map | null>(null);

  useEffect(() => { setOn(localStorage.getItem("map-basemap") === "1"); }, []);

  const set = useCallback((v: boolean) => {
    setOn(v);
    setOffline(false);
    localStorage.setItem("map-basemap", v ? "1" : "0");
  }, []);

  /** 在 map 的 load 事件內呼叫：建圖層（預設隱藏）並掛偵測 */
  const install = useCallback((map: maplibregl.Map, beforeId?: string) => {
    if (map.getSource("nlsc")) return;
    map.addSource("nlsc", {
      type: "raster", tileSize: 256, maxzoom: 20,
      attribution: "© 內政部國土測繪中心", tiles: [TILES],
    });
    map.addLayer({ id: "basemap", type: "raster", source: "nlsc",
      layout: { visibility: "none" } }, beforeId);
    // 暗化：分級色必須在最亮地物（水泥/屋頂）上仍可辨。
    // 80% 為 CVD 重驗定案值（設計師 2026-08-12，取樣 NLSC 台北 z17 實影像）：
    // 50% 時最亮地物上四色**全部 <3:1**（良好 1.49／尚可 2.73／劣化 1.38／
    // 瀕斷 1.59）——影像亮處會把整套分級色吃掉；80% 後只剩瀕斷 #a01818
    // 未過（1.39），但它在無底圖畫布上本來就是 1.79、早有既有 relief
    // （事件流卡片／側欄分級標籤／圖表事件標記），未新增失效模式。
    // **調暗化濃度而非改色盤**——色盤動了要重驗全站。
    map.addLayer({ id: "basemap-dim", type: "background",
      layout: { visibility: "none" },
      paint: { "background-color": CANVAS, "background-opacity": 0.8 } }, beforeId);
    map.on("error", (e) => {
      if ((e as unknown as { sourceId?: string }).sourceId === "nlsc") setOffline(true);
    });
    const upd = () => {
      const c = map.getCenter();
      setOutside(!inTaiwan(c.lng, c.lat));
    };
    upd();
    map.on("moveend", upd);
    setMapRef(map);
  }, []);

  // 顯隱只切 visibility（不重建地圖，deck overlay 不受影響）。
  // try/catch 是必要的：呼叫端可能重建地圖（回放頁在 rows/plan 變動時
  // 會 map.remove()），此時 hook 手上的舊實例 style 已消失，maplibre 內部
  // 會炸出 TypeError 讓整頁白畫面——舊實例直接當作沒事發生
  useEffect(() => {
    try {
      if (!mapRef || !mapRef.getLayer("basemap")) return;
      const v = on && !offline ? "visible" : "none";
      mapRef.setLayoutProperty("basemap", "visibility", v);
      mapRef.setLayoutProperty("basemap-dim", "visibility", v);
    } catch { /* 地圖已被銷毀或 style 尚未就緒 */ }
  }, [mapRef, on, offline]);

  return { on, set, offline, outside, install };
}
