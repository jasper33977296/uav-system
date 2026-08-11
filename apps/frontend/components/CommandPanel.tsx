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

import { colorFor } from "@/components/droneLayer";
import ManualControl from "@/components/ManualControl";
import { API, COMMAND_API } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

/** 能力四態（doc/capability-ui-proposal.md，issue 015）：按鈕由每機
 * capabilities descriptor 驅動，前端不寫死機型行為。
 * capabilities 是 command 服務端 gating 的同一真相——UI 與實際放行永不背離。 */
type CapState = "ok" | "unverified" | "unsupported";
const CAP_KEYS = ["arm", "takeoff", "land", "rtl", "hold",
  "mission_upload", "mission_start", "mission_fly", "manual"] as const;
type CapKey = (typeof CAP_KEYS)[number];
const CAP_LABELS: Record<CapKey, string> = {
  arm: "解鎖", takeoff: "起飛", land: "降落", rtl: "RTL", hold: "Hold",
  mission_upload: "上傳", mission_start: "啟動任務", mission_fly: "起飛→任務",
  manual: "手動",
};
const AP_LABELS: Record<string, string> = { px4: "PX4", ardupilot: "ArduPilot" };

interface DroneHealth {
  age_s: number;
  armed: boolean | null;
  autopilot?: string;                 // "px4" | "ardupilot" | "unknown"（字串枚舉）
  vehicle_type?: string;              // 選配（ArduCopter/ArduPlane…）
  capabilities?: Partial<Record<CapKey, CapState>>;
  capability_reasons?: Partial<Record<CapKey, string>>;   // 僅非 ok 鍵
}
interface Health {
  ok: boolean;
  enabled: boolean;
  drones: Record<string, DroneHealth>;
}
interface Mission { id: string; name: string }

