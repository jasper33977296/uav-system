"use client";
/** 場域訊號頁（ui-spec §6 v4「開頁即答案」）：/compare 重定義。
 *
 * 不再是比較工具，是場域訊號地圖——開頁零步驟直接回答「哪裡訊號弱」：
 *   A 軌跡累積：預設最近 30 天全部架次，deck.gl 絲帶分級色、單趟 ~25%
 *     透明——多趟一致的弱區越疊越濃，濃淡天然表達「每次都爛 vs 偶爾爛」。
 *   B 弱區輪廓（預設開）：10m 格 P10 聚合、入劣化以下且涵蓋 ≥2 趟的
 *     聚簇 → 平滑輪廓＋最差值標籤。只圈飛過的區域（不插值不腦補）。
 * 視覺/操作語言＝即時頁同款（暖畫布、網格、起飛點、浮動面板、Ctrl 縮放）。
 * 繪製抽稀（隔點取樣）不影響聚合口徑（聚合吃全樣本）。
 */
import { TextLayer } from "@deck.gl/layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { pathsLayer, sinrRuns, rgba, type RouteRun } from "@/lib/deckRoute";
import { CANVAS, groundGrid } from "@/lib/geo";
import { API, CLIENT_HEADERS, LINK_CLASSES } from "@/lib/signal";
import { aggregateCells, weakZones, type TrackRow, type WeakZone } from "@/lib/signalMap";

interface SessRow {
  id: string; started_at: string; ended_at: string | null;
  mission_id: string | null; mission_name: string | null;
  drone_id: string; drone_name: string;
  note?: string | null;
  summary?: { samples_total?: number } | string | null;
}

// 「有資料的飛行」門檻：環境事件後 30 天窗內 ~90% 是零/微樣本測試殘留，
// 抓 track 前先以 sessions 既有的 summary.samples_total 過濾（後端
// min_samples 參數上線後改伺服器端篩，此為同義客端實作）
const MIN_SAMPLES = 10;
const LIST_LIMIT = 5000;
const samplesOf = (s: SessRow): number => {
  const sm = typeof s.summary === "string"
    ? (JSON.parse(s.summary) as { samples_total?: number })
    : s.summary;
  return sm?.samples_total ?? 0;
};

const RANGES = [
  { key: 7, label: "7天" },
  { key: 30, label: "30天" },
  { key: 0, label: "全部" },
] as const;

