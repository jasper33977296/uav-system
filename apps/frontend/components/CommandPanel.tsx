"use client";
/** 任務控制面板（GCS 取代階段 3）：即時頁的指令操作 UI。
 *
 * 對象是獨立的 command 服務（:38001，sysid 定址）。設計原則：
 *   - 危險操作兩段式確認（解鎖/上鎖/啟動/降落：再點一次才執行，3.5 秒逾時還原）
 *   - 緊急操作單擊即發（RTL/Hold——緊急時多一步確認是風險不是保護）
 *   - 失敗必須看得見：伺服器拒絕原因（ACK 逾時、機端拒絕、403 gate）原文顯示
 *   - ENABLE_COMMANDS 未開或服務未連線時如實顯示，不假裝可操作
 */
import { useEffect, useRef, useState } from "react";

import { API, COMMAND_API } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

interface Health {
  ok: boolean;
  enabled: boolean;
  drones: Record<string, { age_s: number; armed: boolean | null }>;
}
interface Mission { id: string; name: string }

export default function CommandPanel() {
  const [health, setHealth] = useState<Health | "off" | null>(null);
  const [sysid, setSysid] = useState<string | null>(null);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [missionId, setMissionId] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [open, setOpen] = useState(true);
  const [alt, setAlt] = useState(10);
  const live = useUavStore((s) => s.live);

  // 可拖曳：抓標題列移動，貼齊邊緣，位置記在 localStorage（重整不跑位）
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const posRef = useRef(pos);
  const dragRef = useRef<{ dx: number; dy: number; moved: boolean } | null>(null);
  useEffect(() => {
    try {
      const saved = localStorage.getItem("cmd-panel-pos");
      if (saved) setPos(JSON.parse(saved));
    } catch { /* 壞值就用預設位置 */ }
  }, []);
  useEffect(() => { posRef.current = pos; }, [pos]);

  function dragStart(e: React.PointerEvent) {
    const el = panelRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    dragRef.current = { dx: e.clientX - r.left, dy: e.clientY - r.top, moved: false };
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
  }
  function dragMove(e: React.PointerEvent) {
    const d = dragRef.current;
    const el = panelRef.current;
    if (!d || !el) return;
    const parent = el.offsetParent as HTMLElement | null;
    if (!parent) return;
    const pr = parent.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    let x = e.clientX - pr.left - d.dx;
    let y = e.clientY - pr.top - d.dy;
    x = Math.max(8, Math.min(x, pr.width - w - 8));
    y = Math.max(8, Math.min(y, pr.height - h - 8));
    if (x < 28) x = 8;                                // 貼齊邊緣
    if (pr.width - (x + w) < 28) x = pr.width - w - 8;
    if (y < 28) y = 8;
    if (pr.height - (y + h) < 28) y = pr.height - h - 8;
    d.moved = d.moved || Math.abs(x - (posRef.current?.x ?? -1)) > 4
      || Math.abs(y - (posRef.current?.y ?? -1)) > 4;
    setPos({ x, y });
  }
  function dragEnd() {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d) return;
    if (!d.moved) setOpen((o) => !o);                 // 沒拖動＝點擊：收合/展開
    else if (posRef.current) {
      localStorage.setItem("cmd-panel-pos", JSON.stringify(posRef.current));
    }
  }

  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try {
        const h = await (await fetch(`${COMMAND_API}/healthz`)).json();
        if (!stop) setHealth(h);
      } catch {
        if (!stop) setHealth("off");
      }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  useEffect(() => {
    fetch(`${API}/api/missions`).then((r) => r.json())
      .then((ms: Mission[]) => setMissions(ms)).catch(() => {});
  }, []);

  if (health === null) return null;
  if (health === "off") return null;           // 服務未部署/未連線：不佔版面

  const sysids = Object.keys(health.drones);
  const sid = sysid && sysids.includes(sysid) ? sysid : sysids[0] ?? null;
  const armed = sid ? health.drones[sid].armed : null;

  async function exec(action: string, path: string, needsConfirm = false,
                      payload?: Record<string, unknown>) {
    if (needsConfirm && confirm !== action) {
      setConfirm(action);
      if (confirmTimer.current) clearTimeout(confirmTimer.current);
      confirmTimer.current = setTimeout(() => setConfirm(null), 3500);
      return;
    }
    setConfirm(null);
    setBusy(action);
    setResult(null);
    try {
      const res = await fetch(`${COMMAND_API}/api/command/${sid}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload ? JSON.stringify(payload) : undefined,
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        // detail 可能是字串或結構化報告（預檢 problems／機端拒絕＋PX4 原因文字）
        const d = body.detail;
        const notes = d?.px4_notes?.length ? `｜PX4：${d.px4_notes.join("；")}` : "";
        const text = typeof d === "string" ? d
          : d?.problems?.length ? `${d.msg ?? "被拒"}：${d.problems.join("；")}`
          : d?.msg ? `${d.msg}${d.hint ? `——${d.hint}` : ""}${notes}`
          : JSON.stringify(d ?? `失敗（HTTP ${res.status}）`);
        setResult({ ok: false, text });
      } else {
        setResult({
          ok: true,
          text: `${action} ✓${body.verified ? "（回讀比對通過）" : ""}`,
        });
      }
    } catch (e) {
      setResult({ ok: false, text: `連線失敗：${e}` });
    }
    setBusy(null);
  }

  const btn = (action: string, label: string, path: string,
               opts: { confirm?: boolean; danger?: boolean; disabled?: boolean;
                       body?: Record<string, unknown> } = {}) => (
    <button
      className={opts.danger ? "btn-danger btn-sm" : "btn-plain btn-sm"}
      disabled={!sid || busy !== null || opts.disabled}
      onClick={() => exec(action, path, opts.confirm, opts.body)}
    >
      {busy === action ? "⋯" : confirm === action ? `確認${label}？` : label}
    </button>
  );

  return (
    <div className="cmd-panel" ref={panelRef}
      style={pos ? { left: pos.x, top: pos.y, bottom: "auto" } : undefined}>
      <div className="cmd-head" title="拖曳移動；點擊收合"
        onPointerDown={dragStart} onPointerMove={dragMove}
        onPointerUp={dragEnd} onPointerCancel={dragEnd}>
        <span className="name">任務控制</span>
        {!health.enabled && <span className="meta">未啟用</span>}
        {health.enabled && sid && (
          <span className="meta">
            sysid {sid} · {armed ? "已解鎖" : "待機"} · {live?.flight_mode ?? "—"}
          </span>
        )}
        {health.enabled && !sid && <span className="meta">未看到機</span>}
        <span className="spacer" />
        <span className="meta">{open ? "▾" : "▸"}</span>
      </div>

      {open && !health.enabled && (
        <div className="cmd-body">
          <p className="hint-line">
            指令能力未啟用：地面站 .env 設 <code>ENABLE_COMMANDS=true</code> 後
            <code> docker compose up -d</code>（安全 gate，預設只觀察不指揮）。
          </p>
        </div>
      )}

      {open && health.enabled && (
        <div className="cmd-body">
          {live && (
            <div className={`cmd-ready ${live.ready ? "ok" : "warn"}`}>
              {live.ready
                ? <>✅ Ready to fly（PX4 預檢通過{live.landed_state === "in_air" ? "·飛行中" : ""}）</>
                : <>
                    ❌ 未就緒{live.mav_state ? `（${live.mav_state}）` : ""}
                    {(live.not_ready_reasons ?? []).map((r, i) => (
                      <div className="hint-line" key={i}>· {r}</div>
                    ))}
                  </>}
            </div>
          )}
          {live && (
            <div className="hint-line">
              GPS fix {live.gps_fix ?? "—"}（需≥3）·
              衛星 {live.satellites ?? "—"} 顆 · 電量 {live.battery_pct ?? "—"}%
              {live.landed_state ? ` · ${live.landed_state}` : ""}
            </div>
          )}
          {sysids.length > 1 && (
            <div className="cmd-row">
              {sysids.map((s) => (
                <button key={s}
                  className={`btn-plain btn-sm ${s === sid ? "chip-on" : ""}`}
                  onClick={() => setSysid(s)}>sysid {s}</button>
              ))}
            </div>
          )}

          <div className="cmd-row">
            <select value={missionId} onChange={(e) => setMissionId(e.target.value)}>
              <option value="">選擇任務⋯</option>
              {missions.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
            {btn("上傳", "上傳到機", "/mission/upload",
                 { disabled: !missionId, body: { mission_id: missionId } })}
          </div>

          {/* 起飛→任務：實戰教訓——地面直接啟動任務會失敗，須先到高度。
              一鍵序列：解鎖→起飛→等高度到達→切 MISSION */}
          <div className="cmd-row">
            <label className="cmd-alt">高度
              <input type="number" min={3} max={100} step={1} value={alt}
                onChange={(e) => setAlt(Number(e.target.value) || 10)} /> m
            </label>
            {btn("起飛", "起飛", "/takeoff", { confirm: true, body: { alt } })}
            {btn("起飛→任務", "起飛→任務", "/mission/fly",
                 { confirm: true,
                   body: { mission_id: missionId || undefined, takeoff_alt: alt } })}
          </div>

          <div className="cmd-row">
            {armed
              ? btn("上鎖", "上鎖", "/disarm", { confirm: true, danger: true })
              : btn("解鎖", "解鎖", "/arm", { confirm: true })}
            {btn("啟動任務", "啟動任務（已在空中時）", "/mission/start", { confirm: true })}
          </div>

          <div className="cmd-row cmd-emergency">
            {btn("RTL", "RTL 返航", "/mode/rtl", { danger: true })}
            {btn("Hold", "Hold 懸停", "/mode/hold")}
            {btn("降落", "原地降落", "/mode/land", { confirm: true, danger: true })}
          </div>

          {result && (
            <div className={`cmd-result ${result.ok ? "ok" : "err"}`}>{result.text}</div>
          )}
        </div>
      )}
    </div>
  );
}
