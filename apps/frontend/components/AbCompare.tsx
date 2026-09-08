"use client";
import { ColumnLayer } from "@deck.gl/layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import CompareTabs from "@/components/CompareTabs";
import InfoTip from "@/components/InfoTip";
import { compareAlongPath, voxels, type AbResult, type ChainPoint,
  type Pt, type Sample, type Voxel } from "@/lib/chainage";
import { getJson } from "@/lib/fetchJson";
import { CANVAS, groundGrid } from "@/lib/geo";
import { API, CLIENT_HEADERS } from "@/lib/signal";
import { firstFleetPos } from "@/lib/store";

/** 比較頁（ui-spec §6b；2026-09-08 使用者核准的改版）。
 *
 * 從「前後兩趟」擴成「**基準 ＋ 對照 N 趟**」，並加上**比較維度**：
 *
 *   time     時間  同一台機不同時間的架次
 *   mission  任務  同一台機把同一條航線飛過多趟——**唯一路徑一致的維度**
 *   cross    機隊  不同機、不同任務
 *
 * 三個維度共用同一套對齊（沿基準軌跡的弧長里程，lib/chainage），差別在候選怎麼
 * 圈、標籤帶什麼，以及差異可以怎麼解讀。**基準是一趟，不是「前」**——兩趟時
 * 可以叫前後，三趟以上就不能。
 *
 * 標籤跟著維度換：只有時間時「08/13 16:37」就夠；跨機時不帶機名根本分不出
 * 誰是誰。差值熱區本質是兩兩比對，所以留一排 pill 選「現在看哪一趟對基準」。
 *
 * 解釋一律住 ⓘ（使用者要求 2026-09-08：畫面上不要太多解釋的文字）——
 * 版面上只留事實與數字。
 */

const IDC = ["#3987e5", "#d95926", "#199e70"];   // 識別色 1藍 2橘 3綠
const OVER = "#8f8b80";                          // 第 4 趟起：顏色不再承載識別
const tripColor = (i: number) => (i < IDC.length ? IDC[i] : OVER);
const BASE_INK = "#c9c5bb";                      // 基準線（虛線、中性色）
const VGRID = 10, VZ = 5;   // 體素 10×10×5 m
// 垂直放大：27 m 的高度差擺在 150 m 的場域上，不放大幾乎看不出來。
// **倍率一律寫在畫面上**（ⓘ）——偷偷放大的高度就是一張假的圖
const VEX = 2;

type Mode = "time" | "mission" | "cross";
const MODE_LABEL: Record<Mode, string> = {
  // 三個維度是同一個層級的名詞：時間／任務／機隊。**不用「跨機・跨任務」
  // 這種把兩件事並排的說法**——它讀起來像在描述操作，不像在指一個維度
  time: "時間", mission: "任務", cross: "機隊",
};

interface SessRow {
  id: string; drone_name: string; started_at: string;
  mission_id: string | null; mission_name: string | null;
  note: string | null;
  origin?: string | null;      // 'test'＝rig/驗收觸發的架次
}

const fmtT = (t: string) =>
  new Date(t).toLocaleString("zh-TW", { month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false });
const f1 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
const dd = (b: number | null, a: number | null) =>
  b == null || a == null ? "—" : `${b - a >= 0 ? "+" : ""}${(b - a).toFixed(1)}`;

/** 高度區間的寫法。**負的高度是真的**（起飛點以下的地形），不藏也不夾到 0；
 *  `-10–-5` 那種寫法沒有人讀得出來，所以用「至」。 */
const zLabel = (z: number) =>
  `${(z - VZ / 2).toFixed(0)} 至 ${(z + VZ / 2).toFixed(0)} m`;

const median = (v: number[]): number | null => {
  if (!v.length) return null;
  const s = [...v].sort((x, y) => x - y);
  return s.length % 2 ? s[(s.length - 1) / 2]
    : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
};

/** 發散色盤（dataviz 硬規則：兩極＋灰中點，中點絕不用第三個色相）。 */
function divergeRGB(d: number, max = 8): [number, number, number] {
  const t = Math.max(-1, Math.min(1, d / max));
  const grey: [number, number, number] = [143, 139, 128];
  const end: [number, number, number] = t >= 0 ? [57, 135, 229] : [217, 89, 38];
  const k = Math.abs(t);
  return [0, 1, 2].map((i) => Math.round(grey[i] + (end[i] - grey[i]) * k)) as
    [number, number, number];
}

interface TripRow {
  id: string; sess: SessRow; label: string; color: string;
  res: AbResult; paired: number; dS: number | null;
}

