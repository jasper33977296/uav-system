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
import { classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const BAR_LEVEL: Record<string, number> = { good: 4, warning: 3, serious: 2, critical: 1 };

/** 訊號格（icon spec：格數＝分級、失聯＝0 格＋斜線，形狀先於顏色）。
 * 有 onOpen＝HUD 可點開詳細；無＝純顯示（機隊頁一行式共用）。 */
export function SignalBars({ sinr, lost = false, onOpen }: {
  sinr: number | null | undefined; lost?: boolean; onOpen?: () => void;
}) {
  const cls = !lost && sinr != null ? classifySinr(sinr) : null;
  const level = cls ? BAR_LEVEL[cls.key] ?? 0 : 0;
  const bars = (
    <span className="sig-bars" aria-label={lost ? "失聯" : "訊號強度"}>
      {[1, 2, 3, 4].map((i) => (
        <span key={i} className="sig-bar" style={{
          height: 3 + i * 3,
          background: i <= level ? cls?.color ?? "var(--muted)" : "var(--hairline)",
        }} />
      ))}
      {lost && <span className="sig-slash" />}
    </span>
  );
  return onOpen ? (
    <button className="hud-item hud-tap" title="訊號（點開詳細）" onClick={onOpen}>
      {bars}
    </button>
  ) : bars;
}

/** 電池圖形（填充＝存量、<20% 轉紅）；無資料不畫、不放「—」。 */
export function Battery({ pct, plain = false }: {
  pct: number | null | undefined; plain?: boolean;
}) {
  if (pct == null) return null;
  const p = Math.max(0, Math.min(100, pct));
  const body = (
    <>
      <span className="batt">
        <span className="batt-fill" style={{
          width: `${p}%`,
          background: p <= 20 ? "var(--status-serious)" : "var(--ink-2)",
        }} />
      </span>
      <span className="hud-num">{Math.round(p)}%</span>
    </>
  );
  return plain
    ? <span className="batt-plain" title="電量">{body}</span>
    : <span className="hud-item" title="電量">{body}</span>;
}

export default function SimpleHud() {
  const live = useUavStore((s) => s.live);
  const wsConnected = useUavStore((s) => s.wsConnected);
  const events = useUavStore((s) => s.events);
  const setPanelOpen = useUavStore((s) => s.setPanelOpen);
  const deadman = useUavStore((s) => s.deadman);
  // 起飛被拒通知來自任務控制面板（主按鈕已併回面板首行，ui-spec §2）
  const takeoffDeniedAt = useUavStore((s) => s.takeoffDeniedAt);
  const [logOpen, setLogOpen] = useState(false);

  // 異常 toast（ui-spec §0.2/§0.3）：同時多事只顯最嚴重一則；
  // 一律 10s 或點擊即消（✕）——toast 是通知不是狀態的家，持續性危險由
  // HUD 樣式（訊號格斜線等）與事件流承載
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

  // 候選句（優先序）：key 用於「同一事件只通知一次」——條件解除後
  // key 歸零，下次再發生才會再跳
  const now = Date.now();
  const candidate =
    !wsConnected
      ? { key: "ws", t: "與系統失去連線——畫面可能不是最新", sev: "err" as const }
    : droneLost
      ? { key: "lost", t: "無人機失聯——顯示的是最後已知位置", sev: "err" as const }
    : deadman
      ? { key: "deadman", t: "操控中斷——無人機已自動懸停", sev: "err" as const }
    : fsActive
      ? { key: `fs:${fsEvent!.id}`, t: "無人機進入緊急狀態——正在自動處置", sev: "err" as const }
    : clsKey === "critical"
      ? { key: "critical", t: "訊號快斷了", sev: "err" as const }
    : now - takeoffDeniedAt < 10000
      ? { key: `takeoff:${takeoffDeniedAt}`, t: "現在還不能起飛——點這裡看原因",
          sev: "warn" as const, expand: true }
    : ageStale
      ? { key: "stale", t: "資料延遲——畫面可能不是最新", sev: "warn" as const }
    : gpsBad
      ? { key: "gps", t: "衛星訊號變弱——位置可能不準", sev: "warn" as const }
    : clsKey === "serious"
      ? { key: "degraded", t: "訊號變差了", sev: "warn" as const }
    : now - epRef.current.recovered < 10000
      ? { key: `rec:${epRef.current.recovered}`, t: "訊號恢復了", sev: "ok" as const }
    : null;

  // toast 引擎：新 key → 顯示 10s（或點擊/✕ 消）；同 key 不重複跳
  type Toast = { key: string; t: string; sev: "err" | "warn" | "ok"; expand?: boolean };
  const [toast, setToast] = useState<Toast | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastKeyRef = useRef<string | null>(null);
  const candKey = candidate?.key ?? null;
  useEffect(() => {
    if (candKey && candKey !== lastKeyRef.current) {
      lastKeyRef.current = candKey;
      setToast(candidate!);
      if (toastTimer.current) clearTimeout(toastTimer.current);
      toastTimer.current = setTimeout(() => setToast(null), 10000);
    }
    if (!candKey) lastKeyRef.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candKey]);
  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
  }, []);
  const dismiss = () => setToast(null);

  const latest = events[0];
  const evTime = (t: string) =>
    new Date(t).toLocaleTimeString("zh-TW", { hour12: false });

  return (
    <>
      {toast && (
        <div className={`hud-toast ${toast.sev}`}
          onClick={() => {
            // 起飛被拒例外：點擊展開任務控制面板看原因；其餘點擊即消
            if (toast.expand) useUavStore.getState().requestCmdPanel();
            dismiss();
          }}>
          {toast.sev === "ok" ? "✓" : "⚠"} {toast.t}
          <span className="hud-toast-x" aria-label="關閉">✕</span>
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
