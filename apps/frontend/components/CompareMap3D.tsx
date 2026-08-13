"use client";
/** 比較頁 3D 軌跡疊圖（doc/compare-drones-restyle.md §1）：與即時/回放頁
 * 同一套 3D 語言（傾斜視角＋暖畫布＋網格＋起飛點，geo.ts 重用）。
 *
 * 色彩語意（兩色盤不混用原則）：
 *   - 多線比較＝identity——絲帶用航線類別色，與下方圖表同色同序；
 *     圖表「高亮 ≤3」的 dim 集合同步（dim＝muted 細帶）
 *   - 選中單一架次（點絲帶或圖例）＝該絲帶切 SINR 分級色、其餘退 muted——
 *     單線時狀態色才有意義，這是狀態色在本頁唯一的入口
 */
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";

import { pathsLayer, rgba, sinrRuns, type RouteRun } from "@/lib/deckRoute";
import { firstFleetPos } from "@/lib/store";
import { CANVAS, groundGrid, ribbon } from "@/lib/geo";
import { LINK_CLASSES } from "@/lib/signal";

const MUTED = "#8f8b80";   // ＝--muted（maplibre 吃不到 CSS 變數）

interface Row { lat: number | null; lon: number | null; [k: string]: unknown }

interface Props {
  wps: { lat: number; lon: number; alt?: number }[];
  loaded: string[];
  tracks: Record<string, Row[]>;
  colorOf: (sid: string) => string;
  labelOf: (sid: string) => string;
  dimIds: string[];
}