export default function AbCompare() {
  const [sessions, setSessions] = useState<SessRow[]>([]);
  // 「還在載入」與「取不到」必須分開說：兩者都是畫面空白，但前者會好、
  // 後者不會，而且後者若沿用載入中的字樣就是永遠的謊（§0.2e）
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("time");
  const [drone, setDrone] = useState<string | null>(null);
  const [missionId, setMissionId] = useState<string | null>(null);
  const [baseId, setBaseId] = useState<string | null>(null);
  const [sel, setSel] = useState<string[]>([]);
  const [heatId, setHeatId] = useState<string | null>(null);
  const [tracks, setTracks] = useState<Record<string, Sample[]>>({});
  const [plan, setPlan] = useState<Pt[] | null>(null);
  const [hover, setHover] = useState<Voxel | null>(null);
  const [zSel, setZSel] = useState<number | null>(null);   // 高度切片（null＝全部）
  const [noteEdit, setNoteEdit] = useState<string | null>(null);
  const [showTest, setShowTest] = useState(false);   // 測試架次是否列入

  // 架次清單（有樣本的才可比較——門檻與場域頁一致）。測試架次
  // （origin='test'）一律抓回、以開關切換並如實顯示隱藏了幾筆：
  // 不能讓使用者以為架次憑空消失
  useEffect(() => {
    // 取得失敗不得變成「沒有可比較的架次」（見 lib/fetchJson.ts）
    getJson<SessRow[]>(`${API}/api/sessions?limit=500&min_samples=10&include_test=true`)
      .then((rows) => {
        setSessions(rows);
        const q = new URLSearchParams(window.location.search);
        if (q.get("test") === "1") setShowTest(true);
        const qa = q.get("a"), qb = q.get("b");
        if (qa) setBaseId(qa);
        if (qb) setSel([qb]);
      })
      .catch(() => setLoadErr("無法取得架次清單"));
  }, []);

  // 選單內容：預設只列真飛行；已選中的架次即使是測試也保留，
  // 否則切換開關時選擇會憑空消失
  const chosen = useMemo(() => new Set([baseId, ...sel]), [baseId, sel]);
  const listed = useMemo(() => sessions.filter(
    (r) => showTest || r.origin !== "test" || chosen.has(r.id)),
    [sessions, showTest, chosen]);
  const hiddenTest = sessions.filter((r) => r.origin === "test").length;

  /** 這個維度下可以拿來比的架次（時間新→舊，清單本來就是這個序）。 */
  const cand = useMemo(() => {
    if (mode === "cross") return listed;
    const d = listed.filter((r) => r.drone_name === drone);
    return mode === "time" ? d : d.filter((r) => r.mission_id === missionId);
  }, [listed, mode, drone, missionId]);

  /** 標籤：帶到剛好能分辨為止，不多帶。 */
  const tripLabel = useCallback((s: SessRow | null | undefined): string => {
    if (!s) return "—";
    if (mode === "cross") return `${s.drone_name} · ${fmtT(s.started_at)}`;
    if (mode === "mission") {
      const seq = [...cand].reverse();   // 舊→新才數得出「第幾趟」
      const i = seq.findIndex((x) => x.id === s.id);
      return i >= 0 ? `第 ${i + 1} 趟 · ${fmtT(s.started_at)}` : fmtT(s.started_at);
    }
    return fmtT(s.started_at);
  }, [mode, cand]);

  // 預設落在**架次最多**的那台機：挑最新的那台可能只有一趟，一進來就是空畫面
  useEffect(() => {
    if (drone || !sessions.length) return;
    const cnt: Record<string, number> = {};
    for (const r of sessions) cnt[r.drone_name] = (cnt[r.drone_name] ?? 0) + 1;
    setDrone(Object.entries(cnt).sort((a, b) => b[1] - a[1])[0][0]);
  }, [sessions, drone]);

  // 換維度／換範圍之後把選擇重新落在合法的架次上。**不保留上一個維度的
  // 選擇**——那會讓畫面上出現這個範圍裡根本沒有的趟次
  useEffect(() => {
    if (!cand.length) { setBaseId(null); setSel([]); return; }
    const base = cand.some((s) => s.id === baseId) ? baseId! : cand[0].id;
    if (base !== baseId) setBaseId(base);
    const rest = cand.filter((s) => s.id !== base).map((s) => s.id);
    const keep = sel.filter((id) => rest.includes(id));
    const next = keep.length ? keep : rest.slice(0, 3);
    if (next.join() !== sel.join()) setSel(next);
  }, [cand, baseId, sel]);

  useEffect(() => {
    if (!sel.includes(heatId ?? "")) setHeatId(sel[0] ?? null);
  }, [sel, heatId]);

  // 切到「任務」維度時，把機與任務換到**真的有任務紀錄**的那一組：
  // 停在一台沒有任務的機上，畫面會是空的，那不是這個維度的樣子
  const seatMission = () => {
    const has = sessions.filter((s) => s.mission_id);
    if (!has.length) return;
    const mine = has.filter((s) => s.drone_name === drone);
    if (mine.length) {
      if (!mine.some((s) => s.mission_id === missionId))
        setMissionId(mine[0].mission_id);
      return;
    }
    const cnt: Record<string, number> = {};
    for (const r of has) cnt[r.drone_name] = (cnt[r.drone_name] ?? 0) + 1;
    const d = Object.entries(cnt).sort((a, b) => b[1] - a[1])[0][0];
    setDrone(d);
    setMissionId(has.find((s) => s.drone_name === d)!.mission_id);
  };

  // 切到「機隊」維度時預設就挑到**別台機**去：沿用上一個維度的選擇會讓
  // 它一進來全是同一台機的架次，那正是這個維度要對照的反面
  const seatCross = () => {
    const base = listed.find((s) => s.id === baseId) ?? listed[0];
    if (!base) return;
    setBaseId(base.id);
    const others = listed.filter((s) => s.id !== base.id
      && s.drone_name !== base.drone_name).slice(0, 2);
    const same = listed.filter((s) => s.id !== base.id
      && s.drone_name === base.drone_name).slice(0, 1);
    setSel([...others, ...same].map((s) => s.id));
  };

  // 軌跡（各抓一次即快取）
  useEffect(() => {
    for (const id of [baseId, ...sel]) {
      if (!id || tracks[id]) continue;
      getJson<{ link?: Sample[] }>(`${API}/api/sessions/${id}/track`)
        .then((d) => setTracks((t) => ({ ...t, [id]: (d.link ?? []) as Sample[] })))
        // `d.link ?? []` 在 HTTP 錯誤時會得到空陣列 → 畫面說「這趟沒量測」，
        // 那是把我方的取得失敗說成對方沒資料（§0.2e）
        .catch(() => setLoadErr("無法取得軌跡"));
    }
  }, [baseId, sel, tracks]);

  const baseSess = sessions.find((s) => s.id === baseId) ?? null;

  // 參考路徑：任務維度下每一趟共用同一條計畫航線（共同 X 軸的最佳來源）；
  // 其他維度沒有共同航線，基準就是基準那一趟的軌跡
  useEffect(() => {
    const mid = mode === "mission" ? missionId : null;
    if (!mid) { setPlan(null); return; }
    fetch(`${API}/api/missions/${mid}/waypoints`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setPlan((d?.waypoints ?? [])
        .filter((w: Pt) => w.lat && w.lon)))
      .catch(() => setPlan(null));
  }, [mode, missionId]);

  const baseRows = (baseId && tracks[baseId]) || [];

  const rows: TripRow[] = useMemo(() => {
    if (!baseRows.length) return [];
    const out: TripRow[] = [];
    for (const id of sel) {
      const sess = sessions.find((s) => s.id === id);
      const rs = tracks[id];
      if (!sess || !rs?.length) continue;
      const res = compareAlongPath(baseRows, rs, plan);
      const both = res.chainage.filter((c) => c.a_sinr != null && c.b_sinr != null);
      out.push({
        id, sess, label: tripLabel(sess), color: tripColor(out.length), res,
        paired: both.length,
        dS: median(both.map((c) => c.b_sinr! - c.a_sinr!)),
      });
    }
    return out;
  }, [baseRows, sel, sessions, tracks, plan, tripLabel]);

  const heat = rows.find((r) => r.id === heatId) ?? rows[0] ?? null;
  // 體素而不是平面格：同一個地面格，飛 3 m 與飛 25 m 量到的是兩件事
  const vox = useMemo(() => {
    if (!heat || !baseRows.length) return [];
    const o = baseRows.find((r) => r.lat != null && r.lon != null);
    return o ? voxels(baseRows, tracks[heat.id] ?? [], o, VGRID, VZ) : [];
  }, [heat, baseRows, tracks]);
  // 高度層（高→低）；切到別趟時若那一層不存在就回「全部」
  const zLayers = useMemo(() =>
    [...new Set(vox.map((v) => v.z))].sort((a, b) => b - a), [vox]);
  useEffect(() => {
    if (zSel != null && !zLayers.includes(zSel)) setZSel(null);
  }, [zLayers, zSel]);
  const shown = useMemo(() =>
    vox.filter((v) => zSel == null || v.z === zSel), [vox, zSel]);
  const nBoth = shown.filter((v) => v.delta != null).length;

  const ready = rows.length > 0;

  // 差值熱區地圖（沿用場域頁的暖畫布底＋地面網格）
  const mapRef = useRef<maplibregl.Map | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const ovRef = useRef<MapboxOverlay | null>(null);
  const [mapReady, setMapReady] = useState(false);
  // 地圖容器只在有資料時才渲染——初始化必須等它進 DOM（deps 含 ready），
  // 否則 mount 當下 ref 是 null、地圖永遠不會建
  useEffect(() => {
    if (!boxRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: boxRef.current, zoom: firstFleetPos() ? 16 : 1.5,
      center: firstFleetPos() ?? [0, 20],
      // 體素是 3D 的，俯視角看不出高度——開頁就給俯仰，之後使用者自己轉
      pitch: 45, bearing: -22, maxPitch: 80,
      attributionControl: false, cooperativeGestures: true,
      style: { version: 8, sources: {}, layers: [
        { id: "canvas", type: "background", paint: { "background-color": CANVAS } }] },
    });
    mapRef.current = map;
    map.on("load", () => {
      // 網格錨在資料原點：先建空 source，cells 算出後再填
      map.addSource("grid", { type: "geojson",
        data: { type: "FeatureCollection", features: [] } as GeoJSON.FeatureCollection });
      map.addLayer({ id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#2b2a26", "line-width": 1 } });
      const ov = new MapboxOverlay({ interleaved: true, layers: [] });
      map.addControl(ov);
      ovRef.current = ov;
      setMapReady(true);
    });
    // cleanup 必須把**與這張地圖同生命週期的東西全部歸零**，不只 mapRef：
    // ovRef 還指著已銷毀的 overlay；mapReady 留在 true 時，新地圖 load 後的
    // setMapReady(true) 是 no-op → 推層 effect 再也沒有觸發條件 →
    // **新 overlay 永遠停在 layers: []，熱區一片空白**
    return () => {
      map.remove();
      mapRef.current = null;
      ovRef.current = null;
      setMapReady(false);
    };
  }, [ready]);

  useEffect(() => {
    if (!mapReady || !ovRef.current) return;
    const grid = 10, M_LAT = 110574;
    const mLon = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);
    // 體素：ColumnLayer 的四邊柱（diskResolution 4 ＋ angle 45 ＝ 正方形）。
    // 底面擺在 (z − vz/2)×VEX，高度 vz×VEX——**放大只作用在垂直**，
    // 水平的 10 m 仍然是 10 m
    const col = (id: string, data: Voxel[], both: boolean) => new ColumnLayer<Voxel>({
      id, data,
      diskResolution: 4, angle: 45,
      radius: VGRID / Math.SQRT2,     // 外接圓 → 邊長剛好 10 m
      extruded: true, filled: true,
      wireframe: !both,               // 無對照只留框：填滿會蓋掉有對照的那幾顆
      getPosition: (v) => [v.lon, v.lat, (v.z - VZ / 2) * VEX],
      getElevation: VZ * VEX,
      elevationScale: 1,
      getFillColor: (v) => (both
        ? [...divergeRGB(v.delta!), 235] as [number, number, number, number]
        : [143, 139, 128, 26]),
      getLineColor: [143, 139, 128, 150],
      lineWidthUnits: "pixels" as const, getLineWidth: 1,
      pickable: both,
      onHover: (info) => setHover((info.object as Voxel) ?? null),
      updateTriggers: { getPosition: data, getFillColor: data },
    });
    ovRef.current.setProps({ layers: [
      // 無對照先畫（在下），有對照的疊在上面——找得到差異在哪永遠優先
      col("vox-none", shown.filter((v) => v.delta == null), false),
      col("vox-both", shown.filter((v) => v.delta != null), true),
    ] });
    if (shown.length && mapRef.current) {
      (mapRef.current.getSource("grid") as maplibregl.GeoJSONSource | undefined)
        ?.setData(groundGrid(shown[0].lat, shown[0].lon));
      const b = new maplibregl.LngLatBounds();
      for (const v of shown) b.extend([v.lon, v.lat]);
      // 上方留多一點：體素是往上長的，只給地面足跡對齊會把柱子頂出畫面外
      mapRef.current.fitBounds(b, { animate: false, maxZoom: 18,
        padding: { top: 140, bottom: 40, left: 60, right: 60 } });
    }
  }, [mapReady, shown]);

  async function saveNote(id: string, note: string) {
    await fetch(`${API}/api/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
      body: JSON.stringify({ note }),
    }).catch(() => {});
    setSessions((rs) => rs.map((r) => (r.id === id ? { ...r, note } : r)));
  }

  /** 備註即實驗標籤（沿用 v4：直接可編）。 */
  const noteCell = (s: SessRow | null) => {
    if (!s) return null;
    if (noteEdit === s.id) {
      return (
        <input className="cmp-noteedit" autoFocus defaultValue={s.note ?? ""}
          placeholder="實驗標籤"
          onBlur={(e) => { saveNote(s.id, e.target.value); setNoteEdit(null); }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }} />
      );
    }
    return (
      <button className="cmp-note" title="編輯備註（實驗標籤）"
        onClick={() => setNoteEdit(s.id)}>{s.note || "✎"}</button>
    );
  };

  const missionsOf = (d: string | null) => {
    const seen = new Map<string, string>();
    for (const r of sessions) {
      if (r.drone_name === d && r.mission_id)
        seen.set(r.mission_id, r.mission_name ?? "（未命名航線）");
    }
    return [...seen.entries()];
  };
  const noMission = mode === "mission" && missionsOf(drone).length === 0;

  return (
    <div className="ab-page">
      <div className="ab-head">
        <CompareTabs active="ab" />
        <span className="spacer" style={{ flex: 1 }} />
        {hiddenTest > 0 && (
          <label className="ab-testtoggle" title="測試/驗收觸發的架次（origin=test）">
            <input type="checkbox" checked={showTest}
              onChange={(e) => setShowTest(e.target.checked)} />
            含測試架次（{hiddenTest}）
          </label>
        )}
      </div>

      {loadErr && <div className="card"><div className="form-err">{loadErr}</div></div>}

      {/* ① 比較單位：先講清楚在比什麼，再選誰跟誰 */}
      <div className="card">
        <h3>比較維度<span className="h3-note">
          <InfoTip tip={"三個維度用同一套對齊：沿基準軌跡的弧長里程，不是時間"
            + "（兩趟速度不同，時間對齊會錯位）；偏離基準路徑逾 60 m 的樣本不納入。"
            + "　時間＝同一台機不同時間，路徑不保證一樣。"
            + "　任務＝同一台機把同一條任務飛過多趟，唯一路徑一致的維度，共同區間會接近全滿。"
            + "　機隊＝不同機、不同任務，差異可能來自機或模組本身，不只是位置。"} />
        </span></h3>
        <div className="sess-pills">
          {(Object.keys(MODE_LABEL) as Mode[]).map((k) => (
            <button key={k} className={`pill${mode === k ? " on" : ""}`}
              onClick={() => {
                setMode(k);
                if (k === "mission") seatMission();
                if (k === "cross") seatCross();
              }}>{MODE_LABEL[k]}</button>
          ))}
        </div>
        {mode !== "cross" && (
          <div className="cmp-scope">
            <span className="hint-line">機</span>
            <select value={drone ?? ""} onChange={(e) => {
              setDrone(e.target.value);
              const ms = missionsOf(e.target.value);
              if (mode === "mission") setMissionId(ms[0]?.[0] ?? null);
            }}>
              {[...new Set(sessions.map((s) => s.drone_name))].map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
            {mode === "mission" && (<>
              <span className="hint-line">任務</span>
              <select value={missionId ?? ""} disabled={noMission}
                onChange={(e) => setMissionId(e.target.value)}>
                {noMission
                  ? <option value="">（沒有任務紀錄）</option>
                  : missionsOf(drone).map(([id, nm]) => (
                    <option key={id} value={id}>{nm}</option>))}
              </select>
            </>)}
          </div>
        )}
      </div>

      {/* **不足以比較時直說是哪一種不足**：沒有候選、只有一趟，是兩件不同的事 */}
      {!loadErr && sessions.length > 0 && cand.length < 2 && (
        <div className="card"><div className="empty">
          {noMission
            ? `${drone} 沒有任何一趟掛著任務——這個維度要先有任務紀錄。`
            : cand.length === 0 ? "這個範圍裡沒有任何架次。"
            : mode === "mission"
              ? `${drone} 在這條任務上只有 1 趟——一趟不能比。`
              : "這個範圍裡只有 1 趟——一趟不能比。"}
        </div></div>
      )}

      {cand.length >= 2 && (
        <div className="card">
          <h3>比較對象<span className="h3-note">
            <InfoTip tip={"所有對照都對同一個基準算，所以摘要表的 Δ 之間可以互相比。"
              + "第 4 趟起顏色一律灰（顏色到此不再承載識別），改看線末的標籤。"} />
          </span></h3>
          <div className="cmp-scope">
            <span className="hint-line">基準</span>
            <select value={baseId ?? ""} onChange={(e) => setBaseId(e.target.value)}>
              {cand.map((s) => (
                <option key={s.id} value={s.id}>{tripLabel(s)}</option>
              ))}
            </select>
            {baseSess?.mission_name && (
              <span className="chip">{baseSess.mission_name}</span>
            )}
          </div>
          <div className="sess-pills cmp-tripsel">
            {cand.filter((s) => s.id !== baseId).map((s) => {
              const on = sel.includes(s.id);
              const r = rows.find((x) => x.id === s.id);
              return (
                <button key={s.id} className={`pill${on ? " on" : ""}`}
                  title={s.note ?? undefined}
                  onClick={() => setSel((cur) => cur.includes(s.id)
                    ? cur.filter((x) => x !== s.id) : [...cur, s.id])}>
                  <span className="dot" style={{
                    background: on && r ? r.color : "var(--hairline)" }} />
                  {tripLabel(s)}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {cand.length >= 2 && !ready && (
        <div className="card"><div className="empty">
          {sel.length ? "載入軌跡中…" : "沒有選任何對照——上面挑一趟以上。"}
        </div></div>
      )}

      {ready && (<>
        <div className="card">
          <h3>沿里程訊號<span className="h3-note">
            <InfoTip tip={"每格取該里程區間內樣本的平均；虛線是 5／13 dB 門檻"
              + "（與 backend 事件門檻同一出處）。「逐段」只比對兩趟都飛過的區間、"
              + "逐段取差再取中位數；摘要表的 Δ 是各自整體統計之差，含各自獨飛的"
              + "部分——兩個都對，差很多代表兩趟走過的範圍差很多。持平門檻 ±2 dB。"
              + (plan ? "　基準路徑＝這條任務的計畫航線。" : "　基準路徑＝基準那一趟的軌跡。")} />
          </span></h3>
          <MultiChart rows={rows} />
          <div className="cmp-verdicts">
            {rows.map((r) => (
              <div className="cmp-vrow" key={r.id}>
                <span className="dot" style={{ background: r.color }} />
                <span>{r.label}</span>
                <span className="hint-line">{
                  r.dS == null ? "沒有共同區間"
                    : Math.abs(r.dS) < 2 ? "大致持平"
                    : r.dS > 0 ? "較基準好" : "較基準差"
                }</span>
                <span className="spacer" style={{ flex: 1 }} />
                <span className="cmp-vnum">
                  {r.dS == null ? "—"
                    : `逐段 ${r.dS > 0 ? "+" : ""}${f1(r.dS)} dB`}
                </span>
                <span className="cmp-vn2">共同區間 {r.paired}</span>
                {rows.length === 1 && <RsrpTip res={r.res} dS={r.dS} />}
              </div>
            ))}
          </div>
          {/* ΔRSRP 只在單趟對照時畫得出意思——多趟疊在同一條 24px 帶上分不出誰是誰 */}
          {rows.length === 1 && <RsrpBand pts={rows[0].res.chainage} />}
        </div>

        <div className="card">
          <h3>摘要<span className="h3-note">
            <InfoTip tip={"p5＝最差 5%——尾部才是斷鏈的來源，所以它比均值重要。"
              + "Δ 是對基準的差（正＝比基準好）。共同區間＝這一趟與基準都有樣本的"
              + `里程格數，太少（<3）時趨勢不足採信。分箱 ${rows[0].res.binM} m。`} />
          </span></h3>
          <table className="table ab-sum">
            <thead><tr>
              <th>趟次</th>
              {mode === "cross" && <th>任務</th>}
              <th className="num">均值</th><th className="num">p50</th>
              <th className="num">p5</th><th className="num">樣本數</th>
              <th className="num">Δp50</th><th className="num">共同區間</th>
            </tr></thead>
            <tbody>
              <tr>
                <td>
                  <span className="chip">基準 {tripLabel(baseSess)}</span>
                  {noteCell(baseSess)}
                </td>
                {mode === "cross" && (
                  <td className="cmp-mis">{baseSess?.mission_name ?? "—"}</td>)}
                <td className="num">{f1(rows[0].res.summary.a.mean)}</td>
                <td className="num">{f1(rows[0].res.summary.a.p50)}</td>
                <td className="num"><b>{f1(rows[0].res.summary.a.p5)}</b></td>
                <td className="num">{rows[0].res.summary.a.n.toLocaleString()}</td>
                <td className="num">—</td><td className="num">—</td>
              </tr>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    <span className="chip">
                      <span className="dot" style={{ background: r.color }} />
                      {r.label}
                    </span>
                    {noteCell(r.sess)}
                  </td>
                  {mode === "cross" && (
                    <td className="cmp-mis">{r.sess.mission_name ?? "—"}</td>)}
                  <td className="num">{f1(r.res.summary.b.mean)}</td>
                  <td className="num">{f1(r.res.summary.b.p50)}</td>
                  <td className="num"><b>{f1(r.res.summary.b.p5)}</b></td>
                  <td className="num">{r.res.summary.b.n.toLocaleString()}</td>
                  <td className="num">
                    {dd(r.res.summary.b.p50, r.res.summary.a.p50)}
                  </td>
                  <td className="num"
                    title={r.paired < 3 ? "共同區間太少，趨勢不足採信" : undefined}>
                    {r.paired}{r.paired < 3 ? " ⚠" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* 誠實：不阻止比較，但不假裝對等 */}
          {(() => {
            const a = rows[0].res.summary.a.n;
            const big = rows.filter((r) => {
              const b = r.res.summary.b.n;
              return a > 0 && b > 0 && Math.max(a, b) / Math.min(a, b) > 3;
            });
            return big.length ? (
              <div className="hint-line">
                {big.map((r) => r.label).join("、")}：樣本數與基準差 3 倍以上
              </div>
            ) : null;
          })()}
          {(rows[0].res.dropped.a > 0 || rows.some((r) => r.res.dropped.b > 0)) && (
            <div className="hint-line">
              偏離基準路徑逾 60 m 而未納入：基準 {rows[0].res.dropped.a} 筆
              {rows.filter((r) => r.res.dropped.b > 0)
                .map((r) => `，${r.label} ${r.res.dropped.b} 筆`).join("")}
            </div>
          )}
        </div>

        <div className="card">
          <h3>差值熱區<span className="h3-note">
            <InfoTip tip={`這一趟減基準，體素 ${VGRID}×${VGRID}×${VZ} m。`
              + "訊號分佈在空間裡：同一個地面格，飛 3 m 與飛 25 m 量到的是兩件事，"
              + "壓成平面等於把它們平均掉。發散色盤：兩極用識別色、中點是灰"
              + "——中點絕不用第三個色相，否則「沒變化」會看起來像另一種變化。"
              + "線框＝只有其中一趟飛過那顆體素，那不是「沒變化」。"
              + `垂直放大 ${VEX}×（高度差擺在整個場域上，不放大幾乎看不出來）；`
              + "水平仍是實際尺度。後面的體素會被前面的擋住——要看清楚某一層"
              + "就用高度切片，一次只看一層。地圖可以拖曳旋轉，俯視角即平面圖。"} />
          </span></h3>
          {/* 熱區本質是兩兩比對，所以要選一趟 */}
          <div className="sess-pills">
            {rows.map((r) => (
              <button key={r.id} className={`pill${heat?.id === r.id ? " on" : ""}`}
                onClick={() => setHeatId(r.id)}>{r.label}</button>
            ))}
          </div>
          {vox.length === 0 ? (
            <div className="empty" style={{ marginTop: 8 }}>
              這一趟與基準沒有共同飛過的體素——不畫空圖假裝有比較。
            </div>
          ) : (<>
            {/* 高度切片：遮擋是 3D 的固有代價，一次只看一層時那一層才是完整的 */}
            <div className="cmp-scope">
              <span className="hint-line">高度</span>
              <div className="sess-pills">
                <button className={`pill${zSel == null ? " on" : ""}`}
                  onClick={() => setZSel(null)}>全部</button>
                {zLayers.map((z) => (
                  <button key={z} className={`pill${zSel === z ? " on" : ""}`}
                    onClick={() => setZSel(z)}>{zLabel(z)}</button>
                ))}
              </div>
            </div>
            <div className="ab-map" ref={boxRef} />
            <div className="ab-legend">
              <span className="sw" style={{
                background: `rgb(${divergeRGB(-8).join(",")})` }} />比基準差
              <span className="sw" style={{ background: "rgb(143,139,128)" }} />無變化
              <span className="sw" style={{
                background: `rgb(${divergeRGB(8).join(",")})` }} />比基準好
              <span className="sw sw-none" />無對照
              <span className="cmp-vn2">
                　共同體素 {nBoth}　只有一趟 {shown.length - nBoth}
              </span>
              {hover && (
                <span className="meta">
                　{zLabel(hover.z)}　基準 {f1(hover.a_sinr)}（{hover.a_n}）
                  · 這趟 {f1(hover.b_sinr)}（{hover.b_n}）
                  {hover.delta != null ? `· Δ ${f1(hover.delta)} dB` : "· 無對照"}
                </span>
              )}
            </div>
          </>)}
        </div>
      </>)}
    </div>
  );
}

/** RSRP 對照的判讀住 ⓘ（單趟對照才成立）。**只描述現象，不宣告成因**——
 * 機只知道訊號變差、不知道為什麼；把推測寫成事實就是替使用者下結論。 */
function RsrpTip({ res, dS }: { res: AbResult; dS: number | null }) {
  const rp = res.chainage.filter((c) => c.a_rsrp != null && c.b_rsrp != null);
  const dR = median(rp.map((c) => c.b_rsrp! - c.a_rsrp!));
  if (dR == null || dS == null) return null;
  const FLAT_R = 2, SIG_S = 1.5;
  const say = Math.abs(dR) < FLAT_R && dS < -SIG_S
      ? "RSRP 大致持平而 SINR 下降 → 符合外部雜訊升高的特徵"
    : Math.abs(dR) < FLAT_R && dS > SIG_S
      ? "RSRP 大致持平而 SINR 上升 → 符合外部雜訊下降的特徵"
    : dR < -FLAT_R && dS < -SIG_S
      ? "RSRP 同步下降 → 變因在訊號強度側（距離、遮蔽或發射端）"
    : dR > FLAT_R && dS > SIG_S
      ? "RSRP 同步上升 → 變因在訊號強度側（距離、遮蔽或發射端）"
    : "RSRP 與 SINR 沒有一致的走勢，無從指出變因";
  return <InfoTip tip={`${say}（RSRP 逐段差值中位數 ${f1(dR)} dB）`} />;
}

/** 沿里程主圖：基準一條虛線＋每趟一條，線末標籤排在右側留白。 */
function MultiChart({ rows }: { rows: TripRow[] }) {
  const H = 190, W = 1000, L = 44, T = 12, Bm = 22;
  // 右側留白照**最長的標籤**算：機隊維度的標籤帶機名，固定寬度會把字切掉，
  // 而切掉的正是用來分辨誰是誰的那一段
  const tw = (t: string) => [...t]
    .reduce((a, ch) => a + (ch.charCodeAt(0) > 255 ? 10 : 5.4), 0);
  const R = Math.min(320, 26 + Math.max(...rows.map((r) => tw(r.label)), tw("基準")));

  const pts = rows.flatMap((r) => r.res.chainage);
  const xs = pts.map((p) => p.m);
  const vals = pts.flatMap((p) => [p.a_sinr, p.b_sinr])
    .filter((v): v is number => v != null);
  if (!xs.length || !vals.length) return null;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...vals, -2), y1 = Math.max(...vals, 13);
  const pad = (y1 - y0) * 0.12 || 1; y0 -= pad; y1 += pad;
  const X = (v: number) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R);
  const Y = (v: number) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - Bm);

  const grid: string[] = [];
  for (const v of [5, 13]) {
    if (v < y0 || v > y1) continue;
    grid.push(`<line x1="${L}" x2="${W - R}" y1="${Y(v).toFixed(1)}" `
      + `y2="${Y(v).toFixed(1)}" stroke="var(--status-warn)" stroke-width="1" `
      + `stroke-dasharray="4 4" stroke-opacity=".45"/>`);
  }
  const ticks = [y0 + (y1 - y0) * 0.15, (y0 + y1) / 2, y1 - (y1 - y0) * 0.15];

  interface Lab { ex: number; ey: number; y: number; color: string; text: string }
  const labels: Lab[] = [];
  const line = (cs: ChainPoint[], key: "a_sinr" | "b_sinr",
    color: string, text: string, dash: boolean) => {
    const q = cs.filter((p) => p[key] != null);
    if (q.length < 2) return "";
    const d = q.map((p, i) => `${i ? "L" : "M"}${X(p.m).toFixed(1)} `
      + `${Y(p[key]!).toFixed(1)}`).join(" ");
    const last = q[q.length - 1];
    labels.push({ ex: X(last.m), ey: Y(last[key]!),
      y: Y(last[key]!) + 3.5, color, text });
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="2"`
      + `${dash ? ' stroke-dasharray="5 4"' : ""} vector-effect="non-scaling-stroke"/>`
      + q.map((p) => `<circle cx="${X(p.m).toFixed(1)}" `
        + `cy="${Y(p[key]!).toFixed(1)}" r="2.2" fill="${color}"/>`).join("");
  };
  let body = line(rows[0].res.chainage, "a_sinr", BASE_INK, "基準", true);
  for (const r of rows) body += line(r.res.chainage, "b_sinr", r.color, r.label, false);

  // 標籤全部靠右側留白排，彼此至少差 12px；引線接回各自線末的真實位置
  const lx = W - R + 10;
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].y - labels[i - 1].y < 12) labels[i].y = labels[i - 1].y + 12;
  }
  const over = labels.length ? labels[labels.length - 1].y - (H - Bm) : 0;
  if (over > 0) for (const l of labels) l.y -= over;
  for (const l of labels) {
    body += `<path d="M${l.ex.toFixed(1)} ${l.ey.toFixed(1)} `
      + `L${(lx - 4).toFixed(1)} ${(l.y - 3.5).toFixed(1)}" fill="none" `
      + `stroke="${l.color}" stroke-width="1" stroke-opacity=".45"/>`
      + `<text x="${lx}" y="${l.y.toFixed(1)}" fill="${l.color}" `
      + `font-size="10">${esc(l.text)}</text>`;
  }

  const axis = ticks.map((v) =>
    `<line x1="${L}" x2="${W - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" `
    + `stroke="var(--hairline)"/><text x="${L - 6}" y="${(Y(v) + 3.5).toFixed(1)}" `
    + `fill="var(--muted)" font-size="9" text-anchor="end">${v.toFixed(0)}</text>`)
    .join("");

  return (
    <div className="ab-chart cmp-chart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
        aria-label="沿里程訊號（基準與對照趟）"
        dangerouslySetInnerHTML={{ __html: grid.join("") + axis + body
          + `<text x="${L}" y="${H - 5}" fill="var(--muted)" font-size="9">0 m</text>`
          + `<text x="${W - R}" y="${H - 5}" fill="var(--muted)" font-size="9" `
          + `text-anchor="end">${Math.round(x1)} m</text>`
          + `<text x="${L}" y="${T - 2}" fill="var(--muted)" font-size="9">SINR dB</text>` }} />
    </div>
  );
}