export default function FieldMap() {
  const router = useRouter();
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const [ready, setReady] = useState(false);
  const fittedRef = useRef(false);
  const anchoredRef = useRef(false);

  // 篩選（全部可選、無必選）
  const [range, setRange] = useState<7 | 30 | 0>(30);
  const [missionF, setMissionF] = useState("all");
  const [droneF, setDroneF] = useState("all");
  const [zonesOn, setZonesOn] = useState(true);
  const [panelOpen, setPanelOpen] = useState(true);

  const [sessions, setSessions] = useState<SessRow[]>([]);
  // 誠實截斷資訊（no silent caps）：範圍內總數／略過的無樣本數／清單是否截斷
  const [listInfo, setListInfo] = useState({ total: 0, skipped: 0, truncated: false });
  const [tracks, setTracks] = useState<Record<string, TrackRow[]>>({});
  const tracksRef = useRef(tracks);
  useEffect(() => { tracksRef.current = tracks; }, [tracks]);
  const [loadedN, setLoadedN] = useState(0);

  // 選中卡：點軌跡＝該趟卡；點輪廓＝弱區卡；點空白關
  const [sel, setSel] = useState<
    { type: "run"; sid: string } | { type: "zone"; zone: WeakZone } | null>(null);
  const [noteEdit, setNoteEdit] = useState<string | null>(null);

  // 零步驟載入：sessions（依時間範圍）→ 各 track（4 併發池）
  useEffect(() => {
    let stop = false;
    (async () => {
      const since = range
        ? `&since=${new Date(Date.now() - range * 864e5).toISOString()}` : "";
      // 伺服器端 min_samples 篩選（backend 578294d）：無樣本測試殘留
      // 不進傳輸（4790→95）。客端過濾保留作退場保護——舊後端忽略此參數
      // 時會回全量，過濾後行為與換源前相同（skipped 行此時才會出現）。
      const all: SessRow[] = await fetch(
        `${API}/api/sessions?limit=${LIST_LIMIT}&min_samples=${MIN_SAMPLES}${since}`)
        .then((r) => r.json()).catch(() => []);
      if (stop) return;
      const rows = all.filter((s) => samplesOf(s) >= MIN_SAMPLES);
      setListInfo({
        total: all.length,
        skipped: all.length - rows.length,
        truncated: all.length >= LIST_LIMIT,
      });
      setSessions(rows);
      setLoadedN(Object.keys(tracksRef.current)
        .filter((id) => rows.some((s) => s.id === id)).length);
      const todo = rows.filter((s) => !tracksRef.current[s.id]);
      let i = 0;
      await Promise.all(Array.from({ length: 4 }, async () => {
        while (!stop && i < todo.length) {
          const s = todo[i++];
          const d = await fetch(`${API}/api/sessions/${s.id}/track`)
            .then((r) => r.json()).catch(() => null);
          if (stop) return;
          setTracks((t) => ({ ...t, [s.id]: d?.link ?? [] }));
          setLoadedN((n) => n + 1);
        }
      }));
    })();
    return () => { stop = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range]);

  const visible = useMemo(() => sessions.filter((s) =>
    (missionF === "all" || s.mission_id === missionF)
    && (droneF === "all" || s.drone_id === droneF)), [sessions, missionF, droneF]);
  const visLoaded = visible.filter((s) => tracks[s.id]);

  // 場域原點：第一筆有效樣本（網格/聚合錨定）
  const origin = useMemo(() => {
    for (const s of visLoaded) {
      const r = (tracks[s.id] ?? []).find((x) => x.lat != null && x.lon != null);
      if (r) return { lat: r.lat as number, lon: r.lon as number };
    }
    return null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visLoaded.length, tracks]);

  // 弱區（聚合吃全樣本；query_signal_map 契約同形，後端就緒換源）
  const zones = useMemo(() => {
    if (!origin || !zonesOn) return [];
    const cells = aggregateCells(tracks, visLoaded.map((s) => s.id), origin, 10);
    return weakZones(cells, origin, 10);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [origin, zonesOn, visLoaded.length, tracks]);

  // 地圖（即時頁同款；Ctrl 縮放） ---------------------------------
  useEffect(() => {
    const map = new maplibregl.Map({
      container: containerRef.current!,
      center: [8.5456, 47.3977],
      zoom: 15,
      pitch: 55,
      maxPitch: 75,
      cooperativeGestures: true,
      locale: {
        "CooperativeGesturesHandler.WindowsHelpText": "按住 Ctrl 並滾動以縮放地圖",
        "CooperativeGesturesHandler.MacHelpText": "按住 ⌘ 並滾動以縮放地圖",
      },
      style: { version: 8, sources: {}, layers: [
        { id: "canvas", type: "background", paint: { "background-color": CANVAS } },
      ] },
      attributionControl: false,
    });
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;
    map.on("load", () => {
      const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
      map.addControl(overlay as unknown as maplibregl.IControl);
      overlayRef.current = overlay;
      map.on("click", (e) => {
        const info = overlay.pickObject({ x: e.point.x, y: e.point.y });
        const o = info?.object as (RouteRun & { zone?: WeakZone }) | undefined;
        if (o?.zone) setSel({ type: "zone", zone: o.zone });
        else if (o?.sid) setSel({ type: "run", sid: o.sid });
        else setSel(null);
        setNoteEdit(null);
      });
      map.on("mousemove", (e) => {
        map.getCanvas().style.cursor =
          overlay.pickObject({ x: e.point.x, y: e.point.y }) ? "pointer" : "";
      });
      setReady(true);
    });
    return () => map.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 網格/起飛點錨定＋首次取景（有原點後一次）
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !origin || anchoredRef.current) return;
    anchoredRef.current = true;
    map.addSource("grid", { type: "geojson", data: groundGrid(origin.lat, origin.lon) });
    map.addLayer({ id: "grid", type: "line", source: "grid",
      paint: { "line-color": "#262624", "line-width": 1 } }, undefined);
    map.addSource("home", { type: "geojson", data: {
      type: "Feature", properties: {},
      geometry: { type: "Point", coordinates: [origin.lon, origin.lat] } } });
    map.addLayer({ id: "home-ring", type: "circle", source: "home",
      paint: { "circle-radius": 10, "circle-color": "transparent",
               "circle-stroke-width": 2, "circle-stroke-color": "#8f8b80" } });
    if (!fittedRef.current) {
      fittedRef.current = true;
      map.jumpTo({ center: [origin.lon, origin.lat], zoom: 15.5, pitch: 55 });
    }
  }, [ready, origin]);

  // deck 層：A 累積軌跡（25% 透明、選中趟提亮）＋B 輪廓＋標籤
  useEffect(() => {
    if (!ready || !overlayRef.current) return;
    const runs: RouteRun[] = [];
    for (const s of visLoaded) {
      const pts = (tracks[s.id] ?? [])
        .filter((r) => r.lat != null && r.lon != null)
        .filter((_, i) => i % 2 === 0)   // 繪製抽稀（聚合不受影響）
        .map((r) => ({ lat: r.lat as number, lon: r.lon as number,
                       sinr: (r.sinr as number | null) ?? null,
                       alt: (r.alt_rel as number | null) ?? 0 }));
      const hot = sel?.type === "run" && sel.sid === s.id;
      runs.push(...sinrRuns(pts).map((r) => ({
        ...r, sid: s.id,
        color: [r.color[0], r.color[1], r.color[2],
                hot ? 235 : 64] as RouteRun["color"],
        width: hot ? 3.5 : 2.5,
      })));
    }
    const zoneRuns: RouteRun[] = zones.flatMap((z) => z.outline.map((loop) => ({
      path: loop.map(([lon, lat]) => [lon, lat, 1.5] as [number, number, number]),
      color: rgba("#f0eee6"),
      width: sel?.type === "zone" && sel.zone === z ? 2.4 : 1.2,
      zone: z,
    } as RouteRun & { zone: WeakZone })));
    overlayRef.current.setProps({ layers: [
      pathsLayer("field-runs", runs, true),
      pathsLayer("field-zones", zoneRuns, true),
      new TextLayer({
        id: "field-zone-labels",
        data: zones,
        getPosition: (z: WeakZone) => [z.labelLon, z.labelLat, 3],
        getText: (z: WeakZone) => `▼${z.minVal.toFixed(0)}`,
        getColor: [224, 94, 94, 255],
        getSize: 15,
        outlineWidth: 2,
        outlineColor: [27, 26, 23, 255],
        fontSettings: { sdf: true },
        pickable: true,
        onClick: (info) => {
          if (info.object) setSel({ type: "zone", zone: info.object as WeakZone });
        },
      }),
    ] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, visLoaded.length, tracks, zones, sel]);

  // 篩選選項（由資料導出，零配置）
  const missionOpts = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of sessions) if (s.mission_id) m.set(s.mission_id, s.mission_name ?? s.mission_id);
    return [...m.entries()];
  }, [sessions]);
  const droneOpts = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of sessions) m.set(s.drone_id, s.drone_name);
    return [...m.entries()];
  }, [sessions]);

  const selSess = sel?.type === "run"
    ? sessions.find((s) => s.id === sel.sid) ?? null : null;
  const fmtT = (t: string) => new Date(t).toLocaleString("zh-TW",
    { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

  async function saveNote(id: string, note: string) {
    setNoteEdit(null);
    try {
      const res = await fetch(`${API}/api/sessions/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
        body: JSON.stringify({ note }),
      });
      if (res.ok) setSessions((cur) =>
        cur.map((s) => (s.id === id ? { ...s, note } : s)));
    } catch { /* 失敗維持原值 */ }
  }

  const loading = loadedN < sessions.length;

  return (
    <div className="map-wrap">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />

      {/* 浮動面板（任務控制同款語言：可收合） */}
      <div className="field-col">
        <div className="cmd-panel field-panel">
          <div className="cmd-head" onClick={() => setPanelOpen((o) => !o)}>
            <span className="name">場域訊號</span>
            <span className="meta"
              title={`樣本數 ≥${MIN_SAMPLES} 的架次（門檻與後端 min_samples 對齊）`}>
              {visLoaded.length} 趟</span>
            {loading && <span className="meta">載入 {loadedN}/{sessions.length}…</span>}
            <span className="spacer" />
            <span className="meta">{panelOpen ? "▾" : "▸"}</span>
          </div>
          {panelOpen && (
            <div className="cmd-body">
              <div className="cmd-row">
                <span className="hint-line">時間</span>
                <div className="seg">
                  {RANGES.map((r) => (
                    <button key={r.key} className={range === r.key ? "on" : ""}
                      onClick={() => setRange(r.key)}>{r.label}</button>
                  ))}
                </div>
              </div>
              <div className="cmd-row">
                <select value={missionF} onChange={(e) => setMissionF(e.target.value)}>
                  <option value="all">航線：全部</option>
                  {missionOpts.map(([id, name]) => (
                    <option key={id} value={id}>{name}</option>
                  ))}
                </select>
              </div>
              <div className="cmd-row">
                <select value={droneF} onChange={(e) => setDroneF(e.target.value)}>
                  <option value="all">機：全部</option>
                  {droneOpts.map(([id, name]) => (
                    <option key={id} value={id}>{name}</option>
                  ))}
                </select>
              </div>
              <label className="cmd-row" style={{ cursor: "pointer" }}>
                <input type="checkbox" checked={zonesOn}
                  onChange={(e) => setZonesOn(e.target.checked)} />
                弱區輪廓
              </label>
              {/* 誠實截斷（no silent caps）：總量與略過數如實揭露 */}
              {(listInfo.skipped > 0 || listInfo.truncated) && (
                <div className="hint-line">
                  範圍內 {listInfo.total} 筆架次
                  {listInfo.skipped > 0 && `．已略過 ${listInfo.skipped} 個無樣本測試架次`}
                  {listInfo.truncated && `．清單達 ${LIST_LIMIT} 筆上限（可能未涵蓋全部）`}
                </div>
              )}
            </div>
          )}
        </div>

        {/* 資訊卡：點軌跡＝該趟；點輪廓＝弱區；點空白關 */}
        {selSess && (
          <div className="card field-card">
            <div className="badges">
              <span className="name-lg">{fmtT(selSess.started_at)}</span>
              <span className="chip">{selSess.drone_name}</span>
            </div>
            {noteEdit != null ? (
              <input className="note-input" autoFocus value={noteEdit}
                onChange={(e) => setNoteEdit(e.target.value)}
                onBlur={() => saveNote(selSess.id, noteEdit)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") saveNote(selSess.id, noteEdit);
                  if (e.key === "Escape") setNoteEdit(null);
                }} />
            ) : (
              <button className="note-btn"
                onClick={() => setNoteEdit(selSess.note ?? "")}>
                {selSess.note || "—"} ✎
              </button>
            )}
            <div className="cmd-row" style={{ marginTop: 8 }}>
              <button className="btn-plain btn-sm"
                onClick={() => router.push(`/replay/${selSess.id}`)}>開回放</button>
              <button className="btn-plain btn-sm" onClick={() => setSel(null)}>關閉</button>
            </div>
          </div>
        )}
        {sel?.type === "zone" && (
          <div className="card field-card">
            <div className="badges">
              <span className="name-lg">弱區</span>
              <span className="chip" style={{ color: "var(--status-danger)" }}>
                最差 {sel.zone.minVal.toFixed(1)} dB
              </span>
            </div>
            <div className="hint-line">
              涵蓋 {sel.zone.sessionIds.length} 趟．樣本 {sel.zone.n}
              {sel.zone.lastBad && <>．最近劣化 {fmtT(sel.zone.lastBad)}</>}
            </div>
            <div className="cmd-row" style={{ marginTop: 8 }}>
              <button className="btn-plain btn-sm" onClick={() => setSel(null)}>關閉</button>
            </div>
          </div>
        )}
      </div>

      {/* 圖例：四分級＋濃淡語意＋輪廓說明 */}
      <div className="legend">
        <h4>訊號品質</h4>
        {LINK_CLASSES.map((c) => (
          <div className="row" key={c.key}>
            <span className="dot" style={{ background: c.color }} />
            {c.label}
          </div>
        ))}
        <div className="row"><span className="dot" style={{
          background: "rgba(160,24,24,0.35)" }} />淡＝單趟．濃＝多趟一致</div>
        <div className="row"><span className="dot" style={{
          background: "transparent", border: "1.5px solid #f0eee6" }} />
          弱區輪廓（▼最差值）</div>
      </div>

      {!origin && !loading && (
        <div className="field-empty empty">此範圍沒有飛行資料</div>
      )}
    </div>
  );
}
