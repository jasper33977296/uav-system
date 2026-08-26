"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { colorFor } from "@/components/droneLayer";
import MissionThumb3D from "@/components/MissionThumb3D";
import { errText, getJson } from "@/lib/fetchJson";
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
interface PlanCheck {
  ok: boolean; problems: string[]; warnings: string[];
  max_dist_m: number; max_alt_m?: number;
  //: 圍欄從哪來。plan＝這份航線自己宣告的（超出就是真的超出）；
  //: none＝**這份沒宣告，系統也不替它設一個**——一個全域數字只對一個場地
  //: 成立，拿它去判會產生看起來很具體的假錯誤
  fence_source?: "plan" | "none";
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

export default function Missions() {
  const router = useRouter();
  const [missions, setMissions] = useState<Mission[]>([]);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  // **每次點開一份航線就重檢一次**，不是只在匯入的那一刻檢查一次。
  // 匯入時看到的報告會隨畫面關掉就消失，而使用者是在**要飛之前**才需要它；
  // 而且圍欄的系統預設值可能在匯入之後被改過，那時舊報告就是過期的。
  // **報告只有一個顯示點**（頁面最上方）。上傳完的報告與點開卡片的報告
  // 各自顯示一次時，同一份航線的同一句話會在畫面上出現兩遍——重複的訊息
  // 會讓人開始略過它們，而這是唯一會說「這份不能飛」的地方
  const [report, setReport] =
    useState<PlanCheck | "loading" | "failed" | null>(null);
  const [reportOf, setReportOf] = useState<string | null>(null);   // 這份報告在講誰
  useEffect(() => {
    if (!openId) return;
    let dead = false;
    setReport("loading"); setReportOf(openId);
    getJson<PlanCheck>(`${API}/api/missions/${openId}/check`)
      .then((c) => { if (!dead) setReport(c); })
      // 取不到就說取不到——**空白會被讀成「檢查過了、沒問題」**，
      // 而那是這份報告最不能給錯的方向
      .catch(() => { if (!dead) setReport("failed"); });
    return () => { dead = true; };
  }, [openId]);
  const [menuId, setMenuId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const delTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [thumbs, setThumbs] = useState<Record<string, { lat: number; lon: number }[]>>({});
  const fileRef = useRef<HTMLInputElement>(null);

  // 縮圖資料：每條路線抓一次 waypoints（路線數少，逐條抓可接受）
  useEffect(() => {
    for (const m of missions) {
      if (thumbs[m.id]) continue;
      // 縮圖取不到＝該卡無縮圖（顯性缺口，不會假裝沒事），沿用靜默 catch
      getJson<{ waypoints?: any[] }>(`${API}/api/missions/${m.id}/waypoints`)
        .then((d) => {
          // 縮圖也要含起飛與返航段：那兩項在 .plan 裡沒有座標（意思是
          // 「從 home 起飛」「回 home」），照 lat/lon 過濾會讓縮圖從第一個
          // 航點畫起——與 QGC 的圖形狀不同（2026-08-26 使用者回報）
          const all = d.waypoints ?? [];
          const h = m.home;
          const pts = all.filter((w: any) => w.lat && w.lon);
          if (!Array.isArray(h) || h.length < 2 || !(h[0] || h[1]))
            return setThumbs((t) => ({ ...t, [m.id]: pts }));
          const first = all.find((w: any) => w.action !== "do");
          const out = [...pts];
          if (first?.action === "takeoff" && !(first.lat || first.lon))
            out.unshift({ lat: h[0], lon: h[1] });
          if (all.some((w: any) => w.action === "rtl" || w.action === "land"))
            out.push({ lat: h[0], lon: h[1] });
          setThumbs((t) => ({ ...t, [m.id]: out }));
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

  async function call(path: string, init?: RequestInit, showCheck = false) {
    setErr(null); setBusy(true);
    if (showCheck) setReport(null);
    try {
      const res = await fetch(`${API}${path}`, init);
      const body = await res.json().catch(() => null);
      if (!res.ok) setErr(errText(body?.detail, `失敗（${res.status}）`));
      else {
        // 讀回的任務同樣要出預檢報告：機上那份可能違反現行圍欄/高度上限，
        // 回應裡帶了 check 卻不顯示＝把已知問題藏起來（上傳 .plan 有顯示，
        // 讀回沒有＝同一種資料兩套待遇）
        if (showCheck) setReport(body?.check ?? null);
        reload();
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }


  async function uploadPlan(f: File) {
    setErr(null); setReport(null);
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
      else { setReport(body.check ?? null); reload(); }
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page-pad">
      {err && <div className="form-err">{err}</div>}
      {report === "loading" && <div className="plan-report">檢查中⋯</div>}
      {report === "failed" && (
        <div className="plan-report">
          <div className="bad">
            取不到這份航線的預檢結果——**不是「沒問題」**，是沒檢查到
          </div>
        </div>
      )}
      {report && report !== "loading" && report !== "failed" && (
        <div className="plan-report">
          {/* **報告要指名在講誰**：它是點卡片換內容的，不寫清楚就會出現
              「看著 A 的卡片、讀著 B 的報告」 */}
          {reportOf && (
            <div className="hint-line">
              「{missions.find((m) => m.id === reportOf)?.name ?? "—"}」的幾何預檢
            </div>
          )}
          {report.ok && report.warnings.length === 0 && (
            <div className="ok">✅ 幾何預檢通過（最遠航點 {report.max_dist_m} m
              {report.max_alt_m != null && `／最高 ${report.max_alt_m} m`}
              {report.fence_source === "plan" && "／圍欄用這份航線自帶的"}）
            </div>
          )}
          {report.problems.map((p, i) => (
            <div className="bad" key={`p${i}`}>❌ {p}</div>
          ))}
          {report.warnings.map((w, i) => (
            <div className="warn" key={`w${i}`}>⚠️ {w}</div>
          ))}
        </div>
      )}

      {/* 縮圖卡格（§4 v3）：點卡＝展開使用紀錄；「顯示於即時頁」改由
          任務開始自動 activate，手動切換降級收 ⋯ */}
      <div className="mission-grid">
        {missions.map((m) => {
          const count = sessions.filter((s) => s.mission_id === m.id).length;
          return (
            <div key={m.id}
              className={`mcard ${m.is_active ? "on" : ""}`
                + ` ${openId === m.id ? "expanded" : ""}`}
              title="點擊展開使用紀錄"
              onClick={() => setOpenId(openId === m.id ? null : m.id)}>
              <MissionThumb3D wps={thumbs[m.id]}
                onTap={() => setOpenId(openId === m.id ? null : m.id)} />
              {/* **膠囊自己一行**：卡片只有 ~195px 寬，膠囊跟名字擠同一列時
                  名字被壓到 68px（實測：完整 159px），使用者看到的是半截檔名
                  ——而檔名正是他用來認這份航線的東西。膠囊縮不得（縮了就讀不
                  出是給哪家飛控的），所以讓出整列的只能是版面，不是內容。 */}
              <div className="mcard-foot">
                <span className="mcard-name">{m.name}</span>
                <span className="spacer" />
                <button className="btn-plain btn-sm" title="更多"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuId(menuId === m.id ? null : m.id);
                    setDeleting(null);
                  }}>⋯</button>
              </div>
              <div className="mcard-chips">
                {m.is_active && <span className="chip on-chip">顯示中</span>}
                {/* 預計時間：**估計值，不是承諾**——tooltip 攤開估了什麼、
                    沒估什麼（不含風、不含加減速）。算不出來時說「時間未知」
                    而不是顯示 0 分，後者會被讀成「這條航線很短」 */}
                <span className="chip"
                  style={m.eta_s == null ? { opacity: 0.5 } : undefined}
                  title={m.eta_s == null
                    ? (m.eta_unknown ?? []).join("；") || "算不出預計時間"
                    : "預計飛行時間（估計值）：\n"
                      + (m.eta_assumptions ?? []).map((a) => "· " + a).join("\n")}>
                  {etaText(m)}
                </span>
                  {/* 目標機種：選檔當下就看得到這份航線是給誰寫的。
                      沒宣告時**照樣顯示一顆弱化的膠囊**說「未宣告」——留白會被
                      讀成「還沒載入」或「這版沒這功能」（issues/037 二修） */}
                  {(() => {
                    const t = planTarget(m);
                    return (
                      <span className="chip"
                        style={t.declared ? undefined : { opacity: 0.5 }}
                        title={t.declared
                          // **把原始 enum 一起講出來**：2026-08-26 使用者回報
                          // 「傳上來都變 PX4」，而資料庫裡是 3（ArduPilot）。
                          // 只顯示翻譯後的名字時，要查「畫面說的」與「檔案寫的」
                          // 是不是同一件事，得繞到資料庫——那不該是使用者的工作
                          ? `這份航線宣告的目標機種（來自 .plan：firmwareType=${m.firmware_type ?? "—"}、vehicleType=${m.vehicle_type ?? "—"}）`
                          : "這份 .plan 沒有寫 firmwareType／vehicleType——"
                            + "系統無法替你確認它適不適合這台機，請自己確認"}>
                        {t.text}
                      </span>
                    );
                  })()}
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
                  {/* 刪除兩段式：卡上變紅「確定刪除？」（ui-spec §4.3） */}
                  <button className="btn-danger btn-sm" disabled={busy}
                    onClick={() => {
                      if (deleting === m.id) {
                        setDeleting(null); setMenuId(null);
                        call(`/api/missions/${m.id}`, { method: "DELETE" });
                      } else {
                        setDeleting(m.id);
                        if (delTimer.current) clearTimeout(delTimer.current);
                        delTimer.current = setTimeout(() => setDeleting(null), 3500);
                      }
                    }}>
                    {deleting === m.id ? "確定刪除？" : "刪除"}
                  </button>
                </div>
              )}
            </div>
          );
        })}
        <button className="mcard mcard-add" disabled={busy}
          onClick={() => fileRef.current?.click()}>
          <span className="mcard-plus">＋</span>上傳 .plan
        </button>
        <input ref={fileRef} type="file" accept=".plan,application/json" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadPlan(f); e.target.value = ""; }} />
      </div>
      {/* 技術操作：從機上讀回（全域動作，不屬於任一卡） */}
      <div>
        <button className="btn-plain btn-sm" disabled={busy}
          onClick={() => call("/api/missions/from-vehicle", { method: "POST" }, true)}>
          從機上讀回
        </button>
      </div>

      {missions.length === 0 && (
        <div className="card">
          <div className="empty">尚無儲存的路線——用「＋」上傳 .plan 檔</div>
        </div>
      )}

      {openId && <div className="card">
        {(() => {
          const mine = sessions.filter((s) => s.mission_id === openId);
          const m = missions.find((x) => x.id === openId);
          return (
            <div style={{ marginTop: 12 }}>
              {/* 預檢報告在**頁面最上方**，不在這裡——同一份航線的同一句話
                  出現兩遍，會讓人開始略過它們，而那是唯一會說「這份不能飛」
                  的地方（2026-08-26 使用者回報重複） */}
              <div className="drone-head">
                <span className="meta">「{m?.name}」被使用 {mine.length} 次</span>
                {/* 哪幾台無人機用過（識別色點＋名） */}
                {[...new Map(mine.map((s) => [s.drone_id, s.drone_name])).entries()]
                  .map(([did, name]) => (
                    <span className="chip" key={did}>
                      <span className="dot" style={{ background: colorFor(did) }} />
                      {name}
                    </span>
                  ))}
                <span className="spacer" />
                {mine.length > 1 && (
                  <button className="btn-plain btn-sm"
                    onClick={() => router.push(`/replay-mission/${openId}`)}>
                    比對回放（{mine.length} 條疊圖）
                  </button>
                )}
              </div>
              <table className="table" style={{ marginTop: 6 }}>
                <thead>
                  <tr><th>無人機</th><th>開始時間</th><th className="num">樣本數</th>
                      <th className="num">平均 SINR</th><th className="num">最低 SINR</th></tr>
                </thead>
                <tbody>
                  {mine.length === 0 && (
                    <tr><td colSpan={5} className="empty">
                      尚無航線飛過此路徑——設為「顯示於即時頁」後起飛即自動關聯</td></tr>
                  )}
                  {mine.map((s) => (
                    <tr key={s.id} className="row-link" title="回放這條航線"
                        onClick={() => router.push(`/replay/${s.id}`)}>
                      <td>
                        <span className="dot" style={{ background: colorFor(s.drone_id),
                          display: "inline-block", marginRight: 6 }} />
                        {s.drone_name}
                      </td>
                      <td>{new Date(s.started_at).toLocaleString("zh-TW", { hour12: false })}</td>
                      <td className="num">{s.summary?.samples_total ?? "—"}</td>
                      <td className="num">{s.summary?.avg_sinr?.toFixed(1) ?? "—"} dB</td>
                      <td className="num">{s.summary?.min_sinr?.toFixed(1) ?? "—"} dB</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        })()}
      </div>}
    </div>
  );
}