export default function CommandPanel() {
  const [health, setHealth] = useState<Health | "off" | null>(null);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [missionId, setMissionId] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [open, setOpen] = useState(false);   // 預設收合（使用者 2026-08-11 指示）
  // toast「點這裡看原因」喚起（ui-spec §2.4）
  const cmdOpenReq = useUavStore((s) => s.cmdOpenReq);
  useEffect(() => { if (cmdOpenReq) setOpen(true); }, [cmdOpenReq]);
  const [alt, setAltState] = useState(10);
  useEffect(() => {
    const saved = Number(localStorage.getItem("takeoff-alt"));
    if (saved >= 3 && saved <= 100) setAltState(saved);
  }, []);
  const setAlt = (v: number) => {
    setAltState(v);
    if (v >= 3 && v <= 100) localStorage.setItem("takeoff-alt", String(v));
  };
  const live = useUavStore((s) => s.live);
  // 013-A 編隊：targetIds（指揮）疊在選中機（看）之上
  const formation = useUavStore((s) => s.formation);
  const targetIds = useUavStore((s) => s.targetIds);
  const cfg = useUavStore((s) => s.formationCfg);
  const fleet = useUavStore((s) => s.fleet);
  const selectedId = useUavStore((s) => s.selectedId);
  const primaryId = useUavStore((s) => s.primaryId);
  const focusId = selectedId ?? primaryId;
  const draftGroup = useUavStore((s) => s.draftGroup);
  const [groupBusy, setGroupBusy] = useState(false);
  // draft 失效＝連伺服器端一起清（07260a6 的 DELETE，限 draft；409 不理）——
  // 使用者反覆調整不在 DB 堆孤兒群組
  const discardDraft = () => {
    const st = useUavStore.getState();
    if (!st.draftGroup) return;
    fetch(`${API}/api/groups/${st.draftGroup.id}`, { method: "DELETE" }).catch(() => {});
    st.setDraftGroup(null);
  };
  // 設定/成員變更 → draft 失效（預覽退回前端試算，需重新預檢）
  const cfgKey = `${cfg.mode}|${cfg.base}|${cfg.spacing}|`
    + `${targetIds.join(",")}|${targetIds.map((id) => cfg.assign[id] ?? "").join(",")}`;
  useEffect(() => {
    discardDraft();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfgKey]);

  // 013-B 前半：建群組＋預檢（POST /api/groups，backend 50c3c1a 契約）
  async function createGroup() {
    setGroupBusy(true);
    setResult(null);
    discardDraft();   // 重新預檢＝舊 draft 作廢（伺服器端一併清）
    try {
      const res = await fetch(`${API}/api/groups`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: `編隊 ${new Date().toLocaleTimeString("zh-TW",
            { hour: "2-digit", minute: "2-digit", hour12: false })}`,
          mode: cfg.mode,
          base_mission_id: cfg.mode === "unified" ? cfg.base : undefined,
          drones: targetIds.map((id, i) => ({
            drone_id: id,
            layer_index: i,
            mission_id: cfg.mode === "separate" ? cfg.assign[id] : undefined,
          })),
          params: { vsep_m: cfg.spacing },
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const d = body.detail;
        setResult({ ok: false, text: typeof d === "string" ? d : `建立失敗（${res.status}）` });
      } else {
        useUavStore.getState().setDraftGroup({
          id: body.group_id, name: body.name, mode: body.mode,
          conflictOk: body.conflict?.ok ?? true,
          conflicts: body.conflict?.conflicts ?? [],
          assignments: body.assignments ?? [],
        });
        setResult({ ok: true, text: `群組已建立（${body.name}）✓ 預檢如下` });
      }
    } catch (e) {
      setResult({ ok: false, text: `連線失敗：${e}` });
    }
    setGroupBusy(false);
  }

  // 可拖曳：抓標題列移動，貼齊邊緣，位置記在 localStorage（重整不跑位）
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const posRef = useRef(pos);
  const dragRef = useRef<{ dx: number; dy: number; moved: boolean } | null>(null);
  useEffect(() => {
    try {
      // pos2：座標系從 map-wrap 相對改視口 fixed（批 2a blocker），換 key
      // 讓舊值自然失效
      const saved = localStorage.getItem("cmd-panel-pos2");
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
    // 視口座標（拖走後 position:fixed）：面板平時住 top-stack flex 流，
    // 不能再以 offsetParent 定位（批 2a blocker 修正配套）
    const vw = window.innerWidth, vh = window.innerHeight;
    const w = el.offsetWidth, h = el.offsetHeight;
    let x = e.clientX - d.dx;
    let y = e.clientY - d.dy;
    x = Math.max(8, Math.min(x, vw - w - 8));
    y = Math.max(56, Math.min(y, vh - h - 8));        // 上界避開導覽列
    if (x < 28) x = 8;                                // 貼齊邊緣
    if (vw - (x + w) < 28) x = vw - w - 8;
    if (y < 84) y = 56;
    if (vh - (y + h) < 28) y = vh - h - 8;
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
      localStorage.setItem("cmd-panel-pos2", JSON.stringify(posRef.current));
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

  // 選中機統一（PM 案，後端定案 ca0a472）：sysid 由選中機的 WS 遙測即時
  // 導出——側欄/地圖色點選誰，這裡就指揮誰，遙測與指令對象永遠同一台。
  // WS 的 mav_sysid 是「當下事實」（DB 的持久值斷線後會漂移，不用）；
  // null＝非 MAVLink 機＝無指令通道
  const sid = live?.mav_sysid != null ? String(live.mav_sysid) : null;
  const dh = sid ? health.drones[sid] ?? null : null;
  const armed = dh?.armed ?? null;
  const noChannel = !!live && live.mav_sysid == null;
  const unseen = !!sid && !dh;      // 有 sysid 但 command 服務還沒看到心跳

  // 四態推導：capabilities 缺席＝舊後端 → 退回現行全功能（feature-detect，
  // 前後端可獨立部署）；不在 healthz.drones 的機（無心跳）面板本來就不出現
  const caps = dh?.capabilities ?? null;
  const capState = (k: CapKey): CapState => (caps ? caps[k] ?? "unsupported" : "ok");
  const capReason = (k: CapKey) =>
    dh?.capability_reasons?.[k] ??
    (capState(k) === "unverified" ? "本機型尚未驗證" : "本機型不支援");
  // 僅觀察＝零 action 可用：整個指令區換成鎖定橫幅（含緊急鈕，PM 定案——
  // 會誤觸危險模式的 RTL 比沒有 RTL 更危險）
  const observeOnly = caps !== null && CAP_KEYS.every((k) => capState(k) !== "ok");
  const allUnsupported = caps !== null && CAP_KEYS.every((k) => capState(k) === "unsupported");
  const apLabel = dh?.autopilot ? AP_LABELS[dh.autopilot] ?? "未知機型" : null;
  // 受限態的逐鈕原因行（沿 not_ready_reasons 視覺語言，不用 tooltip）
  const capHints = (keys: CapKey[]) =>
    caps && !observeOnly
      ? keys.filter((k) => capState(k) !== "ok").map((k) => (
          <div className="hint-line" key={k}>· {CAP_LABELS[k]}：{capReason(k)}</div>
        ))
      : null;

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
      if (!res.ok && path === "/takeoff") {
        useUavStore.getState().noticeTakeoffDenied();   // HUD toast 用
      }
      if (!res.ok) {
        // detail 可能是字串或結構化報告（預檢 problems／機端拒絕＋自駕儀原因
        // 文字／非 PX4 機的 501 飛安 guard {msg, autopilot, hint}）。
        // autopilot_notes 為新名、px4_notes 為舊名：雙讀一版，後端改名後移除舊讀
        const d = body.detail;
        const noteList = d?.autopilot_notes ?? d?.px4_notes;
        const notes = noteList?.length ? `｜自駕儀：${noteList.join("；")}` : "";
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
                       body?: Record<string, unknown>; cap?: CapKey;
                       accent?: boolean } = {}) => (
    <button
      className={opts.danger ? "btn-danger btn-sm"
        : opts.accent ? "btn-accent btn-sm" : "btn-plain btn-sm"}
      disabled={!sid || busy !== null || !!opts.disabled
        || (opts.cap ? capState(opts.cap) !== "ok" : false)}
      onClick={() => exec(action, path, opts.confirm, opts.body)}
    >
      {busy === action ? "⋯" : confirm === action ? `確認${label}？` : label}
    </button>
  );

  return (
    <div className={`cmd-panel ${open ? "" : "cmd-closed"}`} ref={panelRef}
      style={pos ? { position: "fixed", left: pos.x, top: pos.y, zIndex: 60 } : undefined}>
      <div className="cmd-head" title="拖曳移動；點擊收合"
        onPointerDown={dragStart} onPointerMove={dragMove}
        onPointerUp={dragEnd} onPointerCancel={dragEnd}>
        <span className="name">任務控制</span>
        {/* 首行＝就緒點＋主按鈕（ui-spec §2：主按鈕併入面板，HUD 不放） */}
        {health.enabled && sid && live && (
          <span className="dot" title={live.ready ? "就緒" : "未就緒"} style={{
            background: live.ready ? "var(--status-ok)" : "var(--status-serious)" }} />
        )}
        {!health.enabled && <span className="meta">未啟用</span>}
        {health.enabled && !live && <span className="meta">無遙測</span>}
        {health.enabled && noChannel && <span className="meta">無指令通道</span>}
        <span className="spacer" />
        {health.enabled && !formation && sid && dh && !observeOnly && (
          <span onPointerDown={(e) => e.stopPropagation()}
            onPointerUp={(e) => e.stopPropagation()}>
            {(live?.landed_state === "in_air" || (live?.alt_rel ?? 0) > 2)
              ? btn("RTL", "⌂ 返航", "/mode/rtl", { danger: true, cap: "rtl" })
              : btn("起飛", "↑ 起飛", "/takeoff",
                    { confirm: true, body: { alt }, cap: "takeoff", accent: true })}
          </span>
        )}
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
          {/* ── 013-A 編隊視圖（§2.5）：設定＋預覽先行，執行進度視圖等 013-B ── */}
          {formation && (() => {
            const members = Object.entries(fleet).filter(([, t]) => t.connected);
            // 風險擋在行動點：逐台原因（未驗證/低電/未就緒），伺服器端同步 gate
            const riskHints = targetIds.flatMap((id) => {
              const t = fleet[id];
              const name = t?.drone_name ?? id.slice(0, 6);
              if (!t || !t.connected) return [`${name}：已離線`];
              const out: string[] = [];
              const tsid = t.mav_sysid != null ? String(t.mav_sysid) : null;
              const tcaps = tsid ? health.drones[tsid]?.capabilities : undefined;
              if (tcaps && CAP_KEYS.some((k) => (tcaps[k] ?? "unsupported") !== "ok")) {
                out.push(`${name}：機型未驗證`);
              }
              if (t.battery_pct != null && t.battery_pct < 20) {
                out.push(`${name}：電量 ${Math.round(t.battery_pct)}%`);
              }
              if (t.ready === false) out.push(`${name}：未就緒`);
              return out;
            });
            return (<>
              <div className="cmd-status">
                <span className="st-target">編隊 · {targetIds.length} 台</span>
                <span className="spacer" />
                {/* A 段：群組指令服務（013-B）上線前如實停用，不假裝可飛 */}
                <button className="btn-accent btn-sm" disabled
                  title="群組指令服務上線後啟用（013-B）">↑ 全部起飛</button>
                <button className="btn-plain btn-sm" title="退出編隊模式"
                  onClick={() => {
                    discardDraft();
                    useUavStore.getState().setFormation(false);
                  }}>✕</button>
              </div>
              {/* 成員列：◎焦點（點地圖球切）、◉目標集（點這裡 toggle）；
                  無指令通道機不可勾（物理上發不了指令），其餘可勾＋狀態環預警 */}
              <div className="cmd-row">
                {members.map(([id, t]) => {
                  const noCh = t.mav_sysid == null;
                  const risky = riskHints.some((h) =>
                    h.startsWith(t.drone_name ?? id.slice(0, 6)));
                  return (
                    <button key={id} disabled={noCh}
                      title={noCh ? "無指令通道（非 MAVLink）" : t.drone_name ?? id}
                      className={`member ${targetIds.includes(id) ? "tgt" : ""}`
                        + ` ${id === focusId ? "focus" : ""} ${risky ? "warn" : ""}`}
                      onClick={() => useUavStore.getState().toggleTarget(id)}>
                      <span className="dot" style={{ background: colorFor(id) }} />
                      {t.drone_name ?? id.slice(0, 6)}
                    </button>
                  );
                })}
              </div>
              <div className="cmd-row">
                <div className="seg">
                  <button className={cfg.mode === "unified" ? "on" : ""}
                    onClick={() => useUavStore.getState().setFormationCfg({ mode: "unified" })}>
                    同一路徑
                  </button>
                  <button className={cfg.mode === "separate" ? "on" : ""}
                    onClick={() => useUavStore.getState().setFormationCfg({ mode: "separate" })}>
                    各自路徑
                  </button>
                </div>
              </div>
              {cfg.mode === "unified" ? (
                <div className="cmd-row">
                  <select value={cfg.base}
                    onChange={(e) => useUavStore.getState()
                      .setFormationCfg({ base: e.target.value })}>
                    <option value="">選擇任務⋯</option>
                    {missions.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                  </select>
                  <label className="cmd-alt">層距
                    <input type="number" min={2} max={20} step={1} value={cfg.spacing}
                      onChange={(e) => useUavStore.getState()
                        .setFormationCfg({ spacing: Number(e.target.value) || 5 })} /> m
                  </label>
                </div>
              ) : (
                targetIds.map((id) => (
                  <div className="cmd-row" key={id}>
                    <span className="dot" style={{ background: colorFor(id) }} />
                    <select value={cfg.assign[id] ?? ""}
                      onChange={(e) => useUavStore.getState()
                        .setFormationCfg({ assign: { ...cfg.assign, [id]: e.target.value } })}>
                      <option value="">選擇任務⋯</option>
                      {missions.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                    </select>
                  </div>
                ))
              )}
              {riskHints.map((h, i) => (
                <div className="hint-line" key={i}>· {h}</div>
              ))}
              {/* 先看再執行：建群組→後端展開＋conflict 預檢（013-B 前半） */}
              <div className="cmd-row">
                <button className="btn-plain btn-sm"
                  disabled={groupBusy || targetIds.length < 2
                    || (cfg.mode === "unified" ? !cfg.base
                        : targetIds.some((id) => !cfg.assign[id]))}
                  onClick={createGroup}>
                  {groupBusy ? "⋯" : draftGroup ? "重新預檢" : "建立群組＋預檢"}
                </button>
                {draftGroup && <span className="hint-line">{draftGroup.name} · draft</span>}
              </div>
              {draftGroup && (<>
                {draftGroup.conflictOk ? (
                  <div className="cmd-ready ok">✅ 路徑分離足夠，無衝突</div>
                ) : (
                  draftGroup.conflicts.map((c, i) => {
                    // 衝突句用機名不用內部層編號（驗收微調）：L0/L1、
                    // drone_id、sysid 三種標籤形式都映射
                    const who = (label: string) => {
                      const byLayer = /^L(\d+)$/i.exec(label);
                      const hit = byLayer
                        ? draftGroup.assignments.find(
                            (x) => x.layer_index === Number(byLayer[1]))
                        : draftGroup.assignments.find(
                            (x) => x.drone_id === label
                              || String(x.mav_sysid ?? "") === label);
                      return hit?.drone_name ?? label;
                    };
                    return (
                      <div className="cmd-ready warn" key={i}>
                        ⚠ {who(c.a)} × {who(c.b)}：{c.why}
                      </div>
                    );
                  })
                )}
                {draftGroup.assignments.map((a) => (
                  <div className="hint-line" key={a.drone_id}>
                    · {a.drone_name ?? a.drone_id.slice(0, 6)}
                    　layer {a.layer_index}
                    {a.mav_sysid != null ? `　sysid ${a.mav_sysid}` : ""}
                  </div>
                ))}
              </>)}
              <div className="cmd-sec">手動</div>
              <p className="hint-line">編隊模式中停用</p>
            </>);
          })()}

          {!formation && (<>
          {/* 狀態：就緒/模式/GPS/電量一行看完；未就緒原因才逐條展開 */}
          {/* 明示對象機：遙測與指令按鈕永遠是同一台（選中機統一） */}
          <div className="cmd-status">
            <span className="st-target">
              {live?.drone_name ?? "—"}{sid ? `（sysid ${sid}）` : ""}
            </span>
            <span className={live?.ready ? "st-ok" : "st-warn"}>
              {live ? (live.ready ? "● 就緒" : "● 未就緒") : "● 無遙測"}
            </span>
            <span>{live?.flight_mode ?? "—"}</span>
            <span>GPS {live?.gps_fix ?? "—"} · {live?.satellites ?? "—"}顆</span>
            <span>電量 {live?.battery_pct != null ? Math.round(live.battery_pct) : "—"}%</span>
            {/* 編隊入口（§2.5 漸進顯示）：≥2 機連線才出現，單機永遠看不到 */}
            {Object.values(fleet).filter((t) => t.connected).length >= 2 && (
              <button className="btn-plain btn-sm" title="進入編隊（多機）模式"
                onClick={() => useUavStore.getState().setFormation(true,
                  Object.entries(fleet)
                    .filter(([, t]) => t.connected && t.mav_sysid != null)
                    .map(([id]) => id))}>
                ⧉ 編隊
              </button>
            )}
          </div>
          {live && !live.ready && (live.not_ready_reasons ?? []).map((r, i) => (
            <div className="hint-line" key={i}>· {r}</div>
          ))}
          {/* sysid chips 移除（選中機統一）：換機＝左上機隊色點／側欄，
              全站單一「選中機」概念，不再有第二個選擇器 */}
          {noChannel && (
            <div className="hint-line">
              此機無指令通道（非 MAVLink 機）——遙測與紀錄不受影響。
            </div>
          )}
          {unseen && (
            <div className="hint-line">指令服務尚未看到此機（sysid {sid}）。</div>
          )}

          {/* 僅觀察（未驗證/不支援機型）：指令區整個換成鎖定橫幅——
              警告色而非紅色（是刻意保護，不是故障）；遙測照常 */}
          {observeOnly && (
            <div className="cmd-ready lock">
              ⚠ 此機型（{dh?.vehicle_type ?? apLabel ?? "未知"}）
              {allUnsupported
                ? "不支援現行指令集，指令已鎖定。"
                : "控制尚未驗證，指令已鎖定——現行指令集對本機型可能誤觸危險模式（詳 issues/015）。"}
              遙測與紀錄不受影響。
            </div>
          )}

          {!observeOnly && !noChannel && !unseen && !!dh && (<>
          <div className="cmd-sec">任務</div>
          <div className="cmd-row">
            <select value={missionId} onChange={(e) => setMissionId(e.target.value)}>
              <option value="">選擇任務⋯</option>
              {missions.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
            {btn("上傳", "上傳", "/mission/upload",
                 { disabled: !missionId, body: { mission_id: missionId },
                   cap: "mission_upload", accent: true })}
          </div>
          {/* 起飛→任務：實戰教訓——地面直接啟動任務會失敗，須先到高度。
              一鍵序列：解鎖→起飛→等高度到達→切 MISSION */}
          <div className="cmd-row">
            <label className="cmd-alt">高度
              <input type="number" min={3} max={100} step={1} value={alt}
                onChange={(e) => setAlt(Number(e.target.value) || 10)} /> m
            </label>
            {btn("起飛→任務", "起飛→任務", "/mission/fly",
                 { confirm: true, cap: "mission_fly",
                   body: { mission_id: missionId || undefined, takeoff_alt: alt } })}
            {btn("啟動任務", "啟動任務", "/mission/start",
                 { confirm: true, cap: "mission_start" })}
          </div>
          {capHints(["mission_upload", "mission_fly", "mission_start"])}

          {/* 起飛/返航住標題列主按鈕（單一住所），這裡只留其餘飛行操作 */}
          <div className="cmd-sec">飛行</div>
          <div className="cmd-row cmd-emergency">
            {armed
              ? btn("上鎖", "上鎖", "/disarm", { confirm: true, danger: true, cap: "arm" })
              : btn("解鎖", "解鎖", "/arm", { confirm: true, cap: "arm" })}
            {btn("Hold", "懸停", "/mode/hold", { cap: "hold" })}
            {btn("降落", "降落", "/mode/land", { confirm: true, danger: true, cap: "land" })}
          </div>
          {capHints(["arm", "takeoff", "rtl", "hold", "land"])}

          {/* 手動：虛擬搖桿（串流/deadman 邏輯在 ManualControl 內自理） */}
          <div className="cmd-sec">手動</div>
          <ManualControl sid={sid}
            lockedReason={caps && capState("manual") !== "ok" ? capReason("manual") : null} />
          </>)}
          </>)}

          {result && (
            <div className={`cmd-result ${result.ok ? "ok" : "err"}`}>{result.text}</div>
          )}
        </div>
      )}
    </div>
  );
}