export default function CompareMap3D({
  wps, loaded, tracks, colorOf, labelOf, dimIds,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const [ready, setReady] = useState(false);
  const fittedRef = useRef(false);
  const pendingBoundsRef = useRef<maplibregl.LngLatBounds | null>(null);

  // 初始取景。歷經三修仍過縮後棄用 fitBounds（node 端重現證明 bounds 資料
  // 正確、是 fitBounds 呼叫算出的 zoom 不對）——改確定性取景：自己從 bounds
  // 公尺跨度算 zoom、jumpTo 置中（即時頁同款路徑，已證實可靠）。
  // 時機仍靠 ResizeObserver 等容器首次非零尺寸（dev 下 CSS 注入晚於 load）；
  // 離群點由呼叫端以 P1–P99 分位裁掉
  const tryFit = () => {
    const map = mapRef.current;
    const el = containerRef.current;
    const b = pendingBoundsRef.current;
    if (!map || !el || !b || fittedRef.current) return;
    const W = el.clientWidth, H = el.clientHeight;
    if (H < 50) return;   // CSS 尚未套用，等下一次 resize 通知
    fittedRef.current = true;
    map.resize();
    const c = b.getCenter();
    const cosLat = Math.cos((c.lat * Math.PI) / 180);
    const spanM = {
      x: (b.getEast() - b.getWest()) * 111320 * cosLat,
      y: (b.getNorth() - b.getSouth()) * 110574,
    };
    // 每像素公尺數取兩軸較大者（含 48px 邊距），zoom 反推自 512-tile 尺度
    const mpp = Math.max(spanM.x / (W - 96), spanM.y / (H - 96), 0.05);
    const zoom = Math.min(Math.log2((78271.517 * cosLat) / mpp), 19);
    // 給驗收 rig 抓數值用（headless 量測取證，issue 由來見 git log）
    console.debug("[cmp3d] fit", {
      w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth(),
      spanM, viewport: { W, H }, zoom,
    });
    map.jumpTo({ center: c, zoom, pitch: 55 });
    // 取證：jumpTo 是否真的生效（v4 後仍見 zoom≈12——若 applied=計算值而
    // moveend 又見別的 zoom，就是有後發呼叫在蓋）
    console.debug("[cmp3d] applied", { zoom: map.getZoom(), center: map.getCenter() });
  };
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => tryFit());
    ro.observe(el);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [sel, setSel] = useState<string | null>(null);
  const selRef = useRef(sel);
  useEffect(() => { selRef.current = sel; }, [sel]);

  useEffect(() => {
    // 原點取航點；沒有航點時退回機隊實際座標，再沒有就世界視野
    // （不寫死地點——SITL 舊出生點在機隊搬家後就是錯的）
    const origin: [number, number] = wps.length
      ? [wps[0].lon, wps[0].lat] : (firstFleetPos() ?? [0, 20]);
    const map = new maplibregl.Map({
      container: containerRef.current!,
      center: origin,
      zoom: wps.length || firstFleetPos() ? 15 : 1.5,
      pitch: 55,
      maxPitch: 75,
      // 嵌在可捲動頁面裡的地圖必開（2026-08-11 取景懸案真兇）：滾輪捲頁
      // 經過卡片會被攔成地圖縮放——rig 每輪捲等量滾輪，才會四修都得到
      // 一模一樣的 zoom 12.15。Ctrl+滾輪才縮放，捲頁歸捲頁
      cooperativeGestures: true,
      locale: {
        "CooperativeGesturesHandler.WindowsHelpText": "按住 Ctrl 並滾動以縮放地圖",
        "CooperativeGesturesHandler.MacHelpText": "按住 ⌘ 並滾動以縮放地圖",
      },
      style: {
        version: 8,
        sources: {},
        layers: [{ id: "canvas", type: "background",
                   paint: { "background-color": CANVAS } }],
      },
    });
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }),
      "bottom-left");
    mapRef.current = map;

    map.on("load", () => {
      map.resize();   // 保險：容器尺寸若在載入後才定，canvas 跟上
      map.addSource("grid", { type: "geojson", data: groundGrid(origin[1], origin[0]) });
      map.addLayer({ id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#262624", "line-width": 1 } });
      map.addSource("home", { type: "geojson",
        data: { type: "Feature", properties: {},
                geometry: { type: "Point", coordinates: origin } } });
      map.addLayer({ id: "home-ring", type: "circle", source: "home",
        paint: { "circle-radius": 10, "circle-color": "transparent",
                 "circle-stroke-width": 2, "circle-stroke-color": MUTED } });
      map.addLayer({ id: "home-dot", type: "circle", source: "home",
        paint: { "circle-radius": 3, "circle-color": MUTED } });

      // 計畫路徑：灰絲帶＋地面虛線＋航點圈（同即時頁主從關係）
      if (wps.length >= 2) {
        map.addSource("plan3d", { type: "geojson",
          data: ribbon(wps.map((w) => ({ lat: w.lat, lon: w.lon, alt: w.alt ?? 0 })),
                       () => ({}), 1.0) });
        map.addLayer({ id: "plan3d", type: "fill-extrusion", source: "plan3d",
          paint: { "fill-extrusion-color": MUTED,
                   "fill-extrusion-height": ["get", "top"],
                   "fill-extrusion-base": ["get", "base"],
                   "fill-extrusion-opacity": 0.35 } });
        map.addSource("plan-ground", { type: "geojson",
          data: { type: "Feature", properties: {}, geometry: {
            type: "LineString",
            coordinates: wps.map((w) => [w.lon, w.lat]) } } });
        map.addLayer({ id: "plan-ground", type: "line", source: "plan-ground",
          paint: { "line-color": MUTED, "line-width": 1.5,
                   "line-dasharray": [3, 3], "line-opacity": 0.6 } });
      }

      // 架次航跡：deck.gl PathLayer（與即時/回放同一渲染，顆粒/閃爍同修）
      const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
      map.addControl(overlay as unknown as maplibregl.IControl);
      overlayRef.current = overlay;

      // 點絲帶＝選中該架次（再點一次或點空白取消）；拾取走 deck
      map.on("click", (e) => {
        const info = overlay.pickObject({ x: e.point.x, y: e.point.y });
        const sid = (info?.object as RouteRun | undefined)?.sid;
        setSel((cur) => (sid ? (cur === sid ? null : sid) : null));
      });
      map.on("mousemove", (e) => {
        const info = overlay.pickObject({ x: e.point.x, y: e.point.y });
        map.getCanvas().style.cursor = info ? "pointer" : "";
      });
      // 取證：所有相機移動都記錄——zoom≈12 的覆蓋源會在這裡現形
      map.on("moveend", () => {
        console.debug("[cmp3d] moveend", { zoom: map.getZoom(), pitch: map.getPitch() });
      });
      setReady(true);
    });

    return () => map.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 絲帶資料：loaded/tracks/dim/選中變動時重建（identity ↔ SINR 上色）
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const runs: RouteRun[] = [];
    const allLats: number[] = [];
    const allLons: number[] = [];
    for (const sid of loaded) {
      const rows = (tracks[sid] ?? [])
        .filter((r) => r.lat != null && r.lon != null)
        .filter((_, i) => i % 3 === 0)
        .map((r) => ({ lat: r.lat as number, lon: r.lon as number,
                       alt: (r.alt_rel as number | null) ?? 0,
                       sinr: r.sinr as number | null }));
      const active = sel === sid;
      const dim = sel ? !active : dimIds.includes(sid);
      if (active) {
        // 選中單架次＝SINR 分級 run 分割（狀態色唯一入口）
        runs.push(...sinrRuns(rows).map((r) => ({ ...r, sid })));
      } else {
        // identity（或 dim）＝整條一色一 path
        runs.push({
          sid,
          path: rows.map((r) => [r.lon, r.lat, r.alt] as [number, number, number]),
          color: rgba(dim ? MUTED : colorOf(sid)),
          width: dim ? 1.6 : 3,
        });
      }
      for (const r of rows) { allLats.push(r.lat); allLons.push(r.lon); }
    }
    overlayRef.current?.setProps({ layers: [...pathsLayer("cmp-runs", runs, true)] });

    // 取景 bounds：樣本按 P1–P99 分位數裁掉 GPS 漂移離群點（長航次會有），
    // 計畫航點不裁（權威資料）。絲帶照常全量渲染，只有取景被裁
    const bounds = new maplibregl.LngLatBounds();
    for (const w of wps) bounds.extend([w.lon, w.lat]);
    if (allLats.length) {
      allLats.sort((a, b) => a - b);
      allLons.sort((a, b) => a - b);
      const q = (arr: number[], f: number) => arr[Math.round(f * (arr.length - 1))];
      bounds.extend([q(allLons, 0.01), q(allLats, 0.01)]);
      bounds.extend([q(allLons, 0.99), q(allLats, 0.99)]);
    }
    if (!bounds.isEmpty()) {
      pendingBoundsRef.current = bounds;
      tryFit();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, loaded, tracks, dimIds, sel, wps, colorOf]);

  return (
    <div className="cmp3d">
      <div ref={containerRef} className="cmp3d-map" />
      <div className="legend legend-right">
        {sel ? (
          <>
            <h4>SINR 分級 · {labelOf(sel)}</h4>
            {LINK_CLASSES.map((c) => (
              <div className="row" key={c.key}>
                <span className="dot" style={{ background: c.color }} />
                {c.label}
              </div>
            ))}
            <button className="btn-plain btn-sm" onClick={() => setSel(null)}>
              返回航線色
            </button>
          </>
        ) : (
          <>
            <h4>航線</h4>
            {loaded.map((sid) => (
              <button className="legend-row" key={sid} onClick={() => setSel(sid)}>
                <span className="dot" style={{
                  background: dimIds.includes(sid) ? MUTED : colorOf(sid) }} />
                {labelOf(sid)}
              </button>
            ))}
            <div className="hint-line">點航線看 SINR 分級</div>
          </>
        )}
      </div>
    </div>
  );
}
