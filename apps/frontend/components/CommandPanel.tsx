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
import { modeLabel } from "@/lib/modeVerb";
import { API, CLIENT_HEADERS, COMMAND_API } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

/** 能力四態（doc/capability-ui-proposal.md，issue 015）：按鈕由每機
 * capabilities descriptor 驅動，前端不寫死機型行為。
 * capabilities 是 command 服務端 gating 的同一真相——UI 與實際放行永不背離。 */
type CapState = "ok" | "unverified" | "unsupported";
const CAP_KEYS = ["arm", "takeoff", "land", "rtl", "hold",
  "mission_upload", "mission_start", "mission_fly"] as const;
type CapKey = (typeof CAP_KEYS)[number];
const CAP_LABELS: Record<CapKey, string> = {
  arm: "解鎖", takeoff: "起飛", land: "降落", rtl: "RTL", hold: "Hold",
  mission_upload: "上傳", mission_start: "啟動任務", mission_fly: "起飛→任務",
};
const AP_LABELS: Record<string, string> = { px4: "PX4", ardupilot: "ArduPilot" };
// 意圖協定的動作 → 畫面上的說法（039 複裁 G 的補送清單用）。**照枚舉列，
// 不猜字串**：漏一個就顯示原文，比顯示一個猜錯的中文好
const INTENT_LABELS: Record<string, string> = {
  start_mission: "開始任務", pause: "中斷任務", resume: "繼續任務",
  change_route: "更換任務", rtl: "返航", land: "降落",
  abort: "中止（原地懸停）", disarm: "上鎖",
};

// 013-B 狀態對照（group-missions-design §7.1 → 畫面人話，照枚舉不猜字串）
const GROUP_STATUS: Record<string, string> = {
  executing: "編隊起飛中…", flying: "✓ 編隊飛行中", gate_rejected: "起飛前檢查未過",
  aborting: "撤銷中…", aborted: "已全撤", partial: "部分完成",
  pending_approval: "等待現場確認", draft: "草稿", completed: "已完成",
};
const PHASE_TXT: Record<string, string> = {
  idle: "待命", uploading: "上傳路徑中…", uploaded: "路徑已上傳",
  arming: "解鎖中…", armed: "已解鎖", starting: "啟動中…", flying: "✓ 飛行中",
  landed: "已降落", upload_failed: "✗ 路徑上傳失敗", prearm_failed: "✗ 起飛檢查未過",
  rejected: "✗ 機端拒絕", rtl: "返航中",
};

interface DroneHealth {
  age_s: number;
  armed: boolean | null;
  autopilot?: string;                 // "px4" | "ardupilot" | "unknown"（字串枚舉）
  vehicle_type?: string;              // 選配（ArduCopter/ArduPlane…）
  capabilities?: Partial<Record<CapKey, CapState>>;
  capability_reasons?: Partial<Record<CapKey, string>>;   // 僅非 ok 鍵
}
interface Health {
  //: **`ok` 講的是「還能不能指揮飛機」**，不是「HTTP 層還活著」（issue 034）。
  //: router 執行緒死掉或卡住時它是 false，而 healthz 同時回 503——但 `fetch`
  //: 不會因為 503 拋錯，所以**不讀這個欄位就等於沒有這道防線**
  ok: boolean;
  detail?: string;          // ok=false 時服務給的人話說明
  enabled: boolean;
  drones: Record<string, DroneHealth>;
}
interface Mission {
  id: string; name: string;
  firmware_type?: number | null; vehicle_type?: number | null;
}

/** MAV_AUTOPILOT／MAV_TYPE → 人話。與 app/missions/page.tsx 同一組對照。
 * **認不得的值原樣顯示 id**，不寫「未知」——那會讓「檔案沒說」與「說了但
 * 我們沒收錄這個型號」看起來一樣。 */
const AP_NAMES: Record<number, string> = { 0: "通用", 3: "ArduPilot", 12: "PX4" };
const VT_NAMES: Record<number, string> = {
  1: "定翼", 2: "四旋翼", 10: "地面載具", 12: "潛航器", 13: "六旋翼", 14: "八旋翼",
};

/** 下拉選單裡的目標機種。**選的當下就要看得到這份航線是給誰寫的**——
 * 等上傳完才在卡片上顯示已經太晚：那時候航線已經在機上了。
 * 沒宣告時明講「未宣告」而不留白（issues/037 二修的同一條理由：留白會被
 * 讀成「還沒載入」）。 */
