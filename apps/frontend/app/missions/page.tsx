"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { colorFor } from "@/components/droneLayer";
import MissionThumb3D from "@/components/MissionThumb3D";
import { errText, getJson } from "@/lib/fetchJson";
import ConfirmModal from "@/components/ConfirmModal";
import InfoTip from "@/components/InfoTip";
import { type PlanPt, planPath } from "@/lib/geo";
import { parseJsonb } from "@/lib/jsonb";
import { API } from "@/lib/signal";

interface Mission {
  id: string; name: string; source: string | null;
  created_at: string; is_active: boolean; waypoint_count: number;
  //: 預計飛行時間（秒）。**null＝算不出來**，eta_unknown 說明為什麼——
  //: 不給預設速度，因為使用者會拿這個數字去安排電池
  eta_s?: number | null;
  eta_unknown?: string[];
  eta_assumptions?: string[];
  home?: number[] | null;                  // 縮圖要用它補起飛／返航段
  // 037：這份任務是照哪一家自駕儀的語意寫的。null＝檔案沒說（手繪／舊資料）
  firmware_type: number | null; vehicle_type: number | null;
}

/** MAV_AUTOPILOT／MAV_TYPE → 人話。**認不得的值原樣顯示 id**，不寫「未知」——
 * 「未知」會讓「檔案沒說」與「說了但我們沒收錄這個型號」看起來一樣
 * （ui-spec §0.2e 的同一條原則）。 */
const AP_NAMES: Record<number, string> = { 0: "通用", 3: "ArduPilot", 12: "PX4" };
const VT_NAMES: Record<number, string> = {
  1: "定翼", 2: "四旋翼", 10: "地面載具", 12: "潛航器", 13: "六旋翼", 14: "八旋翼",
};
/** 目標機種膠囊。**「檔案沒宣告」要說出口，不能留白**（issues/037 二修）。
 *
 * 第一版的判斷是「沒宣告就不顯示 chip，空白代表沒說」——那是錯的：空白同時也
 * 是「還沒載入」「這版前端不支援這個欄位」「渲染掛了」的樣子。使用者要拿這個
 * 資訊決定「這份航線能不能給這台機飛」，而**沒宣告與宣告了我沒看懂，處置不同**：
 * 前者要人自己確認，後者是我方的顯示問題。分不出來就等於沒講。
 *
 * 這與 §0.2e「不知道≠不行」不衝突——那條說的是不要把「不知道」畫成「不行」，
 * 不是叫我們不要講「不知道」。 */
/** QGC geoFence → 本系統的形狀。只取**含納**（inclusion）的圓與多邊形——
 * 那是「只准在裡面飛」的邊界；排除區是另一回事，一併帶著給後端查。
 * 沒有可用的圍欄回 null（＝這份航線沒宣告，後端會退回系統預設並說出來）。 */
function parseFence(gf: any): Record<string, unknown> | null {
  const incC: unknown[] = [], excC: unknown[] = [];
  const incP: unknown[] = [], excP: unknown[] = [];
  for (const c of gf?.circles ?? []) {
    const ctr = c?.circle?.center, r = c?.circle?.radius;
    if (!Array.isArray(ctr) || ctr.length < 2 || !r) continue;
    (c.inclusion !== false ? incC : excC).push(
      { lat: ctr[0], lon: ctr[1], radius: Number(r) });
  }
  for (const p of gf?.polygons ?? []) {
    const pts = (p?.polygon ?? []).filter((v: unknown) =>
      Array.isArray(v) && v.length >= 2).map((v: number[]) => [v[0], v[1]]);
    if (pts.length < 3) continue;
    (p.inclusion !== false ? incP : excP).push(pts);
  }
  if (!incC.length && !excC.length && !incP.length && !excP.length) return null;
  return { inclusion_circles: incC, exclusion_circles: excC,
           inclusion_polygons: incP, exclusion_polygons: excP };
}

/** 秒 → 人看的長度。**算不出來就說算不出來**，不要顯示「0 分」——
 * 那會被讀成「這條航線很短」而不是「我不知道」。 */
function etaText(m: Mission): string {
  if (m.eta_s == null) return "時間未知";
  const t = Math.round(m.eta_s);
  const mm = Math.floor(t / 60), ss = t % 60;
  return mm ? `約 ${mm} 分 ${String(ss).padStart(2, "0")} 秒` : `約 ${ss} 秒`;
}

