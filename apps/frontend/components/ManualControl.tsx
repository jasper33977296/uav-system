"use client";
/** 虛擬搖桿手動控制（GCS 取代階段 3）：POSCTL 直接操控。
 *
 * 安全設計（與 command 服務的 deadman 配合，SITL 實測 ~12Hz 驗證）：
 *   - 啟用期間以 10Hz 連續串流設定點（含中位）——後端 0.4s 沒收到就回中位
 *     （懸停）、2s 沒收到自動切 Hold；瀏覽器當掉／網路斷線都安全收場。
 *     curl 級的低速率會被 deadman 判中位而「不動」，串流頻率不能降。
 *   - 視窗失焦、分頁隱藏、元件卸載：立即停止串流並呼叫 manual/stop
 *   - 預設停用；啟用鈕紅色警示；啟用流程 manual/start（先送中位再切 POSCTL）
 *
 * 軸向（皆 -1..1）：x=俯仰(前+) y=橫滾(右+) z=油門(上+，0=定高) r=偏航(右+)
 * 左搖桿／W S A D＝油門＋偏航；右搖桿／方向鍵＝俯仰＋橫滾。
 */
import { useEffect, useRef, useState } from "react";

import { COMMAND_API } from "@/lib/signal";

const RATE_MS = 100;  // ≥10Hz（後端 deadman 0.4s）
const KB_GAIN = 0.5;  // 鍵盤按住的軸偏移：滿舵太猛，半舵夠用且可與搖桿疊加

const clamp = (v: number) => Math.max(-1, Math.min(1, v));

function Joystick({ label, sub, onMove }: {
  label: string; sub: string;
  onMove: (dx: number, dy: number) => void;  // dx 右+、dy 上+，皆 -1..1
}) {
  const padRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const [knob, setKnob] = useState({ dx: 0, dy: 0 });

  const apply = (dx: number, dy: number) => { setKnob({ dx, dy }); onMove(dx, dy); };
  const fromEvent = (e: React.PointerEvent) => {
    const r = padRef.current!.getBoundingClientRect();
    let dx = ((e.clientX - r.left) / r.width) * 2 - 1;
    let dy = -(((e.clientY - r.top) / r.height) * 2 - 1);
    const len = Math.hypot(dx, dy);
    if (len > 1) { dx /= len; dy /= len; }   // 圓形限位
    apply(dx, dy);
  };

  return (
    <div className="joy">
      <div className="joy-pad" ref={padRef}
        onPointerDown={(e) => {
          draggingRef.current = true;
          (e.currentTarget as Element).setPointerCapture(e.pointerId);
          fromEvent(e);
        }}
        onPointerMove={(e) => { if (draggingRef.current) fromEvent(e); }}
        onPointerUp={() => { draggingRef.current = false; apply(0, 0); }}
        onPointerCancel={() => { draggingRef.current = false; apply(0, 0); }}>
        <div className="joy-knob" style={{
          transform: `translate(calc(-50% + ${(knob.dx * 33).toFixed(1)}px),`
            + ` calc(-50% + ${(-knob.dy * 33).toFixed(1)}px))`,
        }} />
      </div>
      <span className="label">{label}</span>
      <span className="sub">{sub}</span>
    </div>
  );
}

