"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { API } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

interface Drone {
  id: string; name: string; is_simulated: boolean; is_primary: boolean;
  connection_url: string | null; status: string | null;
}
interface Session {
  id: string; drone_id: string; drone_name: string;
  mission_name: string | null;
  started_at: string; ended_at: string | null;
  summary: {
    avg_sinr?: number | null; min_sinr?: number | null; avg_rtt_ms?: number | null;
    max_alt_rel?: number | null; samples_total?: number;
  } | null;
}

const fmt = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

function duration(a: string, b: string | null): string {
  if (!b) return "進行中";
  const s = Math.round((new Date(b).getTime() - new Date(a).getTime()) / 1000);
  return `${Math.floor(s / 60)}m${(s % 60).toString().padStart(2, "0")}s`;
}

export default function Drones() {
  const router = useRouter();
  const [drones, setDrones] = useState<Drone[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const live = useUavStore((s) => s.live);

  const reload = useCallback(() => {
    fetch(`${API}/api/drones`).then((r) => r.json()).then(setDrones).catch(() => {});
    fetch(`${API}/api/sessions`)
      .then((r) => r.json())
      .then((rows) =>
        setSessions(
          rows.map((r: any) => ({
            ...r,
            // REST 的 JSONB 是字串（asyncpg 預設），統一 parse
            summary: typeof r.summary === "string" ? JSON.parse(r.summary) : r.summary,
          }))
        )
      )
      .catch(() => {});
  }, []);
  useEffect(reload, [reload]);

  async function register() {
    setErr(null);
    const res = await fetch(`${API}/api/drones`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), connection_url: url.trim() || null }),
    });
    if (!res.ok) {
      setErr((await res.json()).detail ?? `註冊失敗（${res.status}）`);
      return;
    }
    setName(""); setUrl("");
    reload();
  }

  async function remove(d: Drone, sessionCount: number) {
    const msg =
      `刪除「${d.name}」？\n\n將一併刪除其 ${sessionCount} 條航線與全部遙測、` +
      `鏈路量測、事件資料。此操作無法復原。`;
    if (!window.confirm(msg)) return;
    setErr(null);
    const res = await fetch(`${API}/api/drones/${d.id}`, { method: "DELETE" });
    if (!res.ok) {
      setErr((await res.json()).detail ?? `刪除失敗（${res.status}）`);
      return;
    }
    reload();
  }

  return (
    <div className="page-pad">
      <div className="card">
        <h3>註冊無人機</h3>
        <p className="hint-line">
          機的身分由系統端管理：註冊 → 「設為主機」後，MAVLink（14540）收到的
          遙測就記在這台名下；也可直接對現有的機「改名」。
          多機同時接入待 ingest 多實例化（issues/011）。
        </p>
        <div className="form-row">
          <input
            placeholder="名稱（如 rb5-uav-1）"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            placeholder="連線位址（選填，如 udpin://0.0.0.0:14540）"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button disabled={!name.trim()} onClick={register}>註冊</button>
        </div>
        {err && <div className="form-err">{err}</div>}
      </div>

      {drones.map((d) => {
        const mine = sessions.filter((s) => s.drone_id === d.id);
        const isLive = live?.drone_id === d.id;
        return (
          <div className="card" key={d.id}>
            <div className="drone-head">
              <span className="name">{d.name}</span>
              {d.is_primary && <span className="chip">
                <span className="dot" style={{ background: "#0ca30c" }} />主機</span>}
              {d.is_simulated && <span className="chip">模擬</span>}
              {isLive && live?.armed && (
                <span className="chip">
                  <span className="dot" style={{ background: "#d03b3b" }} />
                  飛行中·記錄中
                </span>
              )}
              <span className="meta">
                {d.connection_url ?? ""} · 共 {mine.length} 條航線
              </span>
              <span className="spacer" />
              <button className="btn-plain btn-sm"
                onClick={async () => {
                  const name = window.prompt("新名稱", d.name);
                  if (!name || name === d.name) return;
                  const res = await fetch(`${API}/api/drones/${d.id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name }),
                  });
                  if (!res.ok) setErr((await res.json()).detail ?? "改名失敗");
                  reload();
                }}>改名</button>{" "}
              {!d.is_primary && !d.name.startsWith("swarm-") && (
                <button className="btn-plain btn-sm"
                  title="MAVLink 收到的遙測記在這台名下（飛行中無法切換）"
                  onClick={async () => {
                    const res = await fetch(`${API}/api/drones/${d.id}/primary`,
                      { method: "POST" });
                    if (!res.ok) setErr((await res.json()).detail ?? "切換失敗");
                    reload();
                  }}>設為主機</button>
              )}{" "}
              <button
                className="btn-danger"
                disabled={isLive}
                title={isLive ? "連線中的無人機無法刪除" : "刪除無人機與其全部資料"}
                onClick={() => remove(d, mine.length)}
              >
                刪除
              </button>
            </div>

            <table className="table" style={{ marginTop: 10 }}>
              <thead>
                <tr>
                  <th>開始時間</th>
                  <th>任務</th>
                  <th>時長</th>
                  <th className="num">樣本數</th>
                  <th className="num">平均 SINR</th>
                  <th className="num">最低 SINR</th>
                  <th className="num">平均 RTT</th>
                  <th className="num">最高高度</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {mine.length === 0 && (
                  <tr><td colSpan={9} className="empty">尚無航線——一次飛行（解鎖到上鎖）＝一條航線紀錄</td></tr>
                )}
                {mine.map((s) => (
                  <tr
                    key={s.id}
                    className="row-link"
                    title="點擊回放這條航線"
                    onClick={() => router.push(`/replay/${s.id}`)}
                  >
                    <td>{new Date(s.started_at).toLocaleString("zh-TW", { hour12: false })}</td>
                    <td>{s.mission_name ?? "—"}</td>
                    <td>{duration(s.started_at, s.ended_at)}</td>
                    <td className="num">{s.summary?.samples_total ?? "—"}</td>
                    <td className="num">{fmt(s.summary?.avg_sinr)} dB</td>
                    <td className="num">{fmt(s.summary?.min_sinr)} dB</td>
                    <td className="num">{fmt(s.summary?.avg_rtt_ms, 0)} ms</td>
                    <td className="num">{fmt(s.summary?.max_alt_rel, 0)} m</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {/* 匯出＝下載完整 JSON；配合 30 天 retention，要長期保留先匯出 */}
                      <a className="btn-plain btn-sm" href={`${API}/api/sessions/${s.id}/export`}
                         download title="下載此航線的完整原始資料（JSON）">匯出</a>{" "}
                      <button className="btn-danger btn-sm"
                        title="從資料庫移除此航線（請先匯出）"
                        onClick={() => {
                          if (window.confirm(
                            `移除航線 ${new Date(s.started_at).toLocaleString("zh-TW", { hour12: false })}？\n\n` +
                            "將刪除其全部遙測與鏈路資料。若尚未匯出，資料將永久遺失。"))
                            fetch(`${API}/api/sessions/${s.id}`, { method: "DELETE" }).then(reload);
                        }}>移除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
      {drones.length === 0 && (
        <div className="card"><div className="empty">讀取中，或 backend 未連線</div></div>
      )}
    </div>
  );
}