function planTarget(m: Mission): { text: string; declared: boolean } {
  const ap = m.firmware_type == null ? null
    : (AP_NAMES[m.firmware_type] ?? `firmware ${m.firmware_type}`);
  const vt = m.vehicle_type == null ? null
    : (VT_NAMES[m.vehicle_type] ?? `type ${m.vehicle_type}`);
  const parts = [ap, vt].filter(Boolean);
  return parts.length
    ? { text: parts.join(" · "), declared: true }
    : { text: "未宣告目標機種", declared: false };
}
interface Sess {
  id: string; drone_id: string; drone_name: string; mission_id: string | null;
  started_at: string; ended_at: string | null;
  summary: { samples_total?: number; min_sinr?: number | null; avg_sinr?: number | null } | null;
}

/** 解析 QGC .plan：**全部 SimpleItem 保留**（含 DO_* 設定類與 RTL），
 * 原始 command/frame/p1–p4 一併存——上傳到機時原樣送出，跟 QGC 上傳
 * 同一份任務（保真度對齊實戰工具 upload_mission.py）。
 * DO_* 無座標以 0 表示（去衝突檢查與地圖疊圖都會略過 0 座標）。 */
const NAV_CMDS = new Set([16, 17, 18, 19, 20, 21, 22]);
interface PlanWp {
  seq: number; lat: number; lon: number; alt: number | null; action: string;
  command: number; frame: number | null;
  p1: number | null; p2: number | null; p3: number | null; p4: number | null;
}
/** `.plan` 自報的目標機種。QGC 用的是 MAV_AUTOPILOT／MAV_TYPE 這兩個 enum，
 * **與機端 HEARTBEAT 同源**，所以存下來就能在上傳前比對（issues/037）。 */
interface ParsedPlan {
  wps: PlanWp[];
  firmware_type: number | null;
  vehicle_type: number | null;
  fence: Record<string, unknown> | null;   // .plan 自帶的 geoFence
  home: number[] | null;                   // plannedHomePosition [lat, lon, alt]
  cruise_speed: number | null;
  hover_speed: number | null;
  rally: number[][] | null;                // rallyPoints：緊急備降點
}
function parsePlan(text: string): ParsedPlan {
  const j = JSON.parse(text);
  if (j?.fileType !== "Plan") throw new Error("not a plan");
  const items = j?.mission?.items ?? [];
  const out: PlanWp[] = [];
  for (const it of items) {
    if (it.type !== "SimpleItem") continue;   // 複雜項（測繪格網等）暫不支援
    const [p1, p2, p3, p4, lat, lon, alt] = it.params ?? [];
    out.push({
      seq: out.length,
      lat: lat ?? 0, lon: lon ?? 0,
      alt: it.Altitude ?? alt ?? null,
      action: it.command === 22 ? "takeoff" : it.command === 21 ? "land"
        : it.command === 20 ? "rtl" : NAV_CMDS.has(it.command) ? "waypoint" : "do",
      command: it.command, frame: it.frame ?? null,
      p1: p1 ?? null, p2: p2 ?? null, p3: p3 ?? null, p4: p4 ?? null,
    });
  }
  return {
    wps: out,
    firmware_type: j?.mission?.firmwareType ?? null,
    vehicle_type: j?.mission?.vehicleType ?? null,
    // **圍欄跟著航線走**：QGC 的 .plan 本來就帶 geoFence，讀它就好。
    // 系統預設值是「這套系統只在一個場地飛」才成立的假設，而測繪任務與
    // 定點巡檢的合理範圍可以差一個數量級
    fence: parseFence(j?.geoFence),
    // **RTL 沒有座標**——它的意思是「回到 home」。少了這個點，返航那一段
    // 在畫面上畫不出來，使用者會以為航線在最後一個航點就結束了
    home: Array.isArray(j?.mission?.plannedHomePosition)
      ? j.mission.plannedHomePosition : null,
    // 速度：估預計時間用。**沒宣告就不估**，不給預設值
    cruise_speed: j?.mission?.cruiseSpeed ?? null,
    hover_speed: j?.mission?.hoverSpeed ?? null,
    // 備降點：QGC 畫得出來、我們畫不出來，兩邊的圖就不一樣
    rally: Array.isArray(j?.rallyPoints?.points) && j.rallyPoints.points.length
      ? j.rallyPoints.points : null,
  };
}