export default function ManualControl({ sid, lockedReason = null }: {
  sid: string | null;
  lockedReason?: string | null;   // 非 null＝能力 gating 鎖定（顯示原因、禁啟用）
}) {
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // deadman 三態視覺（restyle §3，全靠前端本地資訊）：
  // ok(<0.4s) / warn(0.4–2s 進度環) / lost(≥2s＝後端已接管切 Hold)
  const [linkState, setLinkState] = useState<"ok" | "warn" | "lost">("ok");
  const [warnFrac, setWarnFrac] = useState(0);
  const lastOkRef = useRef(0);
  const joyRef = useRef({ x: 0, y: 0, z: 0, r: 0 });
  const keysRef = useRef(new Set<string>());
  const enabledRef = useRef(false);
  const sidRef = useRef<string | null>(sid);
  useEffect(() => { enabledRef.current = enabled; }, [enabled]);

  const postStop = (keepalive = false) => {
    if (!sidRef.current) return;
    fetch(`${COMMAND_API}/api/command/${sidRef.current}/manual/stop`,
      { method: "POST", keepalive }).catch(() => {});
  };

  const stop = () => {   // 停止串流 → 後端結束手動並切 Hold
    setEnabled(false);
    setLinkState("ok");
    keysRef.current.clear();
    joyRef.current = { x: 0, y: 0, z: 0, r: 0 };
    postStop();
  };

  // 換機（或機消失）不能帶著舊機的手動狀態：先對舊 sid 停手動再換
  useEffect(() => {
    if (enabledRef.current && sidRef.current !== sid) stop();
    sidRef.current = sid;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  // 能力 gating 中途翻轉（capabilities 更新）：啟用中被鎖就立即停
  useEffect(() => {
    if (lockedReason && enabledRef.current) stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lockedReason]);

  async function start() {
    if (!sid) return;
    setBusy(true);
    setErr(null);
    try {
      // 約 0.5s 才回：後端先送中位 MANUAL_CONTROL 再切 POSCTL
      const res = await fetch(`${COMMAND_API}/api/command/${sid}/manual/start`,
        { method: "POST" });
      if (!res.ok) {
        // detail 可能是字串或結構化 {msg, hint}（含非 PX4 機的 501 飛安 guard）
        const d = ((await res.json().catch(() => ({}))) as { detail?: unknown }).detail;
        const s = d as { msg?: string; hint?: string } | undefined;
        setErr(typeof d === "string" ? d
          : s?.msg ? `${s.msg}${s.hint ? `——${s.hint}` : ""}`
          : `啟用失敗（HTTP ${res.status}）`);
      } else {
        setEnabled(true);
      }
    } catch (e) {
      setErr(`連線失敗：${e}`);
    }
    setBusy(false);
  }

  // 10Hz 設定點串流：啟用期間連續送（含中位）。持續送中位＝命令懸停並餵
  // deadman——只有前端真的死掉（關頁/斷網）才輪到後端的自動中位與 Hold。
  useEffect(() => {
    if (!enabled) return;
    const send = () => {
      const k = keysRef.current;
      const j = joyRef.current;
      const kb = {
        x: (k.has("ArrowUp") ? KB_GAIN : 0) - (k.has("ArrowDown") ? KB_GAIN : 0),
        y: (k.has("ArrowRight") ? KB_GAIN : 0) - (k.has("ArrowLeft") ? KB_GAIN : 0),
        z: (k.has("w") ? KB_GAIN : 0) - (k.has("s") ? KB_GAIN : 0),
        r: (k.has("d") ? KB_GAIN : 0) - (k.has("a") ? KB_GAIN : 0),
      };
      fetch(`${COMMAND_API}/api/command/${sidRef.current}/manual`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          x: clamp(j.x + kb.x), y: clamp(j.y + kb.y),
          z: clamp(j.z + kb.z), r: clamp(j.r + kb.r),
        }),
      }).then((res) => {
        if (res.ok) lastOkRef.current = performance.now();
      }).catch(() => { /* 掉包交給後端 deadman；lastOk 不更新讓三態如實反映 */ });
    };
    send();
    const t = setInterval(send, RATE_MS);
    return () => clearInterval(t);
  }, [enabled]);

  // deadman 三態計時：以「最後一次成功送達」起算，門檻對齊後端（0.4s/2s）。
  // ≥2s＝後端已自動 Hold（接管）——停止串流但不打 manual/stop（已接管，
  // 打了也只是重複切 Hold），顯示橫幅讓操作者知道並可重新啟用
  useEffect(() => {
    if (!enabled) return;
    lastOkRef.current = performance.now();   // 啟用起點
    setLinkState("ok");
    const t = setInterval(() => {
      const gap = (performance.now() - lastOkRef.current) / 1000;
      if (gap >= 2) {
        setEnabled(false);
        keysRef.current.clear();
        joyRef.current = { x: 0, y: 0, z: 0, r: 0 };
        setLinkState("lost");
      } else if (gap >= 0.4) {
        setWarnFrac(Math.min(1, (gap - 0.4) / 1.6));
        setLinkState("warn");
      } else {
        setLinkState("ok");
      }
    }, 150);
    return () => clearInterval(t);
  }, [enabled]);

  // 失焦／分頁隱藏立即停用：操作者看不到畫面就不該在控
  useEffect(() => {
    if (!enabled) return;
    const onBlur = () => stop();
    const onVis = () => { if (document.hidden) stop(); };
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVis);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  // 卸載（切頁）時仍在手動：keepalive 讓 stop 請求在拆頁後送得出去
  useEffect(() => () => { if (enabledRef.current) postStop(true); }, []);

  // 鍵盤：按住偏移、放開回中。打字目標（input/select）不攔，方向鍵防捲頁。
  useEffect(() => {
    if (!enabled) return;
    const KEYS = new Set(["w", "s", "a", "d",
      "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
    const norm = (e: KeyboardEvent) =>
      e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const isTyping = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      return !!t && (t.tagName === "INPUT" || t.tagName === "SELECT"
        || t.tagName === "TEXTAREA" || t.isContentEditable);
    };
    const down = (e: KeyboardEvent) => {
      const k = norm(e);
      if (!KEYS.has(k) || isTyping(e)) return;
      e.preventDefault();
      keysRef.current.add(k);
    };
    const up = (e: KeyboardEvent) => { keysRef.current.delete(norm(e)); };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      keysRef.current.clear();
    };
  }, [enabled]);

  return (
    <>
      {linkState === "lost" && !enabled && (
        <div className="manual-lost">
          ⚠ deadman 已觸發——輸入中斷逾 2 秒，機體已自動 Hold。
          <div className="cmd-row">
            <button className="btn-plain btn-sm"
              disabled={!sid || busy} onClick={start}>
              {busy ? "⋯" : "重新啟用手動"}
            </button>
            <button className="btn-plain btn-sm"
              onClick={() => setLinkState("ok")}>關閉</button>
          </div>
        </div>
      )}
      <div className="cmd-row">
        {enabled ? (
          <button className="btn-danger btn-sm" onClick={stop}>停用手動</button>
        ) : linkState !== "lost" && (
          <button className="btn-danger btn-sm"
            disabled={!sid || busy || !!lockedReason} onClick={start}>
            {busy ? "⋯" : "啟用手動控制"}
          </button>
        )}
        {enabled && linkState === "ok" && (
          <span className="manual-live ok">● 手動控制中 · 10Hz 串流</span>
        )}
        {enabled && linkState === "warn" && (
          <span className="manual-live warn">
            <svg className="dm-ring" viewBox="0 0 14 14" aria-hidden>
              <circle cx="7" cy="7" r="5.5" fill="none"
                stroke="var(--hairline)" strokeWidth="2.5" />
              <circle cx="7" cy="7" r="5.5" fill="none"
                stroke="var(--status-warn)" strokeWidth="2.5"
                strokeDasharray={`${((1 - warnFrac) * 34.6).toFixed(1)} 34.6`}
                transform="rotate(-90 7 7)" />
            </svg>
            輸入中斷——即將自動懸停
          </span>
        )}
      </div>
      {lockedReason && <p className="hint-line">· 手動：{lockedReason}</p>}
      {enabled ? (
        <div className="joy-row">
          <Joystick label="油門｜偏航" sub="W S｜A D"
            onMove={(dx, dy) => { joyRef.current.z = dy; joyRef.current.r = dx; }} />
          <Joystick label="俯仰｜橫滾" sub="↑ ↓｜← →"
            onMove={(dx, dy) => { joyRef.current.x = dy; joyRef.current.y = dx; }} />
        </div>
      ) : !lockedReason && (
        // 動作語彙（語彙分層）：模式名（POSCTL 等）只在狀態列按機型解碼顯示
        <p className="hint-line">
          切入手動位置模式，以搖桿／鍵盤直接操控；放開回中＝定點懸停。
          失焦或關頁自動懸停並於 2 秒後自動切 Hold（後端 deadman）。
        </p>
      )}
      {err && <div className="cmd-result err">{err}</div>}
    </>
  );
}
