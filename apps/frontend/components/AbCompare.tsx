"use client";
import { PolygonLayer } from "@deck.gl/layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useMemo, useRef, useState } from "react";

import { compareAlongPath, deltaCells, type ChainPoint, type DeltaCell, type Sample }
  from "@/lib/chainage";
import CompareTabs from "@/components/CompareTabs";
import { CANVAS, groundGrid } from "@/lib/geo";
import { getJson } from "@/lib/fetchJson";
import { API, CLIENT_HEADERS } from "@/lib/signal";
import { firstFleetPos } from "@/lib/store";

/** 前後比較頁（ui-spec §6b，023——使用者核准 2026-08-12）。
 *
 * 回答的問題與場域頁（§6 v4）不同：v4＝「這場域哪裡弱」（多趟累積無基準）；
 * 本頁＝「這條路徑，改善措施前後差多少」（兩趟對照、有前後之分）。
 *
 * 三塊：①沿路徑訊號主圖（X＝弧長里程非時間——兩趟速度不同，時間對齊會
 * 錯位）②摘要表（CDF 經使用者二次否決，分佈資訊改以數字承接、p5 為重點）
 * ③差值熱區（發散色盤＋灰中點；兩趟都有樣本才上色）。
 * RSRP 不做雙軸：主圖下 24px 迷你帶＋卡頭自動判讀句，句尾必附依據數值。
 */

const A_COLOR = "#3987e5";   // 前（識別色第 1 槽，CVD 實測通過）
const B_COLOR = "#d95926";   // 後（第 2 槽）
const SMOOTH_WIN = 5;        // 平滑窗＝5 格 × 10m ＝ 50m（卡頭標示）

interface SessRow {
  id: string; drone_name: string; started_at: string;
  mission_id: string | null; mission_name: string | null;
  note: string | null;
  origin?: string | null;      // 'test'＝rig/驗收觸發的架次
  summary: { samples_total?: number } | string | null;
}

const fmtT = (t: string) =>
  new Date(t).toLocaleString("zh-TW", { month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false });
const f1 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));

/** 發散色盤（dataviz 硬規則：兩極＋灰中點，中點絕不用第三個色相）。
 * 兩極沿用同一對識別色，避免全站色語言分裂。 */
function divergeRGB(d: number, max = 8): [number, number, number] {
  const t = Math.max(-1, Math.min(1, d / max));
  const grey: [number, number, number] = [143, 139, 128];
  const pos: [number, number, number] = [57, 135, 229];    // 改善→藍
  const neg: [number, number, number] = [217, 89, 38];     // 惡化→橘
  const end = t >= 0 ? pos : neg;
  const k = Math.abs(t);
  return [0, 1, 2].map((i) => Math.round(grey[i] + (end[i] - grey[i]) * k)) as
    [number, number, number];
}