/** 高度語意：MAV_FRAME 躺在每個航點上。**3＝離起飛點、10＝離地面**
 * （地形跟隨）——同一個「4.6 m」在兩者是不同的地方，所以它必須出現在列上，
 * 不能藏在展開裡（ui-spec §4.6）。
 *
 * 只看導航航點：起飛項與 DO_* 的 frame 是另一回事（takeoff 恆為 3、
 * DO_CHANGE_SPEED 是 2），把它們算進去會讓每一份航線都變成「混用」。
 * **認不得就說認不得**，不猜。 */
function frameText(frames: number[] | undefined):
  { text: string; ok: boolean; terrain: boolean } {
  if (!frames) return { text: "高度語意載入中", ok: false, terrain: false };
  if (!frames.length) return { text: "高度語意未知", ok: false, terrain: false };
  if (frames.length > 1) {
    return { text: `高度混用 frame ${frames.join("/")}`, ok: false, terrain: false };
  }
  if (frames[0] === 10) return { text: "高度＝離地面", ok: true, terrain: true };
  if (frames[0] === 3) return { text: "高度＝離起飛點", ok: true, terrain: false };
  return { text: `高度 frame ${frames[0]}`, ok: false, terrain: false };
}

const fmtT = (t: string) =>
  new Date(t).toLocaleString("zh-TW", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false });

interface WpRow extends PlanPt { frame?: number | null }

