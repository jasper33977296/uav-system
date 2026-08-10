"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { colorFor } from "@/components/droneLayer";
import { API } from "@/lib/signal";

interface Mission {
  id: string; name: string; source: string | null;
  created_at: string; is_active: boolean; waypoint_count: number;
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

/** 解析 QGC .plan：取出帶座標的導航項（16 WAYPOINT / 21 LAND / 22 TAKEOFF） */
function parsePlan(text: string): { seq: number; lat: number; lon: number; alt: number | null; action: string }[] {
  const j = JSON.parse(text);
  const items = j?.mission?.items ?? [];
  const out = [];
  for (const it of items) {
    const [ , , , , lat, lon, alt] = it.params ?? [];
    if (![16, 21, 22].includes(it.command) || lat == null || lon == null) continue;
    out.push({
      seq: out.length, lat, lon,
      alt: it.Altitude ?? alt ?? null,
      action: it.command === 22 ? "takeoff" : it.command === 21 ? "land" : "waypoint",
    });
  }
  return out;
}

export default function Missions() {
  const router = useRouter();
  const [missions, setMissions] = useState<Mission[]>([]);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const reload = useCallback(() => {
    fetch(`${API}/api/missions`).then((r) => r.json()).then(setMissions).catch(() => {});
    fetch(`${API}/api/sessions?limit=200`)
      .then((r) => r.json())
      .then((rows) => setSessions(rows.map((r: any) => ({
        ...r, summary: typeof r.summary === "string" ? JSON.parse(r.summary) : r.summary,
      }))))
      .catch(() => {});
  }, []);
  useEffect(reload, [reload]);

  async function call(path: string, init?: RequestInit) {
    setErr(null); setBusy(true);
    try {
      const res = await fetch(`${API}${path}`, init);
      if (!res.ok) setErr((await res.json()).detail ?? `失敗（${res.status}）`);
      else reload();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const [report, setReport] = useState<PlanCheck | null>(null);

  async function uploadPlan(f: File) {
    setErr(null); setReport(null);
    let wps;
    try {
      wps = parsePlan(await f.text());
    } catch {
      setErr("不是有效的 QGC .plan 檔"); return;
    }
    if (wps.length < 2) { setErr("檔案內找不到足夠的導航航點"); return; }
    setBusy(true);
    try {
      const res = await fetch(`${API}/api/missions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: f.name.replace(/\.plan$/i, ""), source: "plan-file", waypoints: wps }),
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
      <div className="card">
        <h3>路徑管理</h3>
        <p className="hint-line">
          路徑來自 QGC：上傳 .plan 檔，或把機上目前的任務讀回儲存。
          「顯示於即時頁」的那一條會以灰色預計路徑疊在即時監控地圖上。
        </p>
        <div className="form-row" style={{ marginTop: 8 }}>
          <button disabled={busy}
            onClick={() => call("/api/missions/from-vehicle", { method: "POST" })}>
            從機上讀回並儲存
          </button>
          <button disabled={busy} onClick={() => fileRef.current?.click()}>
            上傳 .plan 檔
          </button>
          <input ref={fileRef} type="file" accept=".plan,application/json" hidden
            onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadPlan(f); e.target.value = ""; }} />
        </div>
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
            {!report.ok && (
              <div className="hint-line">已存入任務庫（草稿可留），但**上傳到機時會被擋**
                ——修正路徑或調整圍欄設定（.env 的 GEOFENCE_*，需與 QGC 一致）</div>
            )}
          </div>
        )}
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>名稱</th><th>來源</th><th className="num">航點數</th>
              <th>建立時間</th><th>即時頁顯示</th><th></th>
            </tr>
          </thead>
          <tbody>
            {missions.length === 0 && (
              <tr><td colSpan={6} className="empty">尚無儲存的路徑</td></tr>
            )}
            {missions.map((m) => (
              <tr key={m.id} className="row-link" title="展開此路徑的航線紀錄"
                  onClick={() => setOpenId(openId === m.id ? null : m.id)}>
                <td>
                  {m.name}
                  {m.is_active && <span className="chip" style={{ marginLeft: 8 }}>
                    <span className="dot" style={{ background: "#0ca30c" }} />顯示中</span>}
                </td>
                <td>{m.source === "vehicle" ? "機上讀回" : ".plan 檔"}</td>
                <td className="num">{m.waypoint_count}</td>
                <td>{new Date(m.created_at).toLocaleString("zh-TW", { hour12: false })}</td>
                <td>
                  <button disabled={busy}
                    onClick={() => call(`/api/missions/${m.id}/activate?active=${!m.is_active}`,
                                        { method: "POST" })}>
                    {m.is_active ? "隱藏" : "顯示於即時頁"}
                  </button>
                </td>
                <td onClick={(e) => e.stopPropagation()}>
                  <button className="btn-danger" disabled={busy}
                    onClick={() => { if (window.confirm(`刪除路徑「${m.name}」？`))
                      call(`/api/missions/${m.id}`, { method: "DELETE" }); }}>
                    刪除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {openId && (() => {
          const mine = sessions.filter((s) => s.mission_id === openId);
          const m = missions.find((x) => x.id === openId);
          return (
            <div style={{ marginTop: 12 }}>
              <div className="drone-head">
                <span className="meta">
                  「{m?.name}」的航線紀錄 · 共 {mine.length} 條（跨所有無人機）
                </span>
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
      </div>
    </div>
  );
}
