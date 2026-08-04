"use client";
import { useEffect, useState } from "react";

import { API } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

interface Drone {
  id: string; name: string; is_simulated: boolean;
  connection_url: string | null; status: string | null;
}
interface Session {
  id: string; drone_id: string; drone_name: string;
  started_at: string; ended_at: string | null;
  summary: {
    avg_sinr?: number | null; min_sinr?: number | null; avg_rtt_ms?: number | null;
    max_alt_rel?: number | null; samples_total?: number; samples_in_zone?: number;
  } | null;
}

const fmt = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

function duration(a: string, b: string | null): string {
  if (!b) return "進行中";
  const s = Math.round((new Date(b).getTime() - new Date(a).getTime()) / 1000);
  return `${Math.floor(s / 60)}m${(s % 60).toString().padStart(2, "0")}s`;
}

export default function Drones() {
  const [drones, setDrones] = useState<Drone[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const live = useUavStore((s) => s.live);

  useEffect(() => {
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

  return (
    <div className="page-pad">
      {drones.map((d) => {
        const mine = sessions.filter((s) => s.drone_id === d.id);
        const isLive = live?.drone_id === d.id;
        return (
          <div className="card" key={d.id}>
            <div className="drone-head">
              <span className="name">{d.name}</span>
              {d.is_simulated && <span className="chip">模擬</span>}
              {isLive && live?.armed && (
                <span className="chip">
                  <span className="dot" style={{ background: "#d03b3b" }} />
                  飛行中·記錄中
                </span>
              )}
              <span className="meta">
                {d.connection_url ?? ""} · 共 {mine.length} 個架次
              </span>
            </div>

            <table className="table" style={{ marginTop: 10 }}>
              <thead>
                <tr>
                  <th>開始時間</th>
                  <th>時長</th>
                  <th className="num">樣本數</th>
                  <th className="num">平均 SINR</th>
                  <th className="num">最低 SINR</th>
                  <th className="num">平均 RTT</th>
                  <th className="num">最高高度</th>
                </tr>
              </thead>
              <tbody>
                {mine.length === 0 && (
                  <tr><td colSpan={7} className="empty">尚無架次——一次飛行（解鎖到上鎖）＝一個架次</td></tr>
                )}
                {mine.map((s) => (
                  <tr key={s.id}>
                    <td>{new Date(s.started_at).toLocaleString("zh-TW", { hour12: false })}</td>
                    <td>{duration(s.started_at, s.ended_at)}</td>
                    <td className="num">{s.summary?.samples_total ?? "—"}</td>
                    <td className="num">{fmt(s.summary?.avg_sinr)} dB</td>
                    <td className="num">{fmt(s.summary?.min_sinr)} dB</td>
                    <td className="num">{fmt(s.summary?.avg_rtt_ms, 0)} ms</td>
                    <td className="num">{fmt(s.summary?.max_alt_rel, 0)} m</td>
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