export default function Missions() {
  const router = useRouter();
  const [missions, setMissions] = useState<Mission[]>([]);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  // 幾何預檢報告在這一頁**不顯示**（2026-09-07 使用者指示）。
  // `GET /api/missions/{id}/check` 與上傳回應裡的 `check` 都還在，
  // **真正的守門也還在**：指令服務上傳到機上之前會自己檢查，沒過就是
  // `rejected_precheck`，理由會出現在任務控制面板上。這一頁拿掉的是
  // 「還沒要飛之前先讀一遍報告」那一層，不是安全網本身。
  const [menuId, setMenuId] = useState<string | null>(null);
  const [toDelete, setToDelete] = useState<Mission | null>(null);
  const [thumbs, setThumbs] = useState<Record<string, PlanPt[]>>({});
  const [frames, setFrames] = useState<Record<string, number[]>>({});
  const [sort, setSort] = useState<"used" | "new" | "name">("used");
  const [q, setQ] = useState("");
  const [hot, setHot] = useState(false);       // 拖放中
  const fileRef = useRef<HTMLInputElement>(null);

  // 縮圖與高度語意：每條路線抓一次 waypoints（路線數少，逐條抓可接受）
  useEffect(() => {
    for (const m of missions) {
      if (thumbs[m.id]) continue;
      // 縮圖取不到＝該列無縮圖（顯性缺口，不會假裝沒事），沿用靜默 catch
      getJson<{ waypoints?: WpRow[] }>(`${API}/api/missions/${m.id}/waypoints`)
        .then((d) => {
          const wps = d.waypoints ?? [];
          // **縮圖與三個地圖頁走同一支 planPath**（2026-09-07）：起飛爬升段、
          // 降落段、缺值高度都在那裡補齊，這裡不留第二份實作——同一份任務
          // 在四個畫面上必須是同一個形狀
          setThumbs((t) => ({ ...t, [m.id]: planPath(wps, m.home) }));
          setFrames((f) => ({ ...f, [m.id]: [...new Set(
            wps.filter((w) => w.action === "waypoint" && w.frame != null)
              .map((w) => w.frame as number))].sort((a, b) => a - b) }));
        })
        .catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missions]);

  const reload = useCallback(() => {
    // 見 lib/fetchJson.ts：取不到不得變成「沒有路徑／沒有航線」
    getJson<Mission[]>(`${API}/api/missions`).then(setMissions)
      .catch(() => setErr("無法取得路徑清單"));
    getJson<any[]>(`${API}/api/sessions?limit=200`)
      // 逐列解析：一筆 summary 壞掉不得讓整份架次清單消失（見 lib/jsonb.ts）
      .then((rows) => setSessions(rows.map((r: any) => {
        const v = parseJsonb(r.summary);
        return { ...r, summary: v.ok ? v.value : null };
      })))
      .catch(() => setErr("無法取得航線清單"));
  }, []);
  useEffect(reload, [reload]);

  // 選單開著時點別處就收起來
  useEffect(() => {
    if (!menuId) return;
    const off = () => setMenuId(null);
    window.addEventListener("click", off);
    return () => window.removeEventListener("click", off);
  }, [menuId]);

  async function call(path: string, init?: RequestInit) {
    setErr(null); setBusy(true);
    try {
      const res = await fetch(`${API}${path}`, init);
      const body = await res.json().catch(() => null);
      if (!res.ok) setErr(errText(body?.detail, `失敗（${res.status}）`));
      else reload();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function uploadPlan(f: File) {
    setErr(null);
    let parsed;
    try {
      parsed = parsePlan(await f.text());
    } catch {
      setErr("不是有效的 QGC .plan 檔"); return;
    }
    const wps = parsed.wps;
    const navCount = wps.filter((w) => NAV_CMDS.has(w.command) && w.lat && w.lon).length;
    if (navCount < 2) { setErr("檔案內找不到足夠的導航航點"); return; }
    setBusy(true);
    try {
      const res = await fetch(`${API}/api/missions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: f.name.replace(/\.plan$/i, ""), source: "plan-file", waypoints: wps,
          // 機種一起送：航點的 frame 與 params 是照哪一家的語意寫的，
          // 只有這兩個欄位說得出來（issues/037）
          firmware_type: parsed.firmware_type, vehicle_type: parsed.vehicle_type,
          fence: parsed.fence, home: parsed.home,
          cruise_speed: parsed.cruise_speed, hover_speed: parsed.hover_speed,
          rally: parsed.rally,
        }),
      });
      const body = await res.json();
      if (!res.ok) setErr(errText(body.detail, `失敗（${res.status}）`));
      else reload();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const usesOf = (id: string) => sessions
    .filter((s) => s.mission_id === id)
    .sort((a, b) => (a.started_at < b.started_at ? 1 : -1));

  // 排序與搜尋**只在多到會找不到的時候才出現**：兩三份航線時它們只是雜訊
  const many = missions.length > 6;
  const shown = (() => {
    const needle = q.trim().toLowerCase();
    const list = missions.filter((m) => !needle || m.name.toLowerCase().includes(needle));
    const last = (m: Mission) => usesOf(m.id)[0]?.started_at ?? "";
    if (!many) return list;
    if (sort === "new") return [...list].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    if (sort === "name") return [...list].sort((a, b) => a.name.localeCompare(b.name));
    return [...list].sort((a, b) =>
      (usesOf(b.id).length - usesOf(a.id).length) || (last(b) < last(a) ? -1 : 1));
  })();

  return (
    <div className="page-pad mission-page"
      onDragOver={(e) => { e.preventDefault(); setHot(true); }}
      onDragLeave={() => setHot(false)}
      onDrop={(e) => {
        e.preventDefault(); setHot(false);
        const f = e.dataTransfer.files?.[0];
        if (f) uploadPlan(f);
      }}>
      <div className="drone-head">
        <span className="name">路徑{missions.length ? `（${missions.length}）` : ""}</span>
        <button className="btn-plain btn-sm" disabled={busy}
          onClick={() => fileRef.current?.click()}>＋ 上傳 .plan</button>
        <span className="spacer" />
        <InfoTip tip={"這裡是存下來的 QGC 航線。一列一份：縮圖是它的立體形狀"
          + "（拖曳可以轉動視角、往下拖壓低視角看高低差），旁邊是航點數、"
          + "預計時間、目標機種與高度語意。「顯示中」的那份會畫在即時頁的地圖上。"
          + "點一列展開誰飛過它。預計時間是估計值——不含風、不含加減速、"
          + "不含起飛前的解鎖與檢查。"} />
      </div>

      {err && <div className="form-err">{err}</div>}

      {many && (
        <div className="mtools">
          <span className="hint-line">排序</span>
          {([["used", "最常用"], ["new", "最新"], ["name", "名稱"]] as const)
            .map(([k, l]) => (
              <button key={k} className={`pill${sort === k ? " on" : ""}`}
                onClick={() => setSort(k)}>{l}</button>
            ))}
          <input className="msearch" placeholder="搜尋名稱" value={q}
            onChange={(e) => setQ(e.target.value)} />
        </div>
      )}

      {missions.length === 0 && !err && (
        <div className="card"><div className="empty">
          還沒有存下任何航線——用上面的「＋ 上傳 .plan」加一份。
        </div></div>
      )}
      {missions.length > 0 && shown.length === 0 && (
        <div className="card"><div className="empty">沒有符合的名稱。</div></div>
      )}

      {shown.map((m) => {
        const uses = usesOf(m.id);
        const fr = frameText(frames[m.id]);
        const tg = planTarget(m);
        const open = openId === m.id;
        const toggle = () => { setOpenId(open ? null : m.id); setMenuId(null); };
        return (
          <div className="card mitem" key={m.id}>
            <div className="mrow" role="button" tabIndex={0} onClick={toggle}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
              }}>
              <div className="mthumb-box">
                <MissionThumb3D wps={thumbs[m.id]} onTap={toggle} />
              </div>
              <div className="mmain">
                <div className="mtitle">
                  {/* **名字完整顯示**：檔名正是使用者用來認這份航線的東西，
                      舊版卡片寬 195px 只看得到半截（ui-spec §4.6） */}
                  <span className="mname">{m.name}</span>
                  {m.is_active && (
                    // 「顯示中」是狀態不是按鈕：**accent 只准互動 chrome**，
                    // 所以用中性 chip ＋ 一個點，不把整張卡圈成珊瑚色
                    <span className="chip live-chip">
                      <span className="dot" style={{ background: "var(--status-ok)" }} />
                      顯示中
                    </span>
                  )}
                  {fr.terrain && <span className="chip warn-chip">地形跟隨</span>}
                </div>
                <div className="mmeta">
                  <span>{m.waypoint_count} 航點</span>
                  <span className="msep">·</span>
                  {/* 預計時間：**估計值，不是承諾**——tooltip 攤開估了什麼、
                      沒估什麼。算不出來時說「時間未知」而不是顯示 0 分 */}
                  <span style={m.eta_s == null ? { opacity: 0.6 } : undefined}
                    title={m.eta_s == null
                      ? (m.eta_unknown ?? []).join("；") || "算不出預計時間"
                      : "預計飛行時間（估計值）：\n"
                        + (m.eta_assumptions ?? []).map((a) => "· " + a).join("\n")}>
                    {etaText(m)}
                  </span>
                  <span className="msep">·</span>
                  <span style={tg.declared ? undefined : { opacity: 0.6 }}
                    title={tg.declared
                      // **把原始 enum 一起講出來**：2026-08-26 使用者回報
                      // 「傳上來都變 PX4」，而資料庫裡是 3（ArduPilot）
                      ? `這份航線宣告的目標機種（來自 .plan：firmwareType=${m.firmware_type ?? "—"}、vehicleType=${m.vehicle_type ?? "—"}）`
                      : "這份 .plan 沒有寫 firmwareType／vehicleType——"
                        + "系統無法替你確認它適不適合這台機，請自己確認"}>
                    {tg.text}
                  </span>
                  <span className="msep">·</span>
                  <span className={fr.terrain ? "mframe-terr" : undefined}
                    style={fr.ok ? undefined : { opacity: 0.6 }}
                    title={fr.terrain
                      ? "航點高度是「離地面多少」（MAV_FRAME 10），由飛控用自己的地形圖庫跟著地面飛"
                      : "航點高度是「離起飛點多少」（MAV_FRAME 3）"}>
                    {fr.text}
                  </span>
                </div>
              </div>
              <div className="muse">
                {uses.length ? (<>
                  <div>飛過 {uses.length} 次</div>
                  <div className="hint-line">最近 {fmtT(uses[0].started_at)}</div>
                </>) : <div className="hint-line">還沒飛過</div>}
              </div>
              <span className="caret">{open ? "▾" : "▸"}</span>
              <button className="btn-plain btn-sm" title="更多"
                onClick={(e) => {
                  e.stopPropagation();
                  setMenuId(menuId === m.id ? null : m.id);
                }}>⋯</button>
            </div>

            {menuId === m.id && (
              <div className="mcard-menu" onClick={(e) => e.stopPropagation()}>
                {/* 手動顯示切換（降級保留——常規路徑是任務開始自動浮現） */}
                <button className="btn-plain btn-sm" disabled={busy}
                  onClick={() => {
                    setMenuId(null);
                    call(`/api/missions/${m.id}/activate?active=${!m.is_active}`,
                         { method: "POST" });
                  }}>
                  {m.is_active ? "從即時頁隱藏" : "顯示於即時頁"}
                </button>
                {/* 規劃子頁（issues/048）。**放在這一頁底下，不另開頂層頁**
                    ——使用者 2026-09-08：「管理本身包含規劃」 */}
                <a className="btn-plain btn-sm" href={`/missions/${m.id}/plan`}
                  title="剖面圖（地面高程 vs 規劃高度）與逐段的離地／速度"
                  onClick={() => setMenuId(null)}>離地與速度</a>
                {/* 地形跟隨（issues/047 §1-A）：**存成新的一份**，不就地改寫。
                    改寫之後高度的意思從「離起飛點」變成「離地面」——那是另一
                    份航線，該讓人先看到縮圖再決定要不要飛 */}
                <button className="btn-plain btn-sm"
                  disabled={busy || fr.terrain}
                  title={fr.terrain ? "這份已經是地形跟隨了"
                    : "把每個航點的高度改寫成「離地面多少」（frame 10），"
                      + "由飛控用自己的地形圖庫跟著地面飛。\n"
                      + "起飛、降落、返航不改。查不到地形高程就整份不改。\n"
                      + "會另存一份，原本這份不動。"}
                  onClick={() => {
                    setMenuId(null);
                    call(`/api/missions/${m.id}/terrain-frame`, { method: "POST" });
                  }}>
                  改成地形跟隨
                </button>
                <button className="btn-danger btn-sm" disabled={busy}
                  onClick={() => { setMenuId(null); setToDelete(m); }}>刪除</button>
              </div>
            )}

            {open && (
              <div className="mwork">
                <div className="drone-head">
                  <span className="name">使用紀錄</span>
                  {/* 哪幾台無人機用過（識別色點＋名） */}
                  {[...new Map(uses.map((s) => [s.drone_id, s.drone_name])).entries()]
                    .map(([did, name]) => (
                      <span className="chip" key={did}>
                        <span className="dot" style={{ background: colorFor(did) }} />
                        {name}
                      </span>
                    ))}
                  <span className="spacer" />
                  {uses.length > 1 && (
                    <button className="btn-plain btn-sm"
                      onClick={() => router.push(`/replay-mission/${m.id}`)}>
                      比對回放（{uses.length} 條疊圖）
                    </button>
                  )}
                </div>
                {uses.length === 0 ? (
                  // **「沒有人飛過」是一句話，不是一張空表**：畫五個欄位標題
                  // 再說沒有資料，讀的人要先掃過一遍表頭才知道那裡什麼都沒有
                  <div className="empty">
                    還沒有航次飛過這份航線。設為「顯示於即時頁」後起飛就會自動關聯。
                  </div>
                ) : (
                  <table className="table">
                    <thead>
                      <tr><th>無人機</th><th>開始</th><th className="num">樣本數</th>
                          <th className="num">平均 SINR</th><th className="num">最低 SINR</th></tr>
                    </thead>
                    <tbody>
                      {uses.map((s) => (
                        <tr key={s.id} className="row-link" title="回放這條航線"
                            onClick={() => router.push(`/replay/${s.id}`)}>
                          <td>
                            <span className="dot" style={{ background: colorFor(s.drone_id),
                              display: "inline-block", marginRight: 6 }} />
                            {s.drone_name}
                          </td>
                          <td>{fmtT(s.started_at)}</td>
                          <td className="num">{s.summary?.samples_total ?? "—"}</td>
                          <td className="num">{s.summary?.avg_sinr?.toFixed(1) ?? "—"} dB</td>
                          <td className="num">{s.summary?.min_sinr?.toFixed(1) ?? "—"} dB</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        );
      })}

      <div className={`mdrop${hot ? " hot" : ""}`}>或把 .plan 檔拖到這裡</div>
      <input ref={fileRef} type="file" accept=".plan,application/json" hidden
        onChange={(e) => {
          const f = e.target.files?.[0]; if (f) uploadPlan(f); e.target.value = "";
        }} />
      {/* 「從機上讀回」按鈕移除（2026-09-07 使用者指示）。
          `POST /api/missions/from-vehicle` 還在，rig 與 curl 叫得到——
          拿掉的是這一頁的入口 */}

      {toDelete && (
        <ConfirmModal title={`刪除「${toDelete.name}」？`} confirmLabel="刪除航線"
          onConfirm={() => {
            const id = toDelete.id;
            setToDelete(null);
            call(`/api/missions/${id}`, { method: "DELETE" });
          }}
          onClose={() => setToDelete(null)}>
          <div>這份航線會從清單消失。</div>
          <div>{usesOf(toDelete.id).length
            ? `已經飛過的 ${usesOf(toDelete.id).length} 筆航次紀錄留著。`
            : "它還沒有任何航次紀錄。"}</div>
        </ConfirmModal>
      )}
    </div>
  );
}
