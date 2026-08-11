"use client";
/** 簡約 HUD（doc/simple-first-redesign.md 即時頁先鋒）。
 *
 * 設計判準：沒讀過說明的人第一眼要知道是什麼——零標籤（▲高度、→速度、
 * 電池圖形、訊號格）、零解釋文字；文字只出現在事件 log 與異常浮出句。
 * 正常狀態不說話；異常才浮一句「發生了什麼＋機器正在做什麼」的人話，
 * 不出術語。安全機制原樣：兩段式確認＝按鈕變色＋「確定？」；返航單擊。
 * 專業數值/完整操作不刪除只隱藏（訊號格/▤ 開專業面板、⌃ 開完整控制）。
 */
import { useEffect, useRef, useState } from "react";

import { evText } from "@/lib/evtext";
import { COMMAND_API, classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const BAR_LEVEL: Record<string, number> = { good: 4, warning: 3, serious: 2, critical: 1 };

function SignalBars({ sinr, onOpen }: { sinr: number | null | undefined; onOpen: () => void }) {
  const cls = sinr != null ? classifySinr(sinr) : null;
  const level = cls ? BAR_LEVEL[cls.key] ?? 0 : 0;
  return (
    <button className="hud-item hud-tap" title="訊號（點開詳細）" onClick={onOpen}>
      <span className="sig-bars" aria-label="訊號強度">
        {[1, 2, 3, 4].map((i) => (
          <span key={i} className="sig-bar" style={{
            height: 3 + i * 3,
            background: i <= level ? cls?.color ?? "var(--muted)" : "var(--hairline)",
          }} />
        ))}
      </span>
    </button>
  );
}

function Battery({ pct }: { pct: number | null | undefined }) {
  if (pct == null) return null;   // 沒有就不畫，不放「—」
  const p = Math.max(0, Math.min(100, pct));
  return (
    <span className="hud-item" title="電量">
      <span className="batt">
        <span className="batt-fill" style={{
          width: `${p}%`,
          background: p <= 20 ? "var(--status-serious)" : "var(--ink-2)",
        }} />
      </span>
      <span className="hud-num">{Math.round(p)}%</span>
    </span>
  );
}

interface Health {
  enabled: boolean;
  drones: Record<string, {
    armed: boolean | null;
    capabilities?: Record<string, string>;
    capability_reasons?: Record<string, string>;
  }>;
}

export default function SimpleHud({ onExpand }: { onExpand: () => void }) {
  const live = useUavStore((s) => s.live);
  const wsConnected = useUavStore((s) => s.wsConnected);
  const events = useUavStore((s) => s.events);
  const setPanelOpen = useUavStore((s) => s.setPanelOpen);

  const [health, setHealth] = useState<Health | null>(null);
  const [confirming, setConfirming] = useState(false);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [logOpen, setLogOpen] = useState(false);

  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try {
        const h = await (await fetch(`${COMMAND_API}/healthz`)).json();
        if (!stop) setHealth(h);
      } catch { if (!stop) setHealth(null); }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  const sid = health?.enabled ? Object.keys(health.drones)[0] ?? null : null;
  const caps = sid ? health!.drones[sid].capabilities : undefined;
  const capOk = (k: string) => !caps || caps[k] === "ok";   // 缺席＝舊後端全功能

  // 情境主按鈕：地面＝起飛（兩段式：變色＋「確定？」）；空中＝返航（單擊）
  const inAir = live?.landed_state === "in_air" || (live?.alt_rel ?? 0) > 2;
  const action = inAir
    ? { label: "返航", path: "/mode/rtl", confirm: false, cap: "rtl" }
    : { label: "起飛", path: "/takeoff", confirm: true, cap: "takeoff" };

  async function fire() {
    if (!sid) return;
    if (action.confirm && !confirming) {
      setConfirming(true);
      if (confirmTimer.current) clearTimeout(confirmTimer.current);
      confirmTimer.current = setTimeout(() => setConfirming(false), 3500);
      return;
    }
    setConfirming(false);
    setBusy(true);
    setErr(null);
    try {
      const alt = Number(localStorage.getItem("takeoff-alt")) || 10;
      const res = await fetch(`${COMMAND_API}/api/command/${sid}${action.path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: action.path === "/takeoff" ? JSON.stringify({ alt }) : undefined,
      });
      if (!res.ok) {
        const d = ((await res.json().catch(() => ({}))) as { detail?: unknown }).detail;
        const s = d as { msg?: string; hint?: string } | undefined;
        setErr(typeof d === "string" ? d
          : s?.msg ? `${s.msg}${s.hint ? `——${s.hint}` : ""}`
          : `失敗（HTTP ${res.status}）`);
      }
    } catch (e) { setErr(`連線失敗：${e}`); }
    setBusy(false);
  }
  useEffect(() =>

    () => { if (confirmTimer.current) clearTimeout(confirmTimer.current); }, []);

  // 異常浮出句：只在異常時出現，說「發生了什麼＋機器在做什麼」，零術語
  const cls = live?.link?.sinr != null ? classifySinr(live.link.sinr) : null;
  const anomaly = !wsConnected ? "與地面站失去連線——畫面資料已停止更新"
    : live && !live.connected ? "無人機失聯——收不到機上資料"
    : cls?.key === "critical" ? "訊號快斷了"
    : cls?.key === "serious" ? "訊號變差了"
    : null;

  const latest = events[0];
  const evTime = (t: string) =>
    new Date(t).toLocaleTimeString("zh-TW", { hour12: false });

  return (
    <>
      {(anomaly || err) && (
        <div className={`hud-toast ${err ? "err" : ""}`}>⚠ {err ?? anomaly}</div>
      )}

      <div className="hud-bottom">
        {live?.alt_rel != null && (
          <span className="hud-item" title="高度">▲<span className="hud-num">{live.alt_rel.toFixed(0)}m</span></span>
        )}
        {live?.ground_speed != null && (
          <span className="hud-item" title="速度">→<span className="hud-num">{live.ground_speed.toFixed(1)}</span></span>
        )}
        <SignalBars sinr={live?.link?.sinr} onOpen={() => setPanelOpen(true)} />
        <Battery pct={live?.battery_pct} />
        <span className="hud-spacer" />
        {sid && (
          <span className="hud-main">
            <button
              className={`hud-cta ${confirming ? "confirming" : ""}`}
              disabled={busy || !capOk(action.cap)}
              title={!capOk(action.cap) ? "此機型尚不支援" : undefined}
              onClick={fire}>
              {busy ? "⋯" : confirming ? "確定？" : action.label}
            </button>
            <button className="hud-more" title="更多操作" onClick={onExpand}>⌃</button>
          </span>
        )}
      </div>

      <div className={`hud-events ${logOpen ? "open" : ""}`}>
        <button className="hud-ticker" onClick={() => setLogOpen(!logOpen)}>
          {latest
            ? <><span className="hud-ev-time">{evTime(latest.time)}</span> {evText(latest)}</>
            : "尚無事件"}
          <span className="hud-spacer" />{logOpen ? "▾" : "▴"}
        </button>
        {logOpen && (
          <div className="hud-log">
            {events.map((e) => (
              <div className={`event ${e.severity === "critical" ? "ev-crit" : ""}`} key={e.id}>
                <span className="dot" style={{
                  background: e.severity === "critical" ? "#a01818"
                    : e.severity === "warning" ? "#fab219" : "#8f8b80" }} />
                <time>{evTime(e.time)}</time>
                <span className="detail">{evText(e)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
