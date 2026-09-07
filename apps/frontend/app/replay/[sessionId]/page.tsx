"use client";
import { IconLayer } from "@deck.gl/layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useParams, useRouter } from "next/navigation";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import BasemapToggle from "@/components/BasemapToggle";
import { colorFor, createDroneLayer } from "@/components/droneLayer";
import EventModal from "@/components/EventModal";
import InfoTip from "@/components/InfoTip";
import LogIndexSheet from "@/components/LogIndexSheet";
import ReplayVideo, { type SessionVideo } from "@/components/ReplayVideo";
import { SignalBars } from "@/components/SimpleHud";
import { ARROW_ICON_SIZE, arrowIconUrl } from "@/lib/arrowIcon";
import { routeLayer } from "@/lib/deckRoute";
import { useBasemap } from "@/lib/basemap";
import { emph, unemph } from "@/lib/emph";
import { evText } from "@/lib/evtext";
import { eventDetail } from "@/lib/jsonb";
import { CANVAS, groundGrid, pathArrows, planPath, ribbon, trailLineString } from "@/lib/geo";
import { asGroups, EvDensity, foldEvents, foldTitle } from "@/lib/foldEvents";
import { normSev, SEV_DOT } from "@/lib/severity";
import { API, classifySinr, LINK_CLASSES } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

interface LinkRow {
  time: string; lat: number | null; lon: number | null; alt_rel: number | null;
  sinr: number | null; rtt_ms: number | null;
}
interface Ev {
  id: number; time: string; severity: string; type: string;
  detail: Record<string, unknown>;
  source?: string | null;
  drone_id?: string | null;
}
/** 模式帶要用的遙測（track 回應本來就有，之前整批丟掉）。 */
interface TeleRow { time: string; flight_mode: string | null; alt_rel: number | null }
interface CmdRow {
  time: string; action: string; result: string;
  detail: string | null; client: string | null;
}
/** 這一趟的摘要（`/api/sessions/{id}`）。**「上鎖」與「我們看不到它了」
 * 是兩件事**，所以 end_reason 要照枚舉講人話，認不得就顯示原代號。 */
interface SessionMeta {
  drone_name?: string | null; mission_name: string | null;
  started_at: string; ended_at: string | null; end_reason: string | null;
  summary: unknown;
}
const END_LABELS: Record<string, string> = {
  disarmed: "上鎖（正常結束）",
  telemetry_lost: "遙測中斷（不代表飛行結束）",
  telemetry_lost_backfilled: "遙測中斷，事後由機上補回",
};
interface Quality {
  live: number; backfilled: number; conflicts: number;
  max_gap_m: number | null; mode_mismatch: number; rule: string;
}

const fmt = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));
const hm = (iso: string) =>
  new Date(iso).toLocaleTimeString("zh-TW", { hour12: false });
const secs = (s: number) =>
  s >= 60 ? `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒` : `${Math.round(s)} 秒`;

/* ── 時序圖：SVG viewBox 1000 寬，preserveAspectRatio none 拉滿容器 ── */
const W = 1000;

function Chart({
  rows, field, height, yLabel, thresholds, events, t0, t1, idx, onSeek, onEvent,
}: {
  rows: LinkRow[]; field: "sinr" | "rtt_ms"; height: number; yLabel: string;
  thresholds?: number[]; events?: Ev[]; t0: number; t1: number; idx: number;
  onSeek?: (idx: number) => void;
  onEvent?: (e: Ev) => void;
}) {
  const vals = rows.map((r) => r[field]).filter((v): v is number => v != null);
  if (!vals.length) return null;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  for (const th of thresholds ?? []) { lo = Math.min(lo, th); hi = Math.max(hi, th); }
  const pad = (hi - lo || 1) * 0.12;
  lo -= pad; hi += pad;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * W;
  const y = (v: number) => height - ((v - lo) / (hi - lo)) * height;
  const pts = rows
    .filter((r) => r[field] != null)
    .map((r) => `${x(new Date(r.time).getTime())},${y(r[field] as number)}`)
    .join(" ");
  const cx = rows[idx] ? x(new Date(rows[idx].time).getTime()) : 0;

  // 點圖表跳時刻（ui-spec §5.3）：點擊位置 → 時間 → 最近樣本；
  // 事件三角同一路徑（三角座標＝事件時刻，點它即跳到事件）
  const seek = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!onSeek || !rows.length) return;
    const r = e.currentTarget.getBoundingClientRect();
    const t = t0 + ((e.clientX - r.left) / r.width) * (t1 - t0);
    let best = 0, bd = Infinity;
    rows.forEach((row, i) => {
      const d = Math.abs(new Date(row.time).getTime() - t);
      if (d < bd) { bd = d; best = i; }
    });
    onSeek(best);
  };

  return (
    <div className="chart">
      <span className="chart-label">{yLabel}</span>
      <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none"
           onPointerDown={seek}
           style={onSeek ? { cursor: "crosshair" } : undefined}>
        {(thresholds ?? []).map((th) => (
          <line key={th} x1={0} x2={W} y1={y(th)} y2={y(th)}
                stroke="var(--muted)" strokeWidth="1" strokeDasharray="4 4"
                vectorEffect="non-scaling-stroke" opacity={0.5} />
        ))}
        <polyline points={pts} fill="none" stroke="var(--series-1)" strokeWidth="1.6"
                  vectorEffect="non-scaling-stroke" />
        {(events ?? []).map((e) => (
          <polygon
            key={e.id}
            points={`${x(new Date(e.time).getTime()) - 5},2 ${x(new Date(e.time).getTime()) + 5},2 ${x(new Date(e.time).getTime())},11`}
            fill={e.severity === "critical" ? "#a01818" : e.severity === "warning" ? "#fab219" : "#8f8b80"}
            style={onEvent ? { cursor: "pointer" } : undefined}
            // 點三角＝跳到事件時刻（pointerdown 冒泡到 svg 的 seek）＋開詳情
            // modal（§2.7：modal 接手細節職責；title 保留 hover 摘要）
            onClick={() => onEvent?.(e)}
          >
            {/* SVG <title> 只吃字串，放不了 <b>——記號拿掉而不是印出來 */}
            <title>{unemph(evText({ type: e.type, detail: e.detail,
              severity: e.severity as "info" | "warning" | "critical" }))}</title>
          </polygon>
        ))}
        <line x1={cx} x2={cx} y1={0} y2={height} stroke="var(--ink)" strokeWidth="1"
              vectorEffect="non-scaling-stroke" opacity={0.55} />
      </svg>
    </div>
  );
}