export default function AbCompare() {
  const [sessions, setSessions] = useState<SessRow[]>([]);
  // 「還在載入」與「取不到」必須分開說：兩者都是畫面空白，但前者會好、
  // 後者不會，而且後者若沿用載入中的字樣就是永遠的謊（§0.2e）
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [aId, setAId] = useState<string | null>(null);
  const [bId, setBId] = useState<string | null>(null);
  const [tracks, setTracks] = useState<Record<string, Sample[]>>({});
  const [plan, setPlan] = useState<{ lat: number; lon: number }[] | null>(null);
  const [hover, setHover] = useState<DeltaCell | null>(null);
  const [noteEdit, setNoteEdit] = useState<string | null>(null);
  const [showTest, setShowTest] = useState(false);   // 測試架次是否列入選單

  // 架次清單（有樣本的才可比較——門檻與場域頁一致）。
  // 測試架次（origin='test'）後端預設不回：比較頁本來就該以真飛行為主，
  // 但驗收/實驗用的測試飛行也需要看得到——一律抓回、以開關切換並如實
  // 顯示隱藏了幾筆（不能讓使用者以為架次憑空消失）
  useEffect(() => {
    // 取得失敗不得變成「沒有可比較的架次」（見 lib/fetchJson.ts）
    getJson<SessRow[]>(`${API}/api/sessions?limit=500&min_samples=10&include_test=true`)
      .then((rows: SessRow[]) => {
        setSessions(rows);
        const q = new URLSearchParams(window.location.search);
        const qa = q.get("a"), qb = q.get("b");
        if (q.get("test") === "1") setShowTest(true);
        // 預設：**同一條航線**飛過 ≥2 趟者取最近兩趟（前＝較早、後＝較晚）。
        // 不同航線的兩趟沒有共同里程軸，預設選到它們等於一開頁就是錯的比較
        const byMission = new Map<string, SessRow[]>();
        for (const r of rows) {
          if (!r.mission_id) continue;
          byMission.set(r.mission_id, [...(byMission.get(r.mission_id) ?? []), r]);
        }
        const pair = [...byMission.values()].find((g) => g.length >= 2);
        setAId(qa ?? pair?.[1]?.id ?? null);   // 清單為時間新→舊，[1] 是較早的＝前
        setBId(qb ?? pair?.[0]?.id ?? null);
      })
      .catch(() => setLoadErr("無法取得架次清單"));
  }, []);

  // 兩趟軌跡（各抓一次即快取）
  useEffect(() => {
    for (const id of [aId, bId]) {
      if (!id || tracks[id]) continue;
      getJson<{ link?: Sample[] }>(`${API}/api/sessions/${id}/track`)
        .then((d) => setTracks((t) => ({ ...t, [id]: (d.link ?? []) as Sample[] })))
        // `d.link ?? []` 在 HTTP 錯誤時會得到空陣列 → 畫面說「這趟沒量測」，
        // 那是把我方的取得失敗說成對方沒資料（§0.2e）
        .catch(() => setLoadErr("無法取得軌跡"));
    }
  }, [aId, bId, tracks]);

  const aSess = sessions.find((s) => s.id === aId) ?? null;
  const bSess = sessions.find((s) => s.id === bId) ?? null;
  // 選單內容：預設只列真飛行；已選中的架次即使是測試也保留在選項裡，
  // 否則切換開關時選擇會憑空消失
  const listed = sessions.filter((r) => showTest || r.origin !== "test"
    || r.id === aId || r.id === bId);
  const hiddenTest = sessions.filter((r) => r.origin === "test").length;

  // 參考路徑：兩趟共用同一計畫航線時以它為基準（共同 X 軸的最佳來源）
  useEffect(() => {
    const mid = aSess?.mission_id && aSess.mission_id === bSess?.mission_id
      ? aSess.mission_id : null;
    if (!mid) { setPlan(null); return; }
    fetch(`${API}/api/missions/${mid}/waypoints`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setPlan((d?.waypoints ?? [])
        .filter((w: { lat: number; lon: number }) => w.lat && w.lon)))
      .catch(() => setPlan(null));
  }, [aSess?.mission_id, bSess?.mission_id]);

  const aRows = (aId && tracks[aId]) || [];
  const bRows = (bId && tracks[bId]) || [];
  const ready = aRows.length > 0 && bRows.length > 0;

  const res = useMemo(() =>
    (ready ? compareAlongPath(aRows, bRows, plan) : null),
    [ready, aRows, bRows, plan]);

  const cells = useMemo(() => {
    if (!ready) return [];
    const o = aRows.find((r) => r.lat != null && r.lon != null);
    return o ? deltaCells(aRows, bRows, o) : [];
  }, [ready, aRows, bRows]);

  // 兩趟皆有樣本的里程區間數——0＝無從比較（見下方誠實空態）
  const bothBins = useMemo(() => (res?.chainage ?? [])
    .filter((c) => c.a_sinr != null && c.b_sinr != null).length, [res]);

  // 自動判讀（§6b.3）：依整段中位數；句尾必附依據，使用者可自行反駁
  const verdict = useMemo(() => {
    if (!res) return null;
    const pts = res.chainage.filter((c) => c.a_sinr != null && c.b_sinr != null);
    if (!pts.length) return null;
    const median = (v: number[]) => {
      const s = [...v].sort((x, y) => x - y);
      return s.length ? s[Math.floor(s.length / 2)] : 0;
    };
    const dS = median(pts.map((c) => c.b_sinr! - c.a_sinr!));
    const rp = res.chainage.filter((c) => c.a_rsrp != null && c.b_rsrp != null);
    const dR = rp.length ? median(rp.map((c) => c.b_rsrp! - c.a_rsrp!)) : null;
    const FLAT_R = 2, SIG_S = 1.5, TAIL = 3;   // 判定門檻（dB）：持平／顯著／尾部
    // 語意中性（使用者定案 2026-08-13）：句子只描述**現象**（RSRP 與 SINR
    // 的相對走勢），不宣告成因。機只知道訊號變差、不知道為什麼——
    // 「是不是干擾」是研究者依現場條件判斷的事，系統把推測寫成事實就是
    // 在替使用者下結論。
    // 局部變化分支（§6b 設計師裁定）：整段中位數持平但尾部顯著時，若只說
    // 「無顯著變化」會與同頁摘要表的 Δp5 互相打臉——判讀句的責任不是說出
    // 一個對的結論，是說出一個不與同頁其他證據衝突的結論
    const dP5 = res.summary.b.p5 != null && res.summary.a.p5 != null
      ? res.summary.b.p5 - res.summary.a.p5 : null;
    // **摘要表的 Δ 與判讀句的中位數是不同的統計量**：表上是「各自中位數
    // 之差」median(後)−median(前)，句子裡是「逐段差值的中位數」
    // median(後ᵢ−前ᵢ)。前趟在 700m 驟降、後趟在 1000m 才降時，逐段差值
    // 中位數是 0 而整體中位數差 15.8——兩個數字都對卻互相打臉。
    // 守門條件因此要涵蓋**摘要表任一欄的顯著差異**，不能只看尾部
    const dP50 = res.summary.b.p50 != null && res.summary.a.p50 != null
      ? res.summary.b.p50 - res.summary.a.p50 : null;
    let txt: string;
    if (dR == null) txt = "無 RSRP 對照資料，無法判定變因";
    else if (Math.abs(dR) < FLAT_R && dS < -SIG_S) txt = "RSRP 大致持平而 SINR 下降 → 符合外部雜訊升高的特徵";
    else if (Math.abs(dR) < FLAT_R && dS > SIG_S) txt = "RSRP 大致持平而 SINR 上升 → 符合外部雜訊下降的特徵";
    else if (dR < -FLAT_R && dS < -SIG_S) txt = "RSRP 同步下降 → 變因在訊號強度側（距離、遮蔽或發射端）";
    else if (dR > FLAT_R && dS > SIG_S) txt = "RSRP 同步上升 → 變因在訊號強度側（距離、遮蔽或發射端）";
    else if ((dP5 != null && Math.abs(dP5) >= TAIL)
             || (dP50 != null && Math.abs(dP50) >= TAIL)) {
      // 只要摘要表任一欄顯示顯著差異，判讀句就不得說「無顯著變化」
      const bits: string[] = [];
      if (dP50 != null && Math.abs(dP50) >= TAIL) {
        bits.push(`整體中位數${dP50 > 0 ? "改善" : "惡化"} ${Math.abs(dP50).toFixed(1)} dB`);
      }
      if (dP5 != null && Math.abs(dP5) >= TAIL) {
        bits.push(`最差 5% ${dP5 > 0 ? "改善" : "惡化"} ${Math.abs(dP5).toFixed(1)} dB`);
      }
      txt = `逐段差值中位數持平，但${bits.join("、")}`
        + " → 變化集中在局部區段（見主圖差值帶）";
    } else txt = "無顯著變化";
    return { txt, dS, dR, dP5, dP50 };
  }, [res]);

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
      // 初始中心取機隊；沒有遙測就世界視野——熱區資料到位後 fitBounds
      center: firstFleetPos() ?? [0, 20],
      attributionControl: false, cooperativeGestures: true,
      style: { version: 8, sources: {}, layers: [
        { id: "canvas", type: "background", paint: { "background-color": CANVAS } }] },
    });
    mapRef.current = map;
    map.on("load", () => {
      // 網格錨在資料原點：先建空 source，cells 算出後再填（原本錨死在
      // SITL 舊出生點，機隊搬家後參考線會在別的洲）
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
    //   - `ovRef` 還指著已銷毀的 overlay，推層會推進不存在的東西
    //   - `mapReady` 留在 true 時，新地圖 load 後的 setMapReady(true) 是
    //     no-op（React 不重渲染）→ 推層 effect 再也沒有觸發條件 →
    //     **新 overlay 永遠停在 layers: []，熱區一片空白**
    // 本頁是唯一 deps 非 [] 的地圖（[ready]：容器只在有資料時渲染），
    // 所以只有這裡會重建、也只有這裡會踩到
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
    ovRef.current.setProps({ layers: [
      new PolygonLayer<DeltaCell>({
        id: "delta-cells",
        data: cells,
        getPolygon: (c) => {
          const dLat = grid / 2 / M_LAT, dLon = grid / 2 / mLon(c.lat);
          return [[c.lon - dLon, c.lat - dLat], [c.lon + dLon, c.lat - dLat],
            [c.lon + dLon, c.lat + dLat], [c.lon - dLon, c.lat + dLat]];
        },
        // 兩趟都有樣本才上色；單趟＝無對照（灰框空心，不冒充「沒變化」）
        getFillColor: (c) => (c.delta == null ? [143, 139, 128, 30]
          : [...divergeRGB(c.delta), 205] as [number, number, number, number]),
        getLineColor: (c) => (c.delta == null ? [143, 139, 128, 120] : [0, 0, 0, 0]),
        getLineWidth: 0.6,
        stroked: true, filled: true, pickable: true,
        onHover: (info) => setHover((info.object as DeltaCell) ?? null),
        updateTriggers: { getFillColor: cells.length, getPolygon: cells.length },
      }),
    ] });
    // 首次有格時把視野與網格帶到資料範圍
    if (cells.length && mapRef.current) {
      (mapRef.current.getSource("grid") as maplibregl.GeoJSONSource | undefined)
        ?.setData(groundGrid(cells[0].lat, cells[0].lon));
      const b = new maplibregl.LngLatBounds();
      for (const c of cells) b.extend([c.lon, c.lat]);
      mapRef.current.fitBounds(b, { padding: 40, animate: false, maxZoom: 18 });
    }
  }, [mapReady, cells]);

  async function saveNote(id: string, note: string) {
    await fetch(`${API}/api/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
      body: JSON.stringify({ note }),
    }).catch(() => {});
    setSessions((rows) => rows.map((r) => (r.id === id ? { ...r, note } : r)));
  }

  const capsule = (s: SessRow | null, label: string, color: string,
    onPick: (id: string) => void) => (
    <div className="ab-cap">
      <span className="meta">{label}</span>
      <span className="dot" style={{ background: color }} />
      <select value={s?.id ?? ""} onChange={(e) => onPick(e.target.value)}>
        {listed.map((r) => (
          <option key={r.id} value={r.id}>
            {fmtT(r.started_at)}　{r.drone_name}{r.note ? `　${r.note}` : ""}
            {r.origin === "test" ? "　［測試］" : ""}
          </option>
        ))}
      </select>
      {/* 備註即實驗標籤（沿用 v4：膠囊上直接可編） */}
      {s && (noteEdit === s.id ? (
        <input autoFocus defaultValue={s.note ?? ""} placeholder="實驗標籤"
          onBlur={(e) => { saveNote(s.id, e.target.value); setNoteEdit(null); }}
          onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
      ) : (
        <button className="btn-plain btn-sm" title="編輯備註（實驗標籤）"
          onClick={() => setNoteEdit(s.id)}>✎</button>
      ))}
    </div>
  );

  return (
    <div className="ab-page">
      <div className="ab-head">
        <CompareTabs active="ab" />
        <span className="name">
          路徑：{aSess?.mission_id && aSess.mission_id === bSess?.mission_id
            ? aSess.mission_name ?? "（未命名航線）"
            : "兩趟航線不同"}
        </span>
        {/* 不同航線＝沒有共同里程軸，投影會把不相干的位置壓到同一 X。
            不阻止（研究者可能就是要看），但明說（誠實原則，同樣本數差異） */}
        {aSess && bSess && aSess.mission_id !== bSess.mission_id && (
          <span className="hint-line">
            兩趟不是同一條航線（{aSess.mission_name ?? "無航線"} vs {bSess.mission_name ?? "無航線"}）
            ——里程軸以「前」的軌跡為基準，比較僅供參考
          </span>
        )}
        <span className="spacer" />
        {capsule(aSess, "前", A_COLOR, setAId)}
        {capsule(bSess, "後", B_COLOR, setBId)}
        {/* 隱藏的測試架次如實揭露＋可切換（不能讓使用者以為架次消失了） */}
        {hiddenTest > 0 && (
          <label className="ab-testtoggle" title="測試/驗收觸發的架次（origin=test）">
            <input type="checkbox" checked={showTest}
              onChange={(e) => setShowTest(e.target.checked)} />
            含測試架次（{hiddenTest}）
          </label>
        )}
      </div>

      {!ready && (
        <div className="card">
          <div className="empty">{loadErr ?? "載入兩趟軌跡中…"}</div>
        </div>
      )}

      {/* 兩趟沒有共同里程區間＝沿路徑比較無從談起（實測會發生：一趟飛完
          走廊、另一趟只在起飛點做指令驗收）。不畫空圖假裝有比較，明說 */}
      {res && bothBins === 0 && (
        <div className="card">
          <div className="empty">
            這兩趟沒有共同的里程區間——可能其中一趟未沿此路徑飛行。
            <div className="hint-line" style={{ marginTop: 6 }}>
              前：{res.summary.a.n} 筆樣本、後：{res.summary.b.n} 筆；
              分箱 {res.binM} m，兩趟皆有樣本的區間 0 個。
            </div>
          </div>
        </div>
      )}

      {res && bothBins > 0 && (<>
        <div className="card">
          <h3>沿路徑訊號
            <span className="h3-note">
              {res.binM}m 分箱 · 平滑 {SMOOTH_WIN * res.binM}m 窗（原始淡線並存）
              · 里程 0–{Math.round(res.totalM)}m
              {plan ? "（基準＝計畫航線）" : "（基準＝前一趟軌跡）"}
            </span>
          </h3>
          <ChainChart pts={res.chainage} />
          {verdict && (
            <div className="ab-verdict">
              ⓘ {verdict.txt}
              {/* 判讀必須可反駁：句尾附依據，使用者能自行檢查系統的結論 */}
              <span className="meta">
                {/* 標明統計量：判讀句用「逐段差值」、摘要表用「各自中位數
                    之差」——同名不同義會讓兩個都對的數字看起來互相矛盾 */}
                （依據：SINR 逐段差值中位數 {f1(verdict.dS)} dB
                {verdict.dR != null && `、RSRP 逐段差值中位數 ${f1(verdict.dR)} dB`}
                {verdict.dP50 != null && `、整體 P50 差 ${f1(verdict.dP50)} dB`}
                {verdict.dP5 != null && `、Δp5 ${f1(verdict.dP5)} dB`}；
                持平門檻 ±2 dB、表列顯著門檻 ±3 dB）
              </span>
            </div>
          )}
          <RsrpBand pts={res.chainage} />
        </div>

        <div className="card">
          <h3>摘要<span className="h3-note">p5＝最差 5%（尾部才是斷鏈的來源）</span></h3>
          <table className="table ab-sum">
            <thead><tr><th></th><th className="num">均值</th><th className="num">p50</th>
              <th className="num">p5</th><th className="num">樣本數</th></tr></thead>
            <tbody>
              {([["前", res.summary.a, A_COLOR], ["後", res.summary.b, B_COLOR]] as const)
                .map(([lab, s, c]) => (
                <tr key={lab}>
                  <td><span className="dot" style={{ background: c }} />{lab}</td>
                  <td className="num">{f1(s.mean)}</td>
                  <td className="num">{f1(s.p50)}</td>
                  <td className="num"><b>{f1(s.p5)}</b></td>
                  <td className="num">{s.n.toLocaleString()}</td>
                </tr>
              ))}
              <tr>
                <td>Δ</td>
                <td className="num">{delta(res.summary.b.mean, res.summary.a.mean)}</td>
                <td className="num">{delta(res.summary.b.p50, res.summary.a.p50)}</td>
                <td className="num"><b>{delta(res.summary.b.p5, res.summary.a.p5)}</b></td>
                <td className="num">—</td>
              </tr>
            </tbody>
          </table>
          {/* 誠實：不阻止比較，但不假裝對等 */}
          {res.summary.a.n > 0 && res.summary.b.n > 0
            && Math.max(res.summary.a.n, res.summary.b.n)
               / Math.min(res.summary.a.n, res.summary.b.n) > 3 && (
            <div className="hint-line">樣本數差異大（{res.summary.a.n} vs {res.summary.b.n}），比較僅供參考</div>
          )}
          {bothBins > 0 && bothBins < 3 && (
            <div className="hint-line">
              兩趟僅 {bothBins} 個里程區間有共同樣本——重疊太少，趨勢不足採信
            </div>
          )}
          {(res.dropped.a > 0 || res.dropped.b > 0) && (
            <div className="hint-line">
              偏離基準路徑逾 60 m 而未納入：前 {res.dropped.a}、後 {res.dropped.b} 筆
            </div>
          )}
        </div>

        <div className="card">
          <h3>差值熱區
            <span className="h3-note">後−前（藍＝改善／灰＝無變化／橘＝惡化）· 10m 格</span>
          </h3>
          <div className="ab-map" ref={boxRef} />
          <div className="ab-legend">
            <span className="sw" style={{ background: `rgb(${divergeRGB(-8).join(",")})` }} />惡化
            <span className="sw" style={{ background: "rgb(143,139,128)" }} />無變化
            <span className="sw" style={{ background: `rgb(${divergeRGB(8).join(",")})` }} />改善
            <span className="sw sw-none" />無對照（僅一趟有樣本）
            {hover && (
              <span className="meta">
                　前 {f1(hover.a_sinr)}（{hover.a_n}）· 後 {f1(hover.b_sinr)}（{hover.b_n}）
                {hover.delta != null ? `· Δ ${f1(hover.delta)} dB` : "· 無對照"}
              </span>
            )}
          </div>
        </div>
      </>)}
    </div>
  );
}

const delta = (b: number | null, a: number | null) =>
  b == null || a == null ? "—" : `${b - a >= 0 ? "+" : ""}${(b - a).toFixed(1)}`;

/** 主圖：X＝里程、單一 y 軸（絕不雙軸）；原始淡線＋平滑線；差值帶同兩色 */
function ChainChart({ pts }: { pts: ChainPoint[] }) {
  const W = 1000, H = 220, PAD = 26;
  const xs = pts.map((p) => p.m);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const vals = pts.flatMap((p) => [p.a_sinr, p.b_sinr])
    .filter((v): v is number => v != null);
  if (!vals.length) return null;
  const lo = Math.min(...vals) - 2, hi = Math.max(...vals) + 2;
  const X = (m: number) => ((m - x0) / (x1 - x0 || 1)) * (W - PAD) + PAD;
  const Y = (v: number) => H - 18 - ((v - lo) / (hi - lo || 1)) * (H - 40);
  const smooth = (key: "a_sinr" | "b_sinr") => pts.map((_, i) => {
    const w = pts.slice(Math.max(0, i - SMOOTH_WIN + 1), i + 1)
      .map((p) => p[key]).filter((v): v is number => v != null);
    return w.length ? w.reduce((a, b) => a + b, 0) / w.length : null;
  });
  const line = (v: (number | null)[]) => pts.map((p, i) =>
    (v[i] == null ? null : `${X(p.m)},${Y(v[i]!)}`))
    .filter(Boolean).join(" ");
  const sa = smooth("a_sinr"), sb = smooth("b_sinr");

  return (
    <div className="ab-chart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
        aria-label="沿路徑訊號（前後對照）">
        {/* 差值帶：逐段四邊形，依該段 後−前 的正負取色（不引第三色） */}
        {pts.slice(0, -1).map((p, i) => {
          const q = pts[i + 1];
          if (p.a_sinr == null || p.b_sinr == null
            || q.a_sinr == null || q.b_sinr == null) return null;
          const up = (p.b_sinr - p.a_sinr + q.b_sinr - q.a_sinr) / 2 >= 0;
          return (
            <polygon key={i} opacity={0.18} fill={up ? A_COLOR : B_COLOR}
              points={`${X(p.m)},${Y(p.a_sinr)} ${X(q.m)},${Y(q.a_sinr)} `
                + `${X(q.m)},${Y(q.b_sinr)} ${X(p.m)},${Y(p.b_sinr)}`} />
          );
        })}
        {/* 原始（淡）＋平滑（實）並存——誠實規則禁止只畫平滑線 */}
        <polyline points={line(pts.map((p) => p.a_sinr))} fill="none"
          stroke={A_COLOR} strokeWidth="1" strokeOpacity="0.35"
          vectorEffect="non-scaling-stroke" />
        <polyline points={line(pts.map((p) => p.b_sinr))} fill="none"
          stroke={B_COLOR} strokeWidth="1" strokeOpacity="0.35"
          vectorEffect="non-scaling-stroke" />
        <polyline points={line(sa)} fill="none" stroke={A_COLOR} strokeWidth="2"
          vectorEffect="non-scaling-stroke" />
        <polyline points={line(sb)} fill="none" stroke={B_COLOR} strokeWidth="2"
          vectorEffect="non-scaling-stroke" />
        <text x={4} y={Y(hi - 2) + 4} className="ax">{(hi - 2).toFixed(0)}</text>
        <text x={4} y={Y(lo + 2) + 4} className="ax">{(lo + 2).toFixed(0)}</text>
        <text x={W - 4} y={H - 4} className="ax" textAnchor="end">{Math.round(x1)}m</text>
        <text x={PAD} y={H - 4} className="ax">0m</text>
      </svg>
    </div>
  );
}

/** RSRP 迷你帶（§6b.3）：24px、同 X 軸、只畫 後−前 的差值走勢——不做雙軸 */
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
        <line x1={0} x2={W} y1={H / 2} y2={H / 2} stroke="var(--hairline)" strokeWidth="1"
          vectorEffect="non-scaling-stroke" />
        <polyline fill="none" stroke="var(--ink-2)" strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
          points={pts.map((p, i) => (d[i] == null ? null : `${X(p.m)},${Y(d[i]!)}`))
            .filter(Boolean).join(" ")} />
      </svg>
    </div>
  );
}
