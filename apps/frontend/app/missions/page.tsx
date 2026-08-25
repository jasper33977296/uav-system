"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { colorFor } from "@/components/droneLayer";
import MissionThumb3D from "@/components/MissionThumb3D";
import { getJson } from "@/lib/fetchJson";
import { parseJsonb } from "@/lib/jsonb";
import { API } from "@/lib/signal";

interface Mission {
  id: string; name: string; source: string | null;
  created_at: string; is_active: boolean; waypoint_count: number;
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
function planTarget(m: Mission): string | null {
  if (m.firmware_type == null && m.vehicle_type == null) return null;
  const ap = m.firmware_type == null ? null
    : (AP_NAMES[m.firmware_type] ?? `firmware ${m.firmware_type}`);
  const vt = m.vehicle_type == null ? null
    : (VT_NAMES[m.vehicle_type] ?? `type ${m.vehicle_type}`);
  return [ap, vt].filter(Boolean).join(" · ");
}
interface PlanCheck {
  ok: boolean; problems: string[]; warnings: string[];
  max_dist_m: number; fence_r: number; fence_alt: number;
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
  };
}

export default function Missions() {
  const router = useRouter();
  const [missions, setMissions] = useState<Mission[]>([]);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
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
        .then((d) => setThumbs((t) => ({ ...t, [m.id]: d.waypoints ?? [] })))
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
      if (!res.ok) setErr(body?.detail ?? `失敗（${res.status}）`);
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

  const [report, setReport] = useState<PlanCheck | null>(null);

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
        }),
      });
      const body = await res.json();
      if (!res.ok) setErr(body.detail ?? `失敗（${res.status}）`);
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
      {report && (
        <div className="plan-report">
          {report.ok && report.warnings.length === 0 && (
            <div className="ok">✅ 幾何預檢通過（最遠航點 {report.max_dist_m} m／
              圍欄 {report.fence_r} m）</div>
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
              {(m.is_active || planTarget(m)) && (
                <div className="mcard-chips">
                  {m.is_active && <span className="chip on-chip">顯示中</span>}
                  {/* 目標機種：選檔當下就看得到這份航線是給誰寫的。
                      檔案沒說時不顯示 chip——**空白代表「沒說」，不是「通用」**。 */}
                  {planTarget(m) && (
                    <span className="chip" title="這份航線宣告的目標機種（來自 .plan）">
                      {planTarget(m)}
                    </span>
                  )}
                </div>
              )}
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