export default function Replay() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const router = useRouter();
  const [rows, setRows] = useState<LinkRow[]>([]);
  const [meta, setMeta] = useState<{ mission_id: string | null;
    mission_name: string | null; drone_name?: string | null;
    drone_id?: string | null } | null>(null);
  const [video, setVideo] = useState<SessionVideo | null>(null);   // §5.4
  // 右側面板要的三份（都是既有端點，這次後端不用動）
  const [tele, setTele] = useState<TeleRow[]>([]);        // 模式帶
  const [cmds, setCmds] = useState<CmdRow[] | null>(null);
  const [sess, setSess] = useState<SessionMeta | null>(null);
  const [quality, setQuality] = useState<Quality | null>(null);
  const [sheet, setSheet] = useState<{ url: string; title: string } | null>(null);
  const [evFilter, setEvFilter] = useState<"all" | "warn" | "vehicle" | "system">("all");
  const [plan, setPlan] = useState<{ lat: number; lon: number; alt: number | null }[]>([]);
  const [events, setEvents] = useState<Ev[]>([]);
  const [openEv, setOpenEv] = useState<Ev | null>(null);   // 事件詳情 modal（§2.7）
  const [idx, setIdx] = useState(0);
  // 「還在載入」與「載入完成但沒有樣本」是兩件事——原本共用一句「載入軌跡中…
  // （若架次無樣本則無可回放）」，把兩種狀態塞進同一句括號裡，等於**兩個都
  // 沒真的宣告**：載入卡住時看起來像沒樣本、沒樣本時看起來像還在載（§0.2e）
  const [loaded, setLoaded] = useState(false);
  const [loadErr, setLoadErr] = useState(false);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const ovRef = useRef<MapboxOverlay | null>(null);
  // §2.4b：回放是研究判讀主場景，空間定位需求不低於即時頁——同款底圖切換
  const base = useBasemap();

  useEffect(() => {
    fetch(`${API}/api/sessions/${sessionId}/track`)
      // **必須看 r.ok**：fetch 不會因 HTTP 4xx/5xx 而 reject，而錯誤回應的
      // body（`{"detail": ...}`）是合法 JSON——`d.link ?? []` 於是得到空陣列，
      // 畫面把**我方的取得失敗說成「這趟沒有訊號量測」**。測試抓到的正是這個
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((d) => {
        // 註：track 回應另有 telemetry 陣列（含 heading 等姿態欄）。
        // §2.4c 移除圖示旋轉後前端沒有 heading 的消費者，故不再併入——
        // 留著會是每列一次二分搜尋的白工，也會讓人以為朝向功能還在
        const link = (d.link ?? [])
          .filter((r: LinkRow) => r.lat != null && r.lon != null);
        setRows(link);
        // 模式帶：track 回應本來就帶 telemetry，之前整批丟掉（那時沒有消費者）
        setTele((d.telemetry ?? []).filter((r: TeleRow) => r.flight_mode));
        setLoaded(true);
        // **預設停在起點**（使用者定案 2026-09-08）：回放就從這一趟的開頭開始
        setIdx(0);
        setMeta(d.session ?? null);
        // 航線關聯了任務 → 抓航點疊預計路徑（預計 vs 實際比對）
        if (d.session?.mission_id)
          fetch(`${API}/api/missions/${d.session.mission_id}/waypoints`)
            .then((r) => (r.ok ? r.json() : null))
            // planPath 補起飛爬升段與返航降落段：起飛項的高度是「爬到哪」，
            // 照 lat/lon 過濾直接畫會讓預計路徑從空中出發、與即時頁不同形狀
            .then((m) => m && setPlan(planPath(m.waypoints, m.home)))
            .catch(() => {});
      })
      // 取得失敗**不得**顯示「這趟沒有訊號量測」——那是把我方的失敗說成
      // 對方沒資料，正是 §0.2e 要防的冒充。三態各自有話：載入中／載入失敗／
      // 真的沒有樣本
      .catch(() => setLoadErr(true));
    // §5.4 影片中繼資料：video_status 五態全後端算（UI 不做日期運算）
    fetch(`${API}/api/sessions/${sessionId}/video`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setVideo).catch(() => {});
    // 段長未定案（final=false）→ 定期回抓，定案後涵蓋帶依實際長度重畫
    const poll = setInterval(async () => {
      const v: SessionVideo | null = await fetch(`${API}/api/sessions/${sessionId}/video`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      if (!v) return;
      setVideo(v);
      if (v.segments.every((g) => g.final !== false)) clearInterval(poll);
    }, 5000);
    fetch(`${API}/api/sessions/${sessionId}`)
      .then((r) => (r.ok ? r.json() : null)).then(setSess).catch(() => {});
    fetch(`${API}/api/sessions/${sessionId}/telemetry-quality`)
      .then((r) => (r.ok ? r.json() : null)).then(setQuality).catch(() => {});
    fetch(`${API}/api/sessions/${sessionId}/commands`)
      // 取不到就 null（畫面說「取不到」），不是空陣列（那是「沒有下過指令」）
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then(setCmds).catch(() => setCmds(null));
    fetch(`${API}/api/events?session_id=${sessionId}`)
      .then((r) => r.json())
      // REST 的 detail 是 JSONB 字串——解析成物件（modal 細節層要用）。
      // 逐列解析：一列壞掉不得吃掉整批（見 lib/jsonb.ts）。這裡是輪詢，
      // 整批被吞掉的話會**永遠**顯示「尚無事件」，比即時頁更難察覺
      .then((rows) => setEvents(rows.map((e: any) => ({
        ...e, detail: eventDetail(e.detail),
      }))))
      .catch(() => {});
    return () => clearInterval(poll);
  }, [sessionId]);

  // 影片窗識別徽章（§2.9/§5.4：唯一辨識依據，不得缺）——track 的 session
  // 物件沒帶機身欄位，用事件的 drone_id 補（同架次事件已在手，零額外請求），
  // 機名再查機隊 store；都查不到就顯 id 前綴，不編造
  const fleet = useUavStore((s) => s.fleet);
  const vidDroneId = meta?.drone_id ?? events.find((e) => e.drone_id)?.drone_id ?? null;
  const vidDroneName = meta?.drone_name
    ?? (vidDroneId ? fleet[vidDroneId]?.drone_name ?? `#${vidDroneId.slice(0, 8)}` : null);

  const [t0, t1] = useMemo(() => {
    if (!rows.length) return [0, 1];
    return [new Date(rows[0].time).getTime(), new Date(rows[rows.length - 1].time).getTime()];
  }, [rows]);

  // 地圖：等資料到才建（要用軌跡範圍 fitBounds、第一點當地面網格中心）
  useEffect(() => {
    if (!rows.length || !containerRef.current || mapRef.current) return;
    const lats = rows.map((r) => r.lat!) , lons = rows.map((r) => r.lon!);
    const first = rows[0];
    const map = new maplibregl.Map({
      container: containerRef.current,
      bounds: [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      fitBoundsOptions: { padding: 90 },
      pitch: 55, maxPitch: 75,
      style: {
        version: 8, sources: {},
        layers: [{ id: "canvas", type: "background", paint: { "background-color": CANVAS } }],
      },
    });
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;

    map.on("load", () => {
      base.install(map);
      map.addSource("grid", { type: "geojson", data: groundGrid(first.lat!, first.lon!) });
      map.addLayer({ id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#262624", "line-width": 1 } });
      map.addSource("home", { type: "geojson", data: {
        type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [first.lon!, first.lat!] } } });
      map.addLayer({ id: "home-ring", type: "circle", source: "home",
        paint: { "circle-radius": 10, "circle-color": "transparent",
                 "circle-stroke-width": 2, "circle-stroke-color": "#8f8b80" } });

      // 地面投影：中性細線（單機頁identity無歧義，投影只是把 3D 路徑釘回地面）
      map.addSource("track", { type: "geojson", data: {
        type: "FeatureCollection",
        features: [trailLineString(rows.map((r) => ({ lat: r.lat, lon: r.lon })))]
          .filter((f): f is GeoJSON.Feature => f !== null) } });
      map.addLayer({ id: "track", type: "line", source: "track",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#6b7684",
          "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1, 16, 2, 20, 3.5],
          "line-opacity": 0.55 } });

      // 預計任務路徑（航線開的當下所關聯的任務）：灰絲帶＋地面虛線
      if (plan.length >= 2) {
        map.addSource("plan3d", { type: "geojson",
          data: ribbon(plan.map((w) => ({ lat: w.lat, lon: w.lon, alt: w.alt })), () => ({}), 0.45) });
        map.addLayer({ id: "plan3d", type: "fill-extrusion", source: "plan3d",
          paint: { "fill-extrusion-color": "#8f8b80",
            "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
            "fill-extrusion-opacity": 0.35 } });
        map.addSource("plan-ground", { type: "geojson",
          data: { type: "Feature", properties: {},
            geometry: { type: "LineString", coordinates: plan.map((w) => [w.lon, w.lat]) } } });
        map.addLayer({ id: "plan-ground", type: "line", source: "plan-ground",
          paint: { "line-color": "#8f8b80", "line-width": 1.5,
                   "line-dasharray": [3, 3], "line-opacity": 0.6 } }, "track");
      }

      // ⚠ 順序同即時頁（§2.4c）：計畫路徑必須先建，deck overlay 後掛——
      // 否則灰色計畫路徑會蓋住實測軌跡與游標圖示（產出不得被輸入遮蔽）
      // 懸浮航跡：deck.gl PathLayer（route-render-tool-eval，取代 fill-extrusion）
      // 游標圖示層同掛此 overlay，scrub 時只換該層（軌跡層資料同參考、不重建）
      const ov = new MapboxOverlay({ interleaved: true, layers: [] });
      ovRef.current = ov;
      map.addControl(ov as unknown as maplibregl.IControl);
      pushLayersRef.current();
      // 貼地圖示（箭頭、游標）的俯角補償係數是推送當下算的——轉動視角
      // 後不重推就會停在舊係數，傾斜看時箭頭被壓扁
      map.on("pitchend", () => pushLayersRef.current());

      // **回放游標＝即時頁那顆球體**（使用者定案 2026-09-08）：原本是藍色
      // 2D 四旋翼圖示，於是同一台機在兩頁長得不一樣。沿用同一個
      // `createDroneLayer`（three.js 自訂層），連光照與半徑規則都同一份。
      // 顏色用**識別色**不是 SINR 分級色——identity 與 status 不互相冒充
      // （ui-spec §0.1）；當下的 SINR 由地圖左上的 chip 與下方圖表承載。
      map.addLayer(createDroneLayer("replay-cursor", () => {
        const c = cursorRef.current;
        return c ? [{ id: "cursor", lat: c.lat, lon: c.lon, alt: c.alt,
                      color: c.color, radiusM: 1.1 }] : [];
      }));
    });
    return () => { map.remove(); mapRef.current = null; };
  }, [rows, plan]);

  // 軌跡資料只在 rows 變時重算（scrub 不重算）——但**只記住資料、不記住
  // 圖層實例**。deck 的 `Layer._initialize` 有 `assert(!this.internalState)
  // // finalized layer cannot be reused`：地圖重建時（本頁 map.remove() 依賴
  // [rows, plan]，plan 比 rows 晚到就會重建一次）舊 deck 會 finalize 這些
  // 實例，被 useMemo 記住的同一批實例再推進新 deck 就整層初始化失敗——
  // **軌跡整條不見**。實測 6 次載入 2 次踩到（彩色像素 2469 → 1251），
  // 是抓取順序的競態，不是必現，所以肉眼抽查會漏掉
  const trackPts = useMemo(() => rows
    .filter((r) => r.lat != null && r.lon != null)
    .map((r) => ({ lat: r.lat!, lon: r.lon!,
                   sinr: r.sinr ?? null, alt: r.alt_rel ?? null })), [rows]);

  const arrowPts = useMemo(() => pathArrows(
    rows.map((r) => ({ lat: r.lat, lon: r.lon, alt: r.alt_rel }))), [rows]);

  // scrub → 游標圖示（§2.4b：與即時頁同一套機體圖示、隨當時 heading 旋轉）
  //: 球體游標的當下位置。**放 ref 不放 state**：自訂層在 render 迴圈裡讀，
  //: 每次 scrub 都重建 React 樹是白工
  const cursorRef = useRef<{ lat: number; lon: number; alt: number; color: string } | null>(null);
  const pushLayersRef = useRef<() => void>(() => {});
  useEffect(() => {
    pushLayersRef.current = () => {
      const r = rows[idx];
      ovRef.current?.setProps({ layers: [
        // 每次推送都建新實例（deck 靠 id 比對做差異更新；data 參考不變時
        // 不會重算屬性，所以成本只有物件配置）
        ...routeLayer("track3d", { track: trackPts }),
        // 方向箭頭：**貼在航跡線正上方**（同一個 3D 座標、排在航跡之後
        // 畫），不再是浮在絲帶上方一公尺的獨立三角板。
        //   - 尺寸：公尺錨定＋像素夾限（2–14px）——隨縮放連續變化，
        //     縮到近處不再是一片大白板（使用者反饋「箭頭太大」），
        //     縮遠也不會消失。線寬是 4–5px，箭頭上限取其約 3 倍
        //   - `billboard:false`：箭頭有方向語意，帶面必須貼著地面才會
        //     指向真正的地面方位（§2.4c #1 同一條理由）；傾斜視角的
        //     透視壓縮用與機體圖示同一套 cos^-0.75 補償
        //   - 深度：不比較也不寫入——箭頭與它所貼的航跡線同座標，
        //     做深度比較就會與線互相穿插閃爍
        ...(arrowPts.length ? [new IconLayer({
          id: "track-arrows",
          data: arrowPts,
          getPosition: (d: { pos: [number, number, number] }) => d.pos,
          getAngle: (d: { deg: number }) => -d.deg,   // 羅盤順時針 → deck 逆時針
          getIcon: () => ({
            url: arrowIconUrl, width: ARROW_ICON_SIZE, height: ARROW_ICON_SIZE,
            anchorX: ARROW_ICON_SIZE / 2, anchorY: ARROW_ICON_SIZE / 2, mask: false,
          }),
          // 1.2m 的物理尺寸讓常用縮放落在夾限**之間**（回放頁預設約
          // 11px/m → 約 18px）：太大的公尺數會整段貼在上限，等於固定
          // 螢幕尺寸、縮放沒有反應
          getSize: 1.2, sizeUnits: "meters",
          sizeMinPixels: 7, sizeMaxPixels: 22,
          billboard: false,
          // 往螢幕上方推 6px：**貼著線的上緣**而不是壓在線上。
          // 壓在線上時箭頭被分級色包住，小尺寸下看不出是箭頭
          getPixelOffset: [0, -6],
          sizeScale: Math.min(2.2, Math.pow(Math.max(0.2,
            Math.cos(((mapRef.current?.getPitch() ?? 55) * Math.PI) / 180)), -0.75)),
          parameters: { depthCompare: "always" as const, depthWriteEnabled: false },
        })] : []),
      ] });
      // 球體游標：更新 ref 後叫地圖重畫（自訂層不吃 deck 的 props 更新）
      cursorRef.current = (r && r.lat != null && r.lon != null)
        ? { lat: r.lat, lon: r.lon, alt: r.alt_rel ?? 0,
            // **id 還沒到就不要去登記顏色**：colorFor 依「首次出現順序」配色，
            // 拿假 id 去問會把第一個色槽用掉，真正的機反而拿到第二色
            color: meta?.drone_id ? colorFor(meta.drone_id) : "#3987e5" }
        : null;
      mapRef.current?.triggerRepaint();
    };
    pushLayersRef.current();
  }, [idx, rows, trackPts, arrowPts, meta?.drone_id]);

  // 播放（ui-spec §5）：1Hz 樣本 → 每 1000/speed ms 前進一格；到底自停
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [speedMenu, setSpeedMenu] = useState(false);
  useEffect(() => {
    if (!playing || rows.length < 2) return;
    const t = setInterval(() => {
      setIdx((i) => {
        if (i >= rows.length - 1) { setPlaying(false); return i; }
        return i + 1;
      });
    }, 1000 / speed);
    return () => clearInterval(t);
  }, [playing, speed, rows.length]);
  // 空白鍵播放/暫停（表單元素聚焦時不攔）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key !== " " || t?.closest("button, input, select, textarea")) return;
      e.preventDefault();
      setPlaying((p) => !p);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // 圖表抽屜（研究工作區 → 展開記憶）
  const [drawerOpen, setDrawerOpen] = useState(true);
  // **預設展開**：回放頁是研究主場景，圖表不是附屬品。仍記住使用者的選擇
  useEffect(() => {
    setDrawerOpen(localStorage.getItem("replay-drawer-open") !== "0");
  }, []);

  const cur = rows[idx];

  /** 開這一趟的機上錄製索引。**挑涵蓋這段時間的那一份**——不是最新那份，
   * 那可能是別趟的。挑不到就說挑不到，不隨便給一份。 */
  const openIndex = async () => {
    try {
      const r = await fetch(`${API}/api/onboard-captures`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const did = meta?.drone_id ?? null;
      const files = (d.files ?? []).filter((f: {
        drone_id: string; status: string;
        covers: { from: number; to: number } | null; url: string; name: string;
      }) => f.status === "complete" && (!did || f.drone_id === did));
      const s0 = t0 / 1000, s1 = t1 / 1000;
      const hit = files.find((f: { covers: { from: number; to: number } | null }) =>
        f.covers && f.covers.from <= s1 && f.covers.to >= s0);
      if (!hit) {
        window.alert("找不到涵蓋這一趟的機上錄製——可能還沒回傳，或那段只有地面站那份。");
        return;
      }
      setSheet({ url: `${API}${hit.url}/index`,
        title: `${hit.name} · ${meta?.drone_name ?? ""}` });
    } catch (e) {
      window.alert(`取不到錄製清單：${(e as Error).message}`);
    }
  };

  // §0.2e：中性呈現必須答得出「是沒有這筆資料，還是這筆資料裡沒有這個欄位？」
  // 灰色軌跡有兩個成因——沒有量測、或有量測但沒有 SINR 欄位——**畫面上完全
  // 同形**（實測：把欄位名 sinr 改成 snr，灰線像素數與真的沒量到一模一樣）。
  // 於是「上游改欄位名」可以無聲活過整個實驗：使用者只會記下「這趟沒訊號
  // 資料」繼續研究，**沒有人會去追問一個合法又常見的狀態**。
  // 一個布林運算就把看不見的失效換成看得見的陳述
  const sinrN = rows.filter((r) => r.sinr != null).length;
  const sinrNote = rows.length === 0 ? null      // 尚未載入/無軌跡另有既有處理
    : sinrN === 0 ? `有 ${rows.length} 筆量測，但沒有 SINR 值` : null;

  return (
    <div className="replay">
      {/* 極簡 header（ui-spec §5）：返回＋日期＋任務名，無樣本數 */}
      <div className="replay-head">
        <button className="btn-plain btn-sm" title="返回"
          onClick={() => router.push("/drones")}>←</button>
        {/* 這一趟是誰、什麼時候、飛哪條——**識別在最前面**（原本只有日期） */}
        <span className="rp-who">{sess?.drone_name ?? meta?.drone_name ?? ""}</span>
        <span className="meta">
          {sess && `${hm(sess.started_at)}–${sess.ended_at ? hm(sess.ended_at) : "進行中"}`}
          {sess?.ended_at && ` · ${secs((new Date(sess.ended_at).getTime()
            - new Date(sess.started_at).getTime()) / 1000)}`}
          {meta?.mission_name && ` · ${meta.mission_name}`}
          {/* opt-out 留痕（§5.4）：未錄影＝正常態，弱字不宣告 */}
          {video?.video_status === "off" && "　本趟未錄影"}
        </span>
        {sess?.end_reason && (
          <span className="chip">{END_LABELS[sess.end_reason] ?? sess.end_reason}</span>
        )}
        {sinrNote && <span className="meta rp-nosinr">{sinrNote}</span>}
        <span className="spacer" />
        <button className="btn-plain btn-sm" onClick={openIndex}
          title="在網頁上打開這一趟的機上錄製索引">紀錄索引</button>{" "}
        <a className="btn-plain btn-sm" download
          href={`${API}/api/sessions/${sessionId}/export`}
          title="下載這一趟的完整原始資料（JSON）">匯出 JSON</a>
        <InfoTip tip="地圖上的彩帶依實測 SINR 上色、灰帶是預計航線、白箭頭是方向。時間軸上的釘點是事件（顏色＝嚴重度）、三角是指令、底色帶是飛行模式。拖時間軸，地圖與右側同步跳到那一刻；點事件也會跳。" />
      </div>

      <div className="replay-body">
      <div className="replay-map" ref={containerRef}>
        {!rows.length && (
          <div className="empty" style={{ padding: 20 }}>
            {loadErr ? "軌跡載入失敗"
              : loaded ? "這趟沒有訊號量測" : "載入軌跡中…"}</div>
        )}
        {/* 這一刻的機況：模式／高度／訊號（原本只在時間軸右邊擠成一行） */}
        {rows.length > 0 && (
          <div className="rp-chips">
            <span className="chip">{teleModeAt(tele, cur?.time) ?? "—"}</span>
            <span className="chip">▲ {fmt(cur?.alt_rel, 1)} m</span>
            <span className="chip">
              {cur?.sinr != null ? <>
                <span className="dot" style={{ background: classifySinr(cur.sinr).color }} />
                {cur.sinr.toFixed(1)} dB · {classifySinr(cur.sinr).label.split(" ")[0]}
              </> : "此刻沒有鏈路量測"}
            </span>
          </div>
        )}
        {/* 起點時畫面上只有預計航線——**那不是壞掉，是還沒飛** */}
        {rows.length > 0 && idx === 0 && (
          <div className="rp-startnote">回放在起點——按 ▶ 開始，或拖時間軸</div>
        )}
        {/* 圖例：**一列色階＋底圖**（原本只有底圖切換，而軌跡是有分級色的） */}
        {rows.length > 0 && (
          <div className="legend replay-legend">
            <div className="legend-row">
              <span className="legend-lab">訊號品質</span>
              {LINK_CLASSES.map((c) => (
                <span className="legend-seg" key={c.key}>
                  <i className="legend-sw" style={{ background: c.color }} />
                  {c.label.split(" ")[0]}
                </span>
              ))}
              <InfoTip tip="彩帶＝實際飛的路徑，依當時 SINR 上色（門檻同 backend 事件門檻）。灰色細帶＝預計航線、空心圈＝起飛點、白色箭頭＝方向。球體是這一刻的機身位置，用識別色不是分級色——identity 與 status 不互相冒充。" />
            </div>
            <BasemapToggle on={base.on} set={base.set}
              offline={base.offline} outside={base.outside} />
          </div>
        )}
      </div>

      <aside className="replay-panel">
        <ReplayPanel sess={sess} quality={quality} rows={rows} events={events}
          cmds={cmds} idx={idx} evFilter={evFilter} setEvFilter={setEvFilter}
          onSeekTime={(ms) => { setIdx(nearestIdx(rows, ms)); setPlaying(false); }}
          onEvent={setOpenEv} />
      </aside>
      </div>

      {/* §5.4 影片同步窗：時鐘源＝回放 transport，影片跟隨 */}
      {video && rows.length > 1 && cur && (
        <ReplayVideo video={video} rows={rows}
          tCurMs={new Date(cur.time).getTime()}
          playing={playing} speed={speed}
          droneName={vidDroneName}
          droneColor={vidDroneId ? colorFor(vidDroneId) : "#8f8b80"} />
      )}

      {rows.length > 1 && (
        <div className="timeline">
          {/* 播放＋時間軸＋游標處 ▲高度/訊號格；速度收 ⋯（§7 預設） */}
          <div className="scrub-row">
            <button className="btn-plain btn-sm" title="播放/暫停（空白鍵）"
              onClick={() => setPlaying((p) => !p)}>{playing ? "⏸" : "▶"}</button>
            <span className="scrub-read">
              {cur && new Date(cur.time).toLocaleTimeString("zh-TW", { hour12: false })}
              <span className="rp-rel">
                {cur && `+${String(Math.floor((new Date(cur.time).getTime() - t0) / 60000))
                  .padStart(2, "0")}:${String(Math.floor(((new Date(cur.time).getTime() - t0) / 1000) % 60))
                  .padStart(2, "0")}`}
              </span>
            </span>
            <span style={{ position: "relative" }}>
              <button className="btn-plain btn-sm" title="播放速度"
                onClick={() => setSpeedMenu(!speedMenu)}>{speed > 1 ? `${speed}×` : "1×"}</button>
              {speedMenu && (
                <div className="mcard-menu" style={{ bottom: 34, right: 0 }}>
                  {[1, 4, 8].map((sp) => (
                    <button key={sp} className="btn-plain btn-sm"
                      onClick={() => { setSpeed(sp); setSpeedMenu(false); }}>
                      {sp}×{speed === sp ? " ✓" : ""}
                    </button>
                  ))}
                </div>
              )}
            </span>
            <span className="spacer" />
          </div>

          {/* **軌道整寬，與下面兩張圖共用 X 軸**（原本被左右按鈕擠在中間，
              與圖表對不起來）。上面畫模式帶、事件釘點與指令三角 */}
          <div className="rp-track">
            <TrackBand tele={tele} events={events} cmds={cmds ?? []}
              t0={t0} t1={t1} cur={cur ? new Date(cur.time).getTime() : t0} />
            <span className="scrub-wrap">
              <input type="range" min={0} max={rows.length - 1} value={idx}
                     onChange={(e) => { setIdx(Number(e.target.value)); setPlaying(false); }} />
              {/* §5.4 影像涵蓋帶：3px 薄帶、段間空白＝真空白（不拼接）；
                  expired 不顯示（影像已清除，畫涵蓋帶會謊稱資料還在） */}
              {video?.video_status === "available" && video.segments.length > 0 && (
                <span className="vid-cover">
                  {video.segments.map((g) => {
                    const s = new Date(g.started_at).getTime();
                    const e = s + g.duration_s * 1000;
                    const pct = (t: number) => ((t - t0) / (t1 - t0 || 1)) * 100;
                    const L = Math.max(0, pct(s));
                    const R = Math.min(100, pct(e));
                    if (R <= 0 || L >= 100) return null;
                    return (<Fragment key={g.id}>
                      <span style={{ left: `${L}%`, width: `${R - L}%` }} />
                      {/* final=false 尾端：長度未定案——不畫成缺口（那是斷言
                          沒錄到），以處理中樣式延伸到軸末（§5.4） */}
                      {g.final === false && R < 100 && (
                        <span className="cov-proc"
                          style={{ left: `${R}%`, width: `${100 - R}%` }} />
                      )}
                    </Fragment>);
                  })}
                </span>
              )}
            </span>
          </div>

          {/* 圖表＝研究工作區 → 上滑抽屜（展開記憶，ui-spec §5） */}
          <details className="replay-drawer" open={drawerOpen}
            onToggle={(e) => {
              const o = e.currentTarget.open;
              setDrawerOpen(o);
              localStorage.setItem("replay-drawer-open", o ? "1" : "0");
            }}>
            <summary>SINR 與 RTT（與時間軸共用 X 軸）</summary>
            <Chart rows={rows} field="sinr" height={110} yLabel="SINR (dB)"
                   thresholds={[5, -2]} events={events} t0={t0} t1={t1} idx={idx}
                   onSeek={(i) => { setIdx(i); setPlaying(false); }}
                   onEvent={setOpenEv} />
            <Chart rows={rows} field="rtt_ms" height={70} yLabel="RTT (ms)"
                   t0={t0} t1={t1} idx={idx}
                   onSeek={(i) => { setIdx(i); setPlaying(false); }} />
          </details>
        </div>
      )}
      {openEv && (
        <EventModal ev={{ ...openEv, drone: meta?.drone_name ?? null }}
          onClose={() => setOpenEv(null)} />
      )}
      {sheet && (
        <LogIndexSheet url={sheet.url} title={sheet.title}
          onClose={() => setSheet(null)} />
      )}
    </div>
  );
}

/* ── 回放頁的輔助與元件（2026-09-08，對齊 doc/replay-redesign-proto.html）── */

/** 那一刻的飛行模式。遙測與鏈路樣本是兩條序列，時間不對齊——取最後一筆
 * 不晚於該時刻的（不內插：模式是離散狀態，插值沒有意義）。 */
function teleModeAt(tele: TeleRow[], time: string | undefined): string | null {
  if (!time || !tele.length) return null;
  const t = new Date(time).getTime();
  let m: string | null = null;
  for (const r of tele) {
    if (new Date(r.time).getTime() <= t) m = r.flight_mode ?? m;
    else break;
  }
  return m;
}

/** 時刻 → 最近的鏈路樣本序號（點事件、點圖表都走這裡）。 */
function nearestIdx(rows: LinkRow[], ms: number): number {
  let best = 0, bd = Infinity;
  rows.forEach((r, i) => {
    const d = Math.abs(new Date(r.time).getTime() - ms);
    if (d < bd) { bd = d; best = i; }
  });
  return best;
}

/** 時間軸上的帶：模式底色、事件釘點、指令三角、進度。
 * **viewBox 寬度與下面兩張圖一致（1000）**，所以三者的 X 真的對得起來。 */
function TrackBand({ tele, events, cmds, t0, t1, cur }: {
  tele: TeleRow[]; events: Ev[]; cmds: CmdRow[];
  t0: number; t1: number; cur: number;
}) {
  const X = (ms: number) => ((ms - t0) / (t1 - t0 || 1)) * 1000;
  const MC: Record<string, string> = {
    AUTO: "var(--series-3)", GUIDED: "var(--series-1)",
    LAND: "var(--series-2)", RTL: "var(--status-warn)",
  };
  // 模式段：切換時才開新的一段（照原樣不翻譯——PX4 HOLD 與 ArduPilot
  // LOITER 是不同的字、同一件事，翻譯會讓事後對 log 對不上）
  const segs: { mode: string; a: number; b: number }[] = [];
  tele.forEach((r) => {
    const t = new Date(r.time).getTime();
    const last = segs[segs.length - 1];
    if (!last || last.mode !== r.flight_mode) {
      if (last) last.b = t;
      segs.push({ mode: r.flight_mode ?? "—", a: t, b: t1 });
    }
  });
  return (
    <svg className="rp-band" viewBox="0 0 1000 22" preserveAspectRatio="none"
      role="img" aria-label="時間軸：模式、事件與指令">
      {segs.map((g, i) => {
        const x = X(g.a), w = Math.max(1, X(g.b) - X(g.a));
        return (
          <g key={i}>
            <rect x={x} y={13} width={w} height={8}
              fill={MC[g.mode] ?? "var(--surface-2)"} fillOpacity={0.5} />
            {w > 46 && (
              <text x={x + 4} y={20} fill="var(--muted)" fontSize="8">{g.mode}</text>
            )}
          </g>
        );
      })}
      {events.map((e) => (
        <line key={e.id} x1={X(new Date(e.time).getTime())}
          x2={X(new Date(e.time).getTime())} y1={2} y2={11}
          stroke={SEV_DOT[normSev(e.severity)]} strokeWidth={1.4} />
      ))}
      {cmds.map((c, i) => {
        const x = X(new Date(c.time).getTime());
        return <path key={i} d={`M${x} 0 l4 6 l-8 0 Z`} fill="var(--accent)" />;
      })}
      <line x1={X(cur)} x2={X(cur)} y1={0} y2={22} stroke="var(--accent)" strokeWidth={2} />
    </svg>
  );
}

/** 右側常駐面板：這一趟／事件（折疊、點列跳時刻）／指令。 */
function ReplayPanel({ sess, quality, rows, events, cmds, idx, evFilter,
                       setEvFilter, onSeekTime, onEvent }: {
  sess: SessionMeta | null; quality: Quality | null;
  rows: LinkRow[]; events: Ev[]; cmds: CmdRow[] | null; idx: number;
  evFilter: "all" | "warn" | "vehicle" | "system";
  setEvFilter: (v: "all" | "warn" | "vehicle" | "system") => void;
  onSeekTime: (ms: number) => void;
  onEvent: (e: Ev) => void;
}) {
  const sum = (() => {
    const v = sess?.summary;
    if (!v) return null;
    try { return typeof v === "string" ? JSON.parse(v) : v; } catch { return null; }
  })() as Record<string, number | null> | null;
  const curMs = rows[idx] ? new Date(rows[idx].time).getTime() : null;

  let shown = events;
  if (evFilter === "warn") shown = shown.filter((e) => normSev(e.severity) !== "info");
  if (evFilter === "vehicle") shown = shown.filter((e) => e.source === "vehicle");
  if (evFilter === "system") shown = shown.filter((e) => e.source !== "vehicle");
  // **時間正序**：讀一趟飛行是從頭讀到尾；折疊預設依「最近一次」降冪，
  // 那是即時流的順序，不是回放的順序
  const groups = foldEvents(shown.map((e) => ({ ...e, severity: e.severity })))
    .sort((a, b) => new Date(a.first).getTime() - new Date(b.first).getTime());

  const F = ({ k, v, u, dot }: {
    k: string; v: string; u?: string; dot?: string | null;
  }) => (
    <div className="fact">
      <div className="rp-k">{k}</div>
      <div className="rp-v">
        {dot && <span className="rp-dot" style={{ background: dot }} />}
        {v}{u && <small>{u}</small>}
      </div>
    </div>
  );

  return (
    <>
      <div className="card">
        <h3>這一趟
          <span className="spacer" />
          <InfoTip tip="樣本數＝這趟收到幾筆 5G 量測；SINR 與 RTT 是那些樣本的統計。「結束方式」分得出「上鎖」與「遙測中斷」——後者不代表飛行結束，只代表資料在那裡斷了。" />
        </h3>
        <div className="rp-facts">
          <F k="任務" v={sess?.mission_name ?? "無"} />
          <F k="時長" v={sess?.ended_at
            ? secs((new Date(sess.ended_at).getTime()
              - new Date(sess.started_at).getTime()) / 1000) : "進行中"} />
          <F k="樣本數" v={sum?.samples_total != null ? String(sum.samples_total) : "—"} />
          <F k="平均 SINR" v={fmt(sum?.avg_sinr)} u=" dB"
            dot={sum?.avg_sinr != null ? classifySinr(sum.avg_sinr).color : null} />
          <F k="最低 SINR" v={fmt(sum?.min_sinr)} u=" dB"
            dot={sum?.min_sinr != null ? classifySinr(sum.min_sinr).color : null} />
          <F k="平均 RTT" v={fmt(sum?.avg_rtt_ms, 0)} u=" ms" />
          <F k="最高高度" v={fmt(sum?.max_alt_rel, 0)} u=" m" />
          <F k="結束方式" v={sess?.end_reason
            ? END_LABELS[sess.end_reason] ?? sess.end_reason : "—"} />
        </div>
        {/* 有補傳才說（大多數架次沒這回事，不必每趟都掛一句） */}
        {quality && quality.backfilled > 0 && (
          <div className={quality.conflicts > 0 ? "tq-row tq-bad" : "tq-row"}>
            <span className="tq-main">
              遙測 {quality.live} 筆即時 · {quality.backfilled} 筆機上補傳
              {quality.conflicts > 0 && (
                <b>　{quality.conflicts} 筆落在即時資料已覆蓋的秒數上</b>
              )}
            </span>
          </div>
        )}
      </div>

      <div className="card card-grow">
        <h3>事件<span className="h3-note">{shown.length} 則</span>
          <span className="spacer" />
          <InfoTip tip="同一句話重複只佔一列（×N＋密度條）。點一列把時間軸跳到那一刻——判讀「那件事發生時訊號如何」就不必自己對時間。" />
        </h3>
        <div className="ev-filter rp-filter">
          {([["all", "全部"], ["warn", "警告以上"],
             ["vehicle", "機上"], ["system", "系統"]] as const).map(([k, l]) => (
            <button key={k} className={evFilter === k ? "on" : ""}
              onClick={() => setEvFilter(k)}>{l}</button>
          ))}
        </div>
        <div className="events rp-events">
          {!groups.length && <div className="empty">這組篩選沒有事件。</div>}
          {groups.map((g) => {
            const e = g.latest;
            const sev = normSev(e.severity);
            const ms = new Date(g.first).getTime();
            const near = curMs != null && Math.abs(ms - curMs) < 1500;
            return (
              <div key={g.key} className={`event ev-tap${near ? " rp-now" : ""}`}
                title={g.count > 1 ? foldTitle(g) : "點擊跳到那一刻並看詳情"}
                onClick={() => { onSeekTime(ms); onEvent(e); }}>
                <span className="dot" style={{ background: SEV_DOT[sev] }} />
                <time>{hm(g.first)}</time>
                <span className="detail">
                  {emph(evText({ type: e.type, detail: e.detail,
                    severity: sev }))}
                </span>
                {g.count > 1 && <span className="ev-count">×{g.count}</span>}
                {g.count > 1 && <EvDensity times={g.times} color={SEV_DOT[sev]} />}
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <h3>指令<span className="h3-note">{cmds ? `${cmds.length} 筆` : ""}</span>
          <span className="spacer" />
          <InfoTip tip="這一趟下了什麼、飛控接不接受、花了幾毫秒。含被擋下來的——「我按了但它沒動」是事後最需要回答的問題之一。點一列跳到那一刻。" />
        </h3>
        {/* 取不到與「沒有下過指令」不同形（§0.2e） */}
        {cmds === null && <div className="empty">無法取得指令紀錄。</div>}
        {cmds?.length === 0 && <div className="empty">這一趟沒有從地面站下過指令。</div>}
        {cmds?.map((c, i) => {
          const d = eventDetail(c.detail);
          const steps = Object.entries((d.steps ?? {}) as Record<string, {
            accepted?: boolean; ack_ms?: number }>)
            .map(([k, v]) => `${k}：${v.accepted ? "接受" : "未接受"}`
              + (v.ack_ms != null ? ` ${Math.round(v.ack_ms)}ms` : ""));
          return (
            <div key={i} className="rp-cmd" onClick={() => onSeekTime(new Date(c.time).getTime())}>
              <time>{hm(c.time)}</time>
              <div>
                <div className="rp-cmdname">{c.action}</div>
                {!!steps.length && (
                  <div className="rp-steps">
                    {steps.map((x) => <span className="rp-step" key={x}>{x}</span>)}
                  </div>
                )}
              </div>
              <span className={c.result === "accepted" ? "rp-ok" : "rp-bad"}>
                {c.result === "accepted" ? "已執行" : c.result}
              </span>
            </div>
          );
        })}
      </div>
    </>
  );
}
