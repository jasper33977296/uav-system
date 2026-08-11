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

function SignalBars({ sinr, lost, onOpen }: {
  sinr: number | null | undefined; lost: boolean; onOpen: () => void;
}) {
  const cls = !lost && sinr != null ? classifySinr(sinr) : null;
  const level = cls ? BAR_LEVEL[cls.key] ?? 0 : 0;
  return (
    <button className="hud-item hud-tap" title="訊號（點開詳細）" onClick={onOpen}>
      <span className="sig-bars" aria-label={lost ? "失聯" : "訊號強度"}>
        {[1, 2, 3, 4].map((i) => (
          <span key={i} className="sig-bar" style={{
            height: 3 + i * 3,
            background: i <= level ? cls?.color ?? "var(--muted)" : "var(--hairline)",
          }} />
        ))}
        {/* 失聯＝0 格＋斜線：形狀先於顏色（icon spec） */}
        {lost && <span className="sig-slash" />}
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

  const deadman = useUavStore((s) => s.deadman);
  const [health, setHealth] = useState<Health | null>(null);
  const [confirming, setConfirming] = useState(false);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [takeoffDeniedAt, setTakeoffDeniedAt] = useState(0);
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

  // 情境主按鈕：地面＝起飛（兩段式：變色＋「確定？」）；
  // 空中＝返航（單擊、danger 紅——緊急動作不吃 accent，與展開面板 RTL 同語意）
  const inAir = live?.landed_state === "in_air" || (live?.alt_rel ?? 0) > 2;
  const action = inAir
    ? { label: "⌂ 返航", path: "/mode/rtl", confirm: false, cap: "rtl", danger: true }
    : { label: "↑ 起飛", path: "/takeoff", confirm: true, cap: "takeoff", danger: false };

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
        if (action.path === "/takeoff") {
          // 起飛被拒＝人話句（點擊展開完整面板看 not_ready_reasons 與原因原文）
          setTakeoffDeniedAt(Date.now());
        } else {
          const d = ((await res.json().catch(() => ({}))) as { detail?: unknown }).detail;
          const s = d as { msg?: string; hint?: string } | undefined;
          setErr(typeof d === "string" ? d
            : s?.msg ? `${s.msg}${s.hint ? `——${s.hint}` : ""}`
            : `失敗（HTTP ${res.status}）`);
        }
      }
    } catch (e) { setErr(`連線失敗：${e}`); }
    setBusy(false);
  }
  useEffect(() =>

    () => { if (confirmTimer.current) clearTimeout(confirmTimer.current); }, []);

  // 異常浮出句（設計定稿句式表）：同時多事只顯最嚴重一則；danger 常駐至
  // 解除、warn 10s、ok 3s；句中零術語。warn/ok 的「一段時間後自動消失」
  // 靠 episode 起點時間戳實現（遙測 5Hz 重渲染自然帶動過期）
  const cls = live?.link?.sinr != null ? classifySinr(live.link.sinr) : null;
  const clsKey = cls?.key ?? null;
  const prevClsRef = useRef<string | null>(null);
  const epRef = useRef({ degraded: 0, recovered: 0, gps: 0 });
  useEffect(() => {
    const prev = prevClsRef.current;
    if (clsKey === "serious" && prev !== "serious" && prev !== "critical") {
      epRef.current.degraded = Date.now();
    }
    if ((clsKey === "good" || clsKey === "warning")
        && (prev === "serious" || prev === "critical")) {
      epRef.current.recovered = Date.now();
    }
    prevClsRef.current = clsKey;
  }, [clsKey]);
  const droneLost = !!live && !live.connected;
  // link_age_s > 5s＝失聯預警（warn；connected=false 才是硬斷言 danger）
  const ageStale = !!live && live.connected && (live.link_age_s ?? 0) > 5;
  const gpsBad = !!live && live.connected && live.gps_fix != null && live.gps_fix < 3;
  useEffect(() => { if (gpsBad) epRef.current.gps = Date.now(); }, [gpsBad]);
  // failsafe：最近 30s 內的 critical failsafe 事件（自動處置進行中）
  const fsEvent = events.find((e) => e.severity === "critical" && /failsafe/i.test(e.type));
  const fsActive = !!fsEvent && Date.now() - new Date(fsEvent.time).getTime() < 30000;

  const now = Date.now();
  const notice =
    !wsConnected ? { t: "與系統失去連線——畫面可能不是最新", sev: "err" as const }
    : droneLost ? { t: "無人機失聯——顯示的是最後已知位置", sev: "err" as const }
    : deadman ? { t: "操控中斷——無人機已自動懸停", sev: "err" as const }
    : fsActive ? { t: "無人機進入緊急狀態——正在自動處置", sev: "err" as const }
    : clsKey === "critical" ? { t: "訊號快斷了", sev: "err" as const }
    : err ? { t: err, sev: "err" as const }
    : now - takeoffDeniedAt < 10000
      ? { t: "現在還不能起飛——點這裡看原因", sev: "warn" as const, onClick: onExpand }
    : ageStale ? { t: "無人機資料延遲——顯示的可能不是最新位置", sev: "warn" as const }
    : gpsBad && now - epRef.current.gps < 10000
      ? { t: "衛星訊號變弱——位置可能不準", sev: "warn" as const }
    : clsKey === "serious" && now - epRef.current.degraded < 10000
      ? { t: "訊號變差了", sev: "warn" as const }
    : now - epRef.current.recovered < 3000
      ? { t: "訊號恢復了", sev: "ok" as const }
    : null;

  const latest = events[0];
  const evTime = (t: string) =>
    new Date(t).toLocaleTimeString("zh-TW", { hour12: false });

  return (
    <>
      {notice && (
        <div className={`hud-toast ${notice.sev} ${notice.onClick ? "hud-tap" : ""}`}
          onClick={notice.onClick}>
          {notice.sev === "ok" ? "✓" : "⚠"} {notice.t}
        </div>
      )}

      <div className="hud-bottom">
        {live?.alt_rel != null && (
          <span className="hud-item" title="高度">▲<span className="hud-num">{live.alt_rel.toFixed(0)}m</span></span>
        )}
        {live?.ground_speed != null && (
          <span className="hud-item" title="速度">→<span className="hud-num">{live.ground_speed.toFixed(1)}</span></span>
        )}
        <SignalBars sinr={live?.link?.sinr} lost={!wsConnected || droneLost}
          onOpen={() => setPanelOpen(true)} />
        <Battery pct={live?.battery_pct} />
        <span className="hud-spacer" />
        {sid && (
          <span className="hud-main">
            <button
              className={`hud-cta ${action.danger ? "danger" : ""} ${confirming ? "confirming" : ""}`}
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