/** 架次備註是使用者輸入，進 SVG 前要跳脫——否則一個 `<` 就能毀掉整張圖。 */
function esc(s: string): string {
  return s.replace(/[&<>]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);
}

/** RSRP 迷你帶：24px、同 X 軸、只畫 這趟−基準 的差值走勢——不做雙軸 */
function RsrpBand({ pts }: { pts: ChainPoint[] }) {
  const W = 1000, H = 24;
  const d = pts.map((p) => (p.a_rsrp != null && p.b_rsrp != null
    ? p.b_rsrp - p.a_rsrp : null));
  if (!d.some((v) => v != null)) return null;
  const xs = pts.map((p) => p.m);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const m = Math.max(4, ...d.filter((v): v is number => v != null).map(Math.abs));
  const X = (v: number) => ((v - x0) / (x1 - x0 || 1)) * W;
  const Y = (v: number) => H / 2 - (v / m) * (H / 2 - 2);
  return (
    <div className="ab-rsrp">
      <span className="meta">ΔRSRP</span>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <line x1={0} x2={W} y1={H / 2} y2={H / 2} stroke="var(--hairline)"
          strokeWidth="1" vectorEffect="non-scaling-stroke" />
        <polyline fill="none" stroke="var(--ink-2)" strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
          points={pts.map((p, i) => (d[i] == null ? null : `${X(p.m)},${Y(d[i]!)}`))
            .filter(Boolean).join(" ")} />
      </svg>
    </div>
  );
}
