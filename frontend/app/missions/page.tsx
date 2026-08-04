"use client";
import { useCallback, useEffect, useRef, useState } from "react";

import { API } from "@/lib/signal";

interface Mission {
  id: string; name: string; source: string | null;
  created_at: string; is_active: boolean; waypoint_count: number;
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
  const [missions, setMissions] = useState<Mission[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const reload = useCallback(() => {
    fetch(`${API}/api/missions`).then((r) => r.json()).then(setMissions).catch(() => {});
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

  async function uploadPlan(f: File) {
    setErr(null);
    let wps;
    try {
      wps = parsePlan(await f.text());
    } catch {
      setErr("不是有效的 QGC .plan 檔"); return;
    }
    if (wps.length < 2) { setErr("檔案內找不到足夠的導航航點"); return; }
    await call("/api/missions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: f.name.replace(/\.plan$/i, ""), source: "plan-file", waypoints: wps }),
    });
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
              <tr key={m.id}>
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
                <td>
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
      </div>
    </div>
  );
}