function missionLabel(m: Mission): string {
  const ap = m.firmware_type == null ? null
    : (AP_NAMES[m.firmware_type] ?? `firmware ${m.firmware_type}`);
  const vt = m.vehicle_type == null ? null
    : (VT_NAMES[m.vehicle_type] ?? `type ${m.vehicle_type}`);
  const t = [ap, vt].filter(Boolean).join(" · ");
  return `${m.name}（${t || "未宣告目標機種"}）`;
}

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
  // 意圖通道鏡像：RC 連線（複裁 A）與失聯期間的補送清單（複裁 G）都從這裡來
  const agentsMap = useUavStore((s) => s.agents);
  const replays = useUavStore((s) => s.replays);
  const clearReplays = useUavStore((s) => s.clearReplays);
  const draftGroup = useUavStore((s) => s.draftGroup);
  const [groupBusy, setGroupBusy] = useState(false);
  // draft 失效＝連伺服器端一起清（07260a6 的 DELETE，限 draft；409 不理）——
  // 使用者反覆調整不在 DB 堆孤兒群組
  const discardDraft = (reason = "?") => {
    const st = useUavStore.getState();
    if (st.execArmedUntil || st.draftGroup) {
      console.debug("[013b] window/draft cleared", reason);   // rig 取證：清窗來源
    }
    st.setExecArmedUntil(0);   // 舊確認窗不得延用到新 draft（安全邊角）
    if (!st.draftGroup) return;
    fetch(`${API}/api/groups/${st.draftGroup.id}`,
      { method: "DELETE", headers: CLIENT_HEADERS }).catch(() => {});
    st.setDraftGroup(null);
  };
  // 設定/成員變更 → draft 失效（預覽退回前端試算，需重新預檢）
  const cfgKey = `${cfg.mode}|${cfg.base}|${cfg.spacing}|`
    + `${targetIds.join(",")}|${targetIds.map((id) => cfg.assign[id] ?? "").join(",")}`;
  useEffect(() => {
    discardDraft(`cfgKey=${cfgKey.slice(0, 40)}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfgKey]);

  // ── 013-B 執行段（ca6d658 契約）────────────────────────────
  interface RunAssign {
    drone_id: string; phase: string; layer_index: number;
    drone_name?: string;
    error?: { msg?: string; hint?: string; autopilot_notes?: string[] } | null;
  }
  const [groupRun, setGroupRun] = useState<{
    id: string; status: string; assignments: RunAssign[] } | null>(null);
  const [gateRejects, setGateRejects] = useState<
    { drone_name?: string; reason?: string; hint?: string }[] | null>(null);
  const [execConfirm, setExecConfirm] = useState(false);
  const execTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [abortBusy, setAbortBusy] = useState(false);

  async function executeGroup() {
    if (!draftGroup) return;
    // 兩段式（群組級一顆鈕）：armed-until 存 store、**呼叫當下讀取**——
    // 不經 closure/本地 state，任何 re-render 或重掛都清不掉確認窗
    const st = useUavStore.getState();
    const now = Date.now();
    // rig 取證：進場快照——until=0 表示窗被清或 setter 沒寫進（store 分裂）
    console.debug("[013b] exec entry", {
      until: st.execArmedUntil, dt: st.execArmedUntil - now, draft: draftGroup.id });
    if (now > st.execArmedUntil) {
      try {
        console.debug("[013b] confirm armed");
        st.setExecArmedUntil(now + 3500);
        // rig 取證：寫後回讀——寫不進＝store 分裂；拋錯＝EXC 行現形
        console.debug("[013b] armed verify",
          useUavStore.getState().execArmedUntil - now);
        setExecConfirm(true);                  // 純視覺（變紅）
        if (execTimer.current) clearTimeout(execTimer.current);
        execTimer.current = setTimeout(() => setExecConfirm(false), 3500);
      } catch (err) {
        console.debug("[013b] EXC in arm", err);
      }
      return;
    }
    console.debug("[013b] confirm fired");     // rig 取證：第二擊送出
    st.setExecArmedUntil(0);
    setExecConfirm(false);
    setGroupBusy(true);
    setGateRejects(null);
    setResult(null);
    try {
      const res = await fetch(
        `${COMMAND_API}/api/command/group/${draftGroup.id}/execute`,
        { method: "POST", headers: CLIENT_HEADERS });
      const body = await res.json().catch(() => ({}));
      console.debug("[013b] execute resp", res.status, draftGroup.id);   // rig 取證
      if (res.status === 202) {
        // 序列已交伺服器（斷線不影響；中止只能按 abort）——進度視圖以
        // store 的 runGroupId 為準（重掛自癒），本地只是資料快取
        useUavStore.getState().setRunGroupId(draftGroup.id);
        setGroupRun({
          id: draftGroup.id, status: "executing",
          assignments: draftGroup.assignments.map((a) => ({ ...a, phase: "idle" })),
        });
      } else if (res.status === 409) {
        // gate 擋＝序列未啟動：逐台原因原文列出，留在設定畫面
        setGateRejects(
          (body.detail?.members ?? []).filter((m: { ok: boolean }) => !m.ok));
        setResult({ ok: false, text: body.detail?.msg ?? "起飛前檢查未過" });
      } else {
        setResult({ ok: false, text: `執行失敗（HTTP ${res.status}）` });
      }
    } catch (e) {
      setResult({ ok: false, text: `連線失敗：${e}` });
    }
    setGroupBusy(false);
  }

  // 進度輪詢：1s 打 backend GET /api/groups/{id}（後端定案不用 WS）。
  // 以 store 的 runGroupId 驅動：即使本地快取遺失（重掛/重整）也會
  // 從輪詢重建視圖資料
  const runGroupId = useUavStore((s) => s.runGroupId);
  useEffect(() => {
    if (!runGroupId) { setGroupRun(null); return; }
    const t = setInterval(async () => {
      try {
        const r = await fetch(`${API}/api/groups/${runGroupId}`);
        if (!r.ok) return;
        const g = await r.json();
        setGroupRun({
          id: runGroupId, status: g.status,
          assignments: g.assignments ?? [],
        });
      } catch { /* 掉一拍下秒再試 */ }
    }, 1000);
    return () => clearInterval(t);
  }, [runGroupId]);

  async function abortGroup() {
    if (!groupRun) return;
    setAbortBusy(true);   // 緊急單擊、冪等（依 phase 伺服器自選 disarm/RTL）
    try {
      await fetch(`${COMMAND_API}/api/command/group/${groupRun.id}/abort`,
        { method: "POST", headers: CLIENT_HEADERS });
    } catch { /* 冪等，再按即可 */ }
    setAbortBusy(false);
  }

  // 013-B 前半：建群組＋預檢（POST /api/groups，backend 50c3c1a 契約）
  async function createGroup() {
    setGroupBusy(true);
    setResult(null);
    discardDraft();   // 重新預檢＝舊 draft 作廢（伺服器端一併清）
    try {
      const res = await fetch(`${API}/api/groups`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
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
      // 入列狀態（040 A2）。跟著 healthz 同一圈問，不另開輪詢——
      // 多一條輪詢就多一個會各自漂移的節奏
      // 直接從 store 取當下的 sysid：**不用 ref**——這個輪詢跑在 effect 裡，
      // 而 sid 是在元件本體後面才算出來的，用閉包捕捉會拿到第一次渲染那個值
      const s = useUavStore.getState().live?.mav_sysid;
      if (s == null) { if (!stop) setAdm(null); return; }
      try {
        const r = await fetch(`${API}/api/admission/${s}`);
        if (!stop) setAdm(r.ok ? await r.json() : null);
      } catch {
        if (!stop) setAdm(null);      // 問不到＝不知道，不是「未入列」
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

  // 機上現在載的是哪一份任務。**四個任務動作全部是相對於它的**——不知道
  // 現在載的是什麼，按哪一顆都是猜的。跟著遙測輪詢重取，因為上傳／改航線
  // 都會改變它
  // sid 是字串（給 URL 用），這裡比對的是數字欄位——**兩邊型別不同**，
  // 直接 === 永遠是 false 而且不會有任何錯誤訊息，只會安靜地永遠找不到
  const sysidNum = live?.mav_sysid ?? null;
  const [onboardId, setOnboardId] = useState<string | null>(null);
  useEffect(() => {
    if (sysidNum == null) return;
    let dead = false;
    const pull = () => fetch(`${API}/api/drones`).then((r) => r.json())
      .then((ds: { mav_sysid?: number | null; current_mission_id?: string | null }[]) => {
        if (dead) return;
        const d = ds.find((x) => x.mav_sysid === sysidNum);
        setOnboardId(d?.current_mission_id ?? null);
      }).catch(() => {});
    pull();
    const t = setInterval(pull, 5000);
    return () => { dead = true; clearInterval(t); };
  }, [sysidNum]);

  // ── 飛行中改航線（狀態機文件 §6.3）──────────────────────────
  // 兩段式：先取提案（**不動飛機**）給人看，人確認後才執行三步序列。
  // **hook 必須放在下面那兩個早退之前**：放在後面的話，health 還是 null 的
  // 那幾次 render 不會呼叫到它，等 health 一到 hook 數量就變了
  // → React #310「Rendered more hooks than during the previous render」，
  // 整個即時頁白畫面。2026-08-25 實際炸過一次
  const [proposal, setProposal] = useState<any>(null);
  //: 入列狀態（issues/040 A2）。**事先查、不要讓使用者用失敗去發現**
  //: （ui-spec §0.2c 條款 6）——身分不明時按鈕就不該是可按的。
  //: null＝還沒問到（不是「未入列」）：那兩者不能同形
  const [adm, setAdm] = useState<{ state: string; reason?: string } | null>(null);

  if (health === null) return null;
  if (health === "off") return null;           // 服務未部署/未連線：不佔版面

  // 選中機統一（PM 案，後端定案 ca0a472）：sysid 由選中機的 WS 遙測即時
  // 導出——側欄/地圖色點選誰，這裡就指揮誰，遙測與指令對象永遠同一台。
  // WS 的 mav_sysid 是「當下事實」（DB 的持久值斷線後會漂移，不用）；
  // null＝非 MAVLink 機＝無指令通道
  // issue 034：指令服務的 router 變殭屍——HTTP 還在回話，但心跳已停發、
  // 指令不會送達飛機。**這是最會騙人的一種失效**：遙測照樣流動（backend 直接
  // 收 MAVLink，與指令服務是兩條路），所以畫面上一切正常，只有這個欄位會說。
  // **嚴格比對 false**：舊版服務沒有這個欄位時是 undefined，那是「不知道」，
  // 不該把整個面板鎖掉
  const routerDead = health.ok === false;
  const sid = live?.mav_sysid != null ? String(live.mav_sysid) : null;
  const dh = sid ? health.drones[sid] ?? null : null;
  const armed = dh?.armed ?? null;
  // 039 複裁 A：**RC 未連線不得起飛、不得開始任務**。「機在地上失聯只告警」
  // 那格的前提是有人能用遙控器接管——沒有 RC 就沒有人。權威守門在機上代理，
  // 這裡把同一條規則畫成按不下去，免得人按了被擋卻不知道為什麼。
  // **`null` 不擋**：那是舊版代理還沒送這個欄位＝不知道，把「不知道」當成
  // 「沒有 RC」會讓所有還沒升級的機都起飛不了（issues/036 的同一個教訓）
  const agentHere = focusId ? agentsMap[focusId] ?? null : null;
  const rcDown = agentHere?.rc_link === false;
  // 040 A2：入列沒過就指不動。**`null` 不擋**——那是「還沒問到」，
  // 與「問到了、沒過」是兩件事（同 rc_link 的三態紀律）
  const notAdmitted = adm !== null && adm.state !== "admitted";
  const replayList = focusId ? replays[focusId] ?? [] : [];
  // ── 任務區要用的三個判斷（2026-08-26）───────────────────────
  // **「在空中」全檔案共用同一個判準**（landed_state 或高度）——兩套判準
  // 會在邊界上互相矛盾，而這裡決定的是「顯示哪一組按鈕」
  // **在空中：優先信飛控自己說的 `landed_state`**，高度只是它不回報時的退路。
  //
  // 2026-09-02 實測到的反例：一台**已上鎖、飛控回報 `on_ground`** 的機，
  // 因為沒有 GPS 定位（fix=1、0 顆衛星）而 `alt_rel` 漂到 4.4 m——舊的判準
  // `landed_state === "in_air" || alt_rel > 2` 因此判成「在天上」，
  // 於是畫面長出「更換任務」按鈕與「飛手可能拿著遙控器」那句，
  // **而飛控自己明明說它在地上**。
  //
  // 兩條規則：
  // 1. `landed_state` 有值就聽它——**推導值不該蓋過飛控直接說的話**。
  // 2. 退回高度時，**沒有定位就不用那個高度**：沒有位置解時 `alt_rel` 是漂的，
  //    不是高度。與 036 對 `0,0` 座標的處理是同一條規則——
  //    **不知道的東西不要畫成知道的東西**。
  const airborne = live?.landed_state
    ? live.landed_state === "in_air"
    : live?.lat != null && (live?.alt_rel ?? 0) > 2;
  // 模式判斷一律用 mode_verb（廠牌無關），不比對原廠模式名
  const inMission = live?.mode_verb === "mission";
  const holding = live?.mode_verb === "hold";
  // 機上現在載的是哪一份。**不知道就說不知道**：本系統沒上傳過的機（別的
  // GCS 傳的、或從機上讀回的）這個欄位是空的，那時候顯示任何名字都是猜的
  const onboardName = missions.find((m) => m.id === onboardId)?.name ?? null;
  const noChannel = !!live && live.mav_sysid == null;
  const unseen = !!sid && !dh;      // 有 sysid 但 command 服務還沒看到心跳

  // 四態推導：capabilities 缺席＝舊後端 → 退回現行全功能（feature-detect，
  // 前後端可獨立部署）；不在 healthz.drones 的機（無心跳）面板本來就不出現
  const caps = dh?.capabilities ?? null;
  const capState = (k: CapKey): CapState => (caps ? caps[k] ?? "unsupported" : "ok");
  // 原因句只對「認得的態」下斷言：後端日後新增態（例如「受限」——需機端
  // 設定才可用）時，不可沿用「本機型不支援」——那是把「有條件可用」說成
  // 「做不到」。未知態一律走中性句，鎖定行為照舊（安全方向）。
  const capReason = (k: CapKey) => {
    const st = capState(k);
    return dh?.capability_reasons?.[k]
      ?? (st === "unverified" ? "本機型尚未驗證"
        : st === "unsupported" ? "本機型不支援"
        : "此能力目前不可用");
  };
  // 僅觀察＝零 action 可用：整個指令區換成鎖定橫幅（含緊急鈕，PM 定案——
  // 會誤觸危險模式的 RTL 比沒有 RTL 更危險）
  const observeOnly = caps !== null && CAP_KEYS.every((k) => capState(k) !== "ok");
  const allUnsupported = caps !== null && CAP_KEYS.every((k) => capState(k) === "unsupported");
  const apLabel = dh?.autopilot ? AP_LABELS[dh.autopilot] ?? "未知機型" : null;
  // 混機＝在線機中有 ≥2 種 autopilot：只有此時模式名才需要語意註記
  const mixedFleet = new Set(Object.values(fleet)
    .filter((t) => t.connected && t.autopilot).map((t) => t.autopilot)).size >= 2;
  // 受限態的逐鈕原因行（沿 not_ready_reasons 視覺語言，不用 tooltip）
  const capHints = (keys: CapKey[]) =>
    caps && !observeOnly
      ? keys.filter((k) => capState(k) !== "ok").map((k) => (
          <div className="hint-line" key={k}>· {CAP_LABELS[k]}：{capReason(k)}</div>
        ))
      : null;

  async function proposeChangeRoute() {
    setBusy("改航線"); setResult(null);
    try {
      const res = await fetch(
        `${COMMAND_API}/api/command/${sid}/mission/change-route/proposal`,
        { method: "POST",
          headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
          body: JSON.stringify({ mission_id: missionId, hold_alt: alt }) });
      const p = await res.json();
      if (!res.ok) { setResult({ ok: false, text: p?.detail?.msg ?? "取不到提案" }); }
      else setProposal(p);
    } catch (e) {
      setResult({ ok: false, text: `連線失敗：${e}` });
    }
    setBusy(null);
  }

  async function runChangeRoute() {
    const p = proposal;
    setProposal(null); setBusy("改航線"); setResult(null);
    try {
      const res = await fetch(
        `${COMMAND_API}/api/command/${sid}/mission/change-route`,
        { method: "POST",
          headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
          // **送 intent_id，不是把提案送回去**（協定 §4.5）：提案留在機上，
          // 所以沒有「送回去的那份跟人看到的不一樣」的空間。代理收到確認後
          // 自己重算比對過期，守門也在那一刻再過一次
          body: JSON.stringify({ mission_id: missionId, hold_alt: alt,
                                 intent_id: p?.intent_id }) });
      const b = await res.json();
      if (!res.ok) {
        const d = b.detail ?? {};
        // 提案過期＝機體在人看提案時移動了。**不自作主張用新的執行**，
        // 把新提案再攤一次給人看（協定 §5.1）
        if (d.code === "proposal_drift" && d.proposal) setProposal(d.proposal);
        setResult({ ok: false, text: d.msg ?? "改航線失敗" });
      } else {
        setResult({ ok: true, text: "改航線完成（三步都讀回確認）" });
      }
    } catch (e) {
      setResult({ ok: false, text: `連線失敗：${e}` });
    }
    setBusy(null);
  }

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
        headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
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
        // **被擋下時一定要說得出合法做法**（狀態機文件 §3-A2）：空中上傳被擋
        // 是對的，但只說「不行」會逼人去找繞道，而繞道正是這道門要防的事
        const how = d?.how_to?.length
          ? `｜合法做法：${d.how_to.map((t: string, i: number) => `${i + 1}. ${t}`).join(" → ")}`
          : "";
        const text = typeof d === "string" ? d
          : d?.problems?.length ? `${d.msg ?? "被拒"}：${d.problems.join("；")}`
          : d?.msg ? `${d.msg}${d.hint ? `——${d.hint}` : ""}${notes}${how}`
          : JSON.stringify(d ?? `失敗（HTTP ${res.status}）`);
        setResult({ ok: false, text });
      } else {
        setResult({
          ok: true,
          text: `${action} ✓${body.verified ? "（回讀比對通過）" : ""}`,
        });
        // 顯示到即時頁的事**已經搬到後端**（指令服務在上傳／啟動／改航線成功
        // 後呼叫 /missions/{id}/show，前端由 mission_shown 事件觸發重畫）。
        // 原因：上傳的呼叫端不只有這個畫面——驗收 rig、MCP、curl 都會上傳，
        // 綁在按鈕上等於只有自己按的那次會更新，別人上傳時畫面就與飛機對不上。
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
      disabled={!sid || busy !== null || !!opts.disabled || notAdmitted
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
        {/* 收合態的就緒點同樣三分（§0.2b）：不知道＝灰空心，不可落成
            「未就緒」的橘實心（面板收合時這顆點是唯一的就緒訊息） */}
        {health.enabled && sid && live && (() => {
          const unknown = live.ready == null
            || (live.ready && live.prearm_ok == null && live.ekf_ok == null);
          return (
            <span className="dot"
              title={unknown ? "尚無足夠遙測——無法判定就緒"
                : live.ready ? "就緒" : "未就緒"}
              style={unknown
                ? { background: "transparent", border: "1.5px solid var(--muted)" }
                : { background: live.ready ? "var(--status-ok)" : "var(--status-serious)" }} />
          );
        })()}
        {!health.enabled && <span className="meta">未啟用</span>}
        {health.enabled && !live && <span className="meta">無遙測</span>}
        {health.enabled && noChannel && <span className="meta">無指令通道</span>}
        {routerDead && <span className="meta meta-dead">指令服務失效</span>}
        <span className="spacer" />
        {health.enabled && !routerDead && !formation && sid && dh && !observeOnly && (
          <span onPointerDown={(e) => e.stopPropagation()}
            onPointerUp={(e) => e.stopPropagation()}>
            {/* **用同一個 `airborne`**：原本這裡各寫一份判準，而
                「在空中」的判斷在邊界上分歧會讓標頭顯示返航、
                本體顯示起飛——同一個畫面上兩個互相矛盾的答案 */}
            {airborne
              ? btn("RTL", "⌂ 返航", "/mode/rtl", { danger: true, cap: "rtl" })
              : btn("起飛", "↑ 起飛", "/takeoff",
                    { confirm: true, body: { alt }, cap: "takeoff", accent: true,
                      disabled: rcDown })}
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

      {/* issue 034：router 殭屍時**不給可按的指令鈕**——按下去必然逾時，
          而「按了才發現」正是 ui-spec §0.2e 禁止的「失效冒充合法狀態」。
          擋在這裡而不是逐顆鈕 disable：失效的是整條指令通道，不是某個能力 */}
      {open && routerDead && (
        <div className="cmd-body">
          <div className="cmd-dead">
            <b>指令服務失效——現在指揮不了飛機。</b>
            <div className="hint-line">
              {health.detail
                ?? "MAVLink router 迴圈未在運轉：GCS 心跳已停發、指令不會送達。"}
            </div>
            <div className="hint-line">
              · 遙測可能仍然正常（那是另一條路），**畫面正常不代表指得動**
            </div>
            <div className="hint-line">
              · 機上仍有自己的 failsafe 與待命的實體遙控器；
              查 command 服務日誌後重啟服務
            </div>
          </div>
        </div>
      )}

      {/* 040 A2：入列沒過。**事先標示而不是等按下去才說**（ui-spec §0.2c
          條款 6）——而且原因要可行動：「未驗證」不是原因，「這台機沒有代理」
          才是。緊急退路（實體遙控器）不受影響，這句話要出現在畫面上，
          否則「指不動」會被讀成「沒救了」 */}
      {open && health.enabled && !routerDead && notAdmitted && (
        <div className="cmd-body">
          <div className="cmd-dead">
            <b>這台機還不能被指揮（{adm?.state}）。</b>
            <div className="hint-line">{adm?.reason ?? "入列檢查未通過"}</div>
            <div className="hint-line">
              · 本系統只指揮通過入列的機——身分不明時指令可能送到錯的飛機
            </div>
            <div className="hint-line">· **實體遙控器不受影響**</div>
          </div>
        </div>
      )}

      {open && health.enabled && !routerDead && !notAdmitted && (
        <div className="cmd-body">
          {/* 039 複裁 G：失聯期間按下的操作。**這不是「已經做了」的通知**，
              是「你按過、系統沒有送出去」的清單——鏈路恢復後只重新問了一次
              守門判決（乾跑，飛機沒有動）。要做的話人再按一次，走原本那條
              完整路徑：那則意圖附帶的幾何是斷線前算的，直接放出去等於拿一個
              過期的位置去指揮現在的飛機 */}
          {replayList.length > 0 && (
            <div className="cmd-replay">
              <div>
                <b>失聯期間你按了 {replayList.length} 個操作，系統沒有執行。</b>
                鏈路恢復後重新問過機上守門（乾跑，飛機沒有動）——要做請重新按一次。
              </div>
              {replayList.map((r) => (
                <div className="hint-line" key={r.intent_id}>
                  · {INTENT_LABELS[r.action] ?? r.action}
                  （{Math.round(r.age_s)} 秒前按下）：
                  {r.verdict === "guard_refused"
                    ? `現在會被守門擋下——${r.reason ?? "未說明原因"}`
                    : r.verdict === "unknown"
                      ? `問不到判決——${r.reason ?? "未說明原因"}`
                      : `守門現在放行（機端狀態 ${r.state ?? "未知"}）`}
                </div>
              ))}
              <button className="btn-plain btn-sm"
                onClick={() => focusId && clearReplays(focusId)}>知道了</button>
            </div>
          )}
          {/* ── 013-A 編隊視圖（§2.5）：設定＋預覽先行，執行進度視圖等 013-B ── */}
          {formation && (() => {
            const members = Object.entries(fleet).filter(([, t]) => t.connected);
            // 風險擋在行動點：逐台原因（未驗證/低電/未就緒），伺服器端同步 gate
            const riskHints = targetIds.flatMap((id) => {
              const t = fleet[id];
              const name = t?.drone_name || id.slice(0, 6);
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
              // 三分（§0.2b）：false＝知道不行；null＝不知道，同樣要說，
              // 但句子不可冒充「未就緒」（那是有依據的斷言）
              if (t.ready === false) out.push(`${name}：未就緒`);
              else if (t.ready == null) out.push(`${name}：尚無足夠遙測`);
              return out;
            });
            // ── 進度視圖（執行中接管面板；過程唯一互動＝中止）──
            // 條件用 store 的 runGroupId：一次性 setState 遺失也不會漏切視圖
            if (runGroupId) {
              const status = groupRun?.status ?? "executing";
              const running = ["executing", "flying", "aborting", "pending_approval"]
                .includes(status);
              return (<>
                <div className="cmd-status">
                  <span className="st-target">
                    {GROUP_STATUS[status] ?? status}
                  </span>
                  <span className="spacer" />
                  {running ? (
                    <button className="btn-danger btn-sm" disabled={abortBusy}
                      onClick={abortGroup}>{abortBusy ? "⋯" : "中止"}</button>
                  ) : (
                    <button className="btn-plain btn-sm"
                      onClick={() => {
                        useUavStore.getState().setRunGroupId(null);
                        setGroupRun(null);
                      }}>返回設定</button>
                  )}
                </div>
                {!groupRun && <div className="hint-line">狀態載入中…</div>}
                {(groupRun?.assignments ?? []).map((a) => (
                  <div className="run-row" key={a.drone_id}>
                    <span className="dot" style={{ background: colorFor(a.drone_id) }} />
                    <span>{a.drone_name || a.drone_id.slice(0, 6)}</span>
                    <span className="spacer" />
                    <span className={a.phase.includes("failed") || a.phase === "rejected"
                      ? "run-err" : "run-ok"}>
                      {PHASE_TXT[a.phase] ?? a.phase}
                    </span>
                  </div>
                ))}
                {/* 失敗必須看得見：結構化原因原文逐台列出 */}
                {(groupRun?.assignments ?? []).filter((a) => a.error?.msg).map((a) => (
                  <div className="cmd-result err" key={`e${a.drone_id}`}>
                    {a.drone_name || a.drone_id.slice(0, 6)}：{a.error!.msg}
                    {a.error!.hint ? `——${a.error!.hint}` : ""}
                    {a.error!.autopilot_notes?.length
                      ? `｜自駕儀：${a.error!.autopilot_notes.join("；")}` : ""}
                  </div>
                ))}
              </>);
            }
            return (<>
              <div className="cmd-status">
                <span className="st-target">編隊 · {targetIds.length} 台</span>
                <span className="spacer" />
                {/* 兩段式（群組級一顆鈕）：變紅「確定起飛？」；未預檢先 disabled */}
                <button
                  className={execConfirm ? "btn-danger btn-sm" : "btn-accent btn-sm"}
                  disabled={!draftGroup || groupBusy}
                  title={!draftGroup ? "先建立群組＋預檢" : undefined}
                  onClick={executeGroup}>
                  {groupBusy ? "⋯" : execConfirm ? "確定起飛？" : "↑ 全部起飛"}
                </button>
                <button className="btn-plain btn-sm" title="退出編隊模式"
                  onClick={() => {
                    discardDraft();
                    useUavStore.getState().setFormation(false);
                  }}>✕</button>
              </div>
              {/* gate 擋（409＝序列未啟動）：逐台原因原文 */}
              {gateRejects?.map((m, i) => (
                <div className="cmd-ready warn" key={i}>
                  ✗ {m.drone_name || "?"}：{m.reason ?? ""}
                  {m.hint ? `——${m.hint}` : ""}
                </div>
              ))}
              {/* 成員列：◎焦點（點地圖球切）、◉目標集（點這裡 toggle）；
                  無指令通道機不可勾（物理上發不了指令），其餘可勾＋狀態環預警 */}
              <div className="cmd-row">
                {members.map(([id, t]) => {
                  const noCh = t.mav_sysid == null;
                  const risky = riskHints.some((h) =>
                    h.startsWith(t.drone_name || id.slice(0, 6)));
                  return (
                    <button key={id} disabled={noCh}
                      title={noCh ? "無指令通道（非 MAVLink）" : t.drone_name || id}
                      className={`member ${targetIds.includes(id) ? "tgt" : ""}`
                        + ` ${id === focusId ? "focus" : ""} ${risky ? "warn" : ""}`}
                      onClick={() => useUavStore.getState().toggleTarget(id)}>
                      <span className="dot" style={{ background: colorFor(id) }} />
                      {t.drone_name || id.slice(0, 6)}
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
                    {missions.map((m) =>
                      <option key={m.id} value={m.id}>{missionLabel(m)}</option>)}
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
                      {missions.map((m) =>
                      <option key={m.id} value={m.id}>{missionLabel(m)}</option>)}
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
                      return hit?.drone_name || label;
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
                    · {a.drone_name || a.drone_id.slice(0, 6)}
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
              {live?.drone_name || "—"}{sid ? `（sysid ${sid}）` : ""}
            </span>
            {/* 機型 chip（§2.6 安置：身分歸身分——自抽屜專業數值卡移入） */}
            {apLabel && <span className="chip">{apLabel}</span>}
            {/* 就緒三分（ui-spec §0.2b）：知道行＝綠實心／知道不行＝黃實心＋
                原因／**不知道＝灰空心，不做斷言**。ready 為 null（或 true 但
                判斷依據 prearm_ok、ekf_ok 皆缺席）都屬「不知道」——把不知道
                顯示成「未就緒」是假裝知道，顯示成「就緒」更糟。
                「不知道」絕不用紅：會讓操作者把剛連上的正常機當故障處置。 */}
            {(() => {
              const unknown = !live || live.ready == null
                || (live.ready && live.prearm_ok == null && live.ekf_ok == null);
              if (unknown) {
                return (<span className="st-unk">
                  ○ {live ? "尚無足夠遙測" : "無遙測"}
                </span>);
              }
              return (<span className={live.ready ? "st-ok" : "st-warn"}>
                {live.ready ? "● 就緒" : "● 未就緒"}
              </span>);
            })()}
            {/* 模式顯示（§0.2d）：原廠名不翻譯；**混機時**才加語意括注
                （PX4 HOLD 與 ArduPilot LOITER 是同一件事，單一廠牌無歧義
                就不加字）。**要判斷模式請用 live.mode_verb，不得比對
                flight_mode 字串**——比字串在混機環境必錯 */}
            <span>{modeLabel(live?.flight_mode, live?.mode_verb, mixedFleet)}</span>
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
          {/* 原因行：未就緒（知道不行）與遙測不足（不知道）都要說明白——
              後端在 ready=null 時也帶原因句（「尚未收到 SYS_STATUS…」） */}
          {live && live.ready !== true && (live.not_ready_reasons ?? []).map((r, i) => (
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
          {/* ── 任務區（2026-08-26 重排）─────────────────────────────
              **按操作員想做的事分組，不是按端點分組。** 原本一排是
              上傳／起飛→任務／啟動任務、另一排是解鎖／懸停／降落——那是
              實作的形狀，不是「我現在要幹嘛」的形狀。四件事：
              上傳任務、開始任務、中斷任務、更換任務。

              而且**先講機上現在是哪一份**：這四個動作全部是相對於它的，
              不知道現在載的是什麼，按哪一顆都是猜的。 */}
          <div className="cmd-sec">任務</div>
          <div className="hint-line">
            機上目前：{onboardName
              ? <b>{onboardName}</b>
              : <span style={{ opacity: 0.6 }}>不知道（本系統沒上傳過，
                  或是別的 GCS 傳的）</span>}
            {inMission && "・執行中"}
            {holding && "・已暫停"}
          </div>
          <div className="cmd-row">
            <select value={missionId} onChange={(e) => setMissionId(e.target.value)}>
              <option value="">選擇任務⋯</option>
              {missions.map((m) =>
                      <option key={m.id} value={m.id}>{missionLabel(m)}</option>)}
            </select>
          </div>

          {/* 地面：上傳 → 開始。**空中不顯示上傳**——那是狀態機文件 §3-A
              列為最高優先的危害（上傳在空中是立即生效的航線變更），而且
              後端的守門也會擋。按了才知道不行，不如一開始就換成正確的入口 */}
          {!airborne && (
            <div className="cmd-row">
              {btn("上傳", "① 上傳到機", "/mission/upload",
                   { disabled: !missionId, body: { mission_id: missionId },
                     cap: "mission_upload", accent: true })}
              <label className="cmd-alt">起飛高度
                <input type="number" min={3} max={100} step={1} value={alt}
                  onChange={(e) => setAlt(Number(e.target.value) || 10)} /> m
              </label>
              {btn("起飛→任務", "② 開始任務（起飛→執行）", "/mission/fly",
                   { confirm: true, cap: "mission_fly", disabled: rcDown,
                     body: { mission_id: missionId || undefined, takeoff_alt: alt } })}
              {rcDown && (
                <div className="hint-line">
                  · 遙控器未連線——自動起飛的前提是有人能隨時接管，
                  請先確認遙控器開機並與飛控連上（039 複裁 A）
                </div>
              )}
            </div>
          )}

          {/* 空中：中斷／繼續／更換。**三顆按當下狀態亮**——飛行中不給
              「繼續」、暫停中不給「中斷」，那兩顆按下去只會被守門擋回來 */}
          {airborne && (
            <div className="cmd-row">
              {inMission && btn("中斷任務", "⏸ 中斷任務（原地懸停）",
                                "/mode/hold", { cap: "hold" })}
              {holding && btn("繼續任務", "▶ 繼續任務", "/mode/mission",
                              { confirm: true, cap: "mission_start" })}
              <button className="btn-plain btn-sm"
                disabled={!missionId || busy !== null}
                title="飛行中換一份航線：先看系統打算怎麼調整（暫停→上傳→從最近的航點續飛），確認後才執行"
                onClick={() => proposeChangeRoute()}>
                {busy === "改航線" ? "⋯" : "⇄ 更換任務⋯"}
              </button>
            </div>
          )}
          {airborne && !inMission && !holding && (
            <div className="hint-line">
              目前不在任務模式（{live?.flight_mode ?? "模式未知"}）——
              {/* **不要在 `rc_link === false` 時說「飛手可能拿著遙控器」**：
                  我們**知道**沒有人拿著（039 複裁 A 的同一個事實來源）。
                  說一句已知為假的話，比不說更糟——它會讓人以為有人接得了手 */}
              {agentHere?.rc_link === false
                ? "而且遙控器未連線：此刻沒有人接得了手。"
                : agentHere?.rc_link
                  ? "飛手可能拿著遙控器。"
                  : "遙控器狀態不明。"}
              系統此時只觀察不介入。
            </div>
          )}
          {capHints(["mission_upload", "mission_fly", "mission_start", "hold"])}

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

          </>)}
          </>)}

          {result && (
            <div className={`cmd-result ${result.ok ? "ok" : "err"}`}>{result.text}</div>
          )}
        </div>
      )}

      {/* **確認畫面不得只問「確定嗎？」**（狀態機文件 §6.3）——那種確認框沒有
          資訊，人只會照按。這裡逐項對應規格要求顯示的東西：現在在哪、續飛到
          哪一點、多遠、往哪個方向、會不會先爬升下降、以及這是可中止的三步。 */}
      {proposal && (
        <div className="modal-backdrop" onClick={() => setProposal(null)}>
          <div className="modal confirm-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <span className="name">飛行中改航線：系統打算這樣調整</span>
            </div>
            <div className="modal-text">
              <div className="hint-line">
                目前 {proposal.current?.lat != null
                  ? `${proposal.current.lat.toFixed(5)}, ${proposal.current.lon.toFixed(5)}`
                  : "位置未知"}
                {proposal.current?.alt_rel != null && ` · 高度 ${proposal.current.alt_rel.toFixed(1)} m`}
                {proposal.current?.mission_seq != null && ` · 正在飛第 ${proposal.current.mission_seq} 點`}
              </div>
              <div style={{ marginTop: 10, fontWeight: 600 }}>
                新航線「{proposal.mission_name}」
              </div>
              {proposal.resume_wp ? (
                <div className="hint-line" style={{ marginTop: 4 }}>
                  續飛航點：**第 {proposal.resume_wp.index} 點**
                  （{proposal.resume_wp.distance_m} m、方位 {proposal.resume_wp.bearing_deg}°
                  {proposal.resume_wp.alt_delta_m != null
                    && `、高度 ${proposal.resume_wp.alt_delta_m > 0 ? "+" : ""}${proposal.resume_wp.alt_delta_m} m`}）
                </div>
              ) : (
                <div className="form-err" style={{ marginTop: 4 }}>算不出續飛航點</div>
              )}
              <div className="hint-line" style={{ marginTop: 4 }}>
                暫停懸停高度 {proposal.hold?.alt ?? "維持當前"} m
                {proposal.hold?.alt_delta_m != null
                  && `（${proposal.hold.alt_delta_m > 0 ? "先爬升" : proposal.hold.alt_delta_m < 0 ? "先下降" : "不變"} ${Math.abs(proposal.hold.alt_delta_m)} m）`}
              </div>
              <ol style={{ marginTop: 10, paddingLeft: 20 }}>
                {(proposal.steps ?? []).map((t: string, i: number) =>
                  <li key={i} className="hint-line">{t}</li>)}
              </ol>
              <div className="hint-line" style={{ marginTop: 6 }}>
                每一步都會讀回機端確認；任何一步沒過就**停在懸停**，不會繼續飛。
              </div>
              {(proposal.warnings ?? []).map((w: string, i: number) =>
                <div key={i} className="form-err" style={{ marginTop: 6 }}>⚠ {w}</div>)}
              {(proposal.blockers ?? []).map((w: string, i: number) =>
                <div key={i} className="form-err" style={{ marginTop: 6 }}>✕ {w}</div>)}
            </div>
            <div className="modal-actions">
              <button className="btn-plain" onClick={() => setProposal(null)}>取消</button>
              <button className="btn-danger" disabled={!proposal.ok}
                onClick={runChangeRoute}>執行三步序列</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
