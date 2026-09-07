import { create } from "zustand";

export interface LinkMetrics {
  rsrp: number; rsrq: number; sinr: number; cqi: number;
  pci: number; cell_id: number; band: string; nr_mode: string;
  /** modem 原始回應。**其中 `_derived` 是後端從 raw 解出來的欄位清單**
   * （`app/modem_raw.py`）——畫面要分得出「模組報的」與「我方算的」。 */
  raw: Record<string, unknown> | null;
  rtt_ms: number; jitter_ms: number; packet_loss_pct: number;
  throughput_up_kbps: number; throughput_down_kbps: number;
  in_interference_zone: boolean; source: string;
}

/** IMU 卡資料（ui-spec §2.6）。後端契約落地前的暫定形：欄位名沿 MAVLink
 * 原訊息（ATTITUDE 角速率／HIGHRES_IMU／VIBRATION），全部可缺（feature-
 * detect：缺項回 null、缺欄整列不畫）。單位假設＝MAVLink 原生（角速率
 * rad/s、加速度 m/s²、磁力 gauss→後端應轉 µT）——契約到齊時對齊此註解。 */
export interface ImuData {
  rollspeed?: number | null; pitchspeed?: number | null; yawspeed?: number | null;
  xacc?: number | null; yacc?: number | null; zacc?: number | null;
  xgyro?: number | null; ygyro?: number | null; zgyro?: number | null;
  xmag?: number | null; ymag?: number | null; zmag?: number | null;
  temperature?: number | null;
  abs_pressure?: number | null;   // hPa
  pressure_alt?: number | null;   // m（氣壓高度）
  vibration_x?: number | null; vibration_y?: number | null; vibration_z?: number | null;
  clipping_0?: number | null; clipping_1?: number | null; clipping_2?: number | null;
}

/** 意圖協定 §4.2 的鏡像（doc/agent-intent-protocol.md）。
 * **權威在機上代理，這裡只是鏡像**——不修正、不補值。`fresh=false` 代表
 * 「這是最後看到的狀態，現在不知道」，與「沒有代理」是兩件事。 */
export interface AgentState {
  board_uid: string; drone_id: string | null;
  agent_version: string | null;
  inputs: string[];
  connected: boolean;      // 意圖通道還在嗎
  fresh: boolean;          // 5 秒內有推過 state 嗎（代理 1Hz 保活）
  state: string | null;    // FLYING_MISSION / HOLDING / PILOT_CONTROL…
  since: string | null;
  mission_seq?: number | null; mission_total?: number | null;
  /** 機端對任務現況的說法（見後端 state.MISSION_STATE）。null＝舊韌體沒這欄 */
  mission_state?: string | null;
  derived?: Record<string, unknown> | null;
  /** 遙控器連上了沒有（039 複裁 A）。**null＝不知道**（舊版代理還沒開始送），
   * 與 false＝確定沒連上是兩件事——畫面要分開講，不能把「不知道」畫成「沒有」。
   * 這是「機在地上失聯只告警」那格的前提：沒有 RC 就沒有人能接管。 */
  rc_link?: boolean | null;
  /** 失聯期間壓下來、等著補送的 intent 則數（039 複裁 G）。 */
  pending?: number;
  /** 機上錄製的回傳現況（issues/014）。**null＝這台機不會自己回傳**——
   * 代理太舊（v0.6.0 起才有）或自動回傳被關掉，兩者都不等於「沒有東西要傳」。
   * `blocked` 是「為什麼沒在傳」那句話：**「沒有東西要傳」與「傳不動」
   * 都是「沒在傳」，但一個是完成、一個是故障**，而處置完全相反。 */
  record_upload?: RecordUpload | null;
  /** 現在這個模式是誰造成的：`"us"`／`"pilot"`／**`null`＝不知道**。
   * LOITER 同時是地面站的「暫停」與飛手最常用的手飛模式，只有溯源分得出來。
   * `null` 時守門放行（使用者 2026-09-02 裁定：不知道也要能掌控），
   * **但畫面要說「來源不明」**——操作員有權知道他可能正在接管一台有人在飛的機。 */
  mode_owner?: "us" | "pilot" | null;
}

/** 機上錄製回傳的現況（代理每秒推一次）。 */
export interface RecordUpload {
  pending: number;            // 關好了、還沒回傳成功的份數
  pending_files: string[];    // 那幾份的檔名（最多 20）
  blocked: string | null;     // 為什麼沒在傳；null＝沒有阻礙
  current: string | null;     // 正在傳哪一份
  progress: number | null;    // 0..1
  abandoned: number;          // 滾動刪掉時還沒回傳成功的累計份數
}

/** 失聯期間按下的操作，恢復後系統重算的判決（039 複裁 G）。
 * **這不是「已經做了」的通知，是「你按過、但沒有送出去」的清單**——
 * 要不要真的做，人再按一次，走原本那條完整路徑。 */
export interface IntentReplay {
  drone_id: string | null; board_uid: string;
  action: string; intent_id: string;
  age_s: number;            // 當初按下距離補送有多久
  verdict: string | null;   // 代理重算後的判決（乾跑，沒有動飛機）
  reason: string | null;
  state: string | null;
  at: number;               // 收到補送結果的本地時刻（畫面排序用）
}

export interface Telemetry {
  drone_id: string; drone_name?: string | null;
  primary?: boolean;                 // MAVLink 主機的廣播帶此旗標
  session_id: string | null; connected: boolean;
  lat: number | null; lon: number | null;
  alt_msl: number | null; alt_rel: number | null;
  heading: number | null; roll: number | null; pitch: number | null;
  ground_speed: number | null; vertical_speed: number | null;
  battery_pct: number | null; battery_voltage: number | null;
  gps_fix: number | null; satellites: number | null;
  flight_mode: string | null; armed: boolean;
  // 廠牌無關的模式語意（§0.2d）：**判斷用這個、顯示用 flight_mode**。
  // null 是常態（PX4 起飛中、手動類模式、ArduPilot SMART_RTL 皆為 null）
  mode_verb?: string | null;
  autopilot?: string | null;         // "px4"|"ardupilot"|"unknown"（015 batch2）
  mav_sysid?: number | null;         // 當下 sysid（選中機統一 ca0a472；null=非 MAVLink）
  link_state?: string | null;        // 機上鏈路狀態（ok/stale/lost）
  link_age_s?: number | null;        // 距最後一筆機上資料的秒數（失聯預警用）
  //: **畫面上那些數字有多舊**（A 層）。斷線時數值不會消失，它們只是變舊——
  //: 而舊到某個程度之後，它們與「現在」的關係就只剩誤導
  telem_age_s?: number | null;
  // 飛行就緒（QGC「Ready To Fly」同源訊號）
  ready?: boolean | null;   // null＝判斷依據未到齊（§0.2b：不知道，非未就緒）
  not_ready_reasons?: string[];
  mav_state?: string | null;            // STANDBY / ACTIVE / CRITICAL…
  landed_state?: string | null;         // on_ground / in_air / takeoff / landing
  prearm_ok?: boolean | null;
  ekf_ok?: boolean | null;
  sensors_unhealthy?: string[];
  imu?: ImuData | null;              // IMU 卡（§2.6）；WS telemetry 整包透傳
  // 影像錄製現況（§2.9；022 暫定契約形，欄位缺＝記錄燈維持原文案不宣告影像）
  video_mode?: "on" | "off" | "no_source" | null;
  link: Partial<LinkMetrics>;
}

/** 機上資料 §2.8（014 Phase B 訊息登錄表）。後端契約落地前的暫定形：
 * WS 廣播 {type:"msg_registry", drone_id, sensors, messages}，1–2Hz。
 * fields＝該型別最新欄位值（方言原樣不翻譯）；未知型別 name=null。 */
export interface RegistryMsg {
  id: number;                  // MAVLink msgid
  name?: string | null;        // 已知型別名；null＝未知 → UI 顯 #id
  hz: number | null;           // 一次性訊息（MISSION_ACK 等）無率——null 誠實
  age_s: number | null;
  fields?: Record<string, unknown> | null;
  // 線上單位（pymavlink fieldunits_by_name＝MAVLink XML 同源）：raw wire
  // 單位配 raw 值——degE7/mV/cdegC 等縮放單位原樣直出，兩邊都誠實不換算
  units?: Record<string, string> | null;
  displays?: Record<string, string> | null;   // 顯示提示（如 'bitmask'）
}
export interface SensorHealth { name: string; ok: boolean }
export interface DroneRegistry { sensors: SensorHealth[]; messages: RegistryMsg[] }

export interface UavEvent {
  id: number; time: string; severity: "info" | "warning" | "critical";
  type: string; detail: Record<string, unknown>;
  drone?: string | null;   // 多機時標示來源機（WS 路徑帶名）
  drone_id?: string | null; // REST 補歷史路徑帶 id 不帶名——顯示時查 fleet
  source?: "vehicle" | "system" | null;   // vehicle＝自駕儀 STATUSTEXT；system＝backend 推導
  timeFirst?: string;      // 折疊事件首次時間（客端保留；modal ×N 時間範圍用）
}

export interface TrailPoint { lat: number; lon: number; sinr: number | null; alt: number | null }

const TRAIL_MAX = 1200; // 1Hz 入庫、5Hz 推送下約 4 分鐘的尾跡

interface UavStore {
  live: Telemetry | null;                    // 主機（第一台出現的，＝MAVLink 機）
  primaryId: string | null;
  fleet: Record<string, Telemetry>;          // 全部機（多 SITL/編隊），鍵為 drone_id
  trails: Record<string, TrailPoint[]>;      // 每機各自的尾跡
  selectedId: string | null;                 // 側欄顯示哪台；null＝跟隨主機
  wsConnected: boolean;
  registry: Record<string, DroneRegistry>;   // 機上資料 §2.8，鍵為 drone_id
  // 意圖協定的狀態鏡像，鍵為 drone_id（沒註冊的代理沒有 drone_id，不入表——
  // 那種連線在後端 log 看得到，但它不對應畫面上任何一台機）
  agents: Record<string, AgentState>;
  setAgent: (a: AgentState) => void;
  //: 補送結果，鍵為 drone_id。**留著直到人自己關掉**：它描述的是一件
  //: 已經發生的事（你按過、系統沒做），不是一個會自己過期的狀態
  replays: Record<string, IntentReplay[]>;
  pushReplay: (r: IntentReplay) => void;
  clearReplays: (droneId: string) => void;
  events: UavEvent[];
  sinrHistories: Record<string, number[]>;   // 每機 sparkline，各 120 筆
  // simple-first：專業數值面板是抽屜（預設關、點訊號格/▤ 開）
  panelOpen: boolean;
  setPanelOpen: (v: boolean) => void;
  /** 最近一次指令被拒。**存的是那句話，不只是時間戳。**
   *
   * 原本只存 `takeoffDeniedAt`，所以 HUD 只說得出「現在還不能起飛——點這裡
   * 看原因」。2026-09-02 現場實測：操作員被三件事同時擋著（油門桿沒推到底、
   * GCS failsafe、入列未通過），而畫面上那一句話對三者一視同仁——**要知道
   * 是哪一件，得自己去展開面板、或去翻事件流**。
   *
   * 而原因後端一直都有給（`detail.msg`／`not_ready_reasons`），
   * 只是被前端在這一格丟掉了。 */
  denial: { at: number; action: string; text: string } | null;
  noticeDenied: (action: string, text: string) => void;
  // 喚起任務控制面板（toast 點擊展開原因用；計數器遞增觸發）
  cmdOpenReq: number;
  requestCmdPanel: () => void;
  // 任務疊圖重刷（§4 v3：任務開始成功自動 activate → 即時頁疊圖即刻浮現）
  planReq: number;
  requestPlanRefresh: () => void;
  // 013-A 編隊模式（ui-spec §2.5）：targetIds（指揮誰）疊在 selectedId
  // （看誰）之上——兩者可不同機；layer_index＝targetIds 內的順序
  formation: boolean;
  setFormation: (v: boolean, seedTargets?: string[]) => void;
  targetIds: string[];
  toggleTarget: (id: string) => void;
  formationCfg: {
    mode: "unified" | "separate";
    base: string;                      // unified：base_mission_id
    spacing: number;                   // unified：垂直層距（GROUP_VSEP_M）
    assign: Record<string, string>;    // separate：drone_id → mission_id
  };
  setFormationCfg: (p: Partial<UavStore["formationCfg"]>) => void;
  // 013-B 前半：draft 群組（POST /api/groups 的回應）——預覽自此改讀
  // 後端 materialized assignments（單一真相），設定變更即失效待重建
  // 013-B 執行中的群組 id：進度視圖以此為準（存 store——元件重掛/state
  // 丟失時輪詢自癒，不依賴一次性 setState）
  runGroupId: string | null;
  setRunGroupId: (id: string | null) => void;
  // 全部起飛的兩段式 armed-until（store＋呼叫當下讀取：confirm 窗存活
  // 必須獨立於任何 re-render/重掛——live 驗收抓到窗跨 poll 邊界即被清）
  execArmedUntil: number;
  setExecArmedUntil: (t: number) => void;
  draftGroup: {
    id: string; name: string; mode: string;
    conflictOk: boolean;
    conflicts: { a: string; b: string; why: string }[];
    assignments: { drone_id: string; mission_id: string; layer_index: number;
      phase: string; drone_name?: string; mav_sysid?: number | null }[];
  } | null;
  setDraftGroup: (g: UavStore["draftGroup"]) => void;
  setLive: (t: Telemetry) => void;
  select: (id: string) => void;
  /** 記錄被刪除了：把這台機從所有以 drone_id 為鍵的表裡拿掉。
   * **少清一張表，它就會在那張表撐著半條命**——例如尾跡還在地圖上、
   * 或側欄鎖著一台已經不存在的機。 */
  removeDrone: (id: string) => void;
  /** 記錄改名了。**執行期是快取，資料庫才是事實來源**——後端改完會推
   * 一則，這裡把畫面上那幾張表一起更新，不必等重新整理。 */
  renameDrone: (id: string, name: string) => void;
  setWsConnected: (v: boolean) => void;
  setRegistry: (droneId: string, r: DroneRegistry) => void;
  pushEvent: (e: UavEvent, fold?: boolean) => void;
  seedEvents: (es: UavEvent[]) => void;
  // 事件歷史取得失敗（§0.2e）：沒有這個旗標的話，後端掛掉時事件卡會顯示
  // 「尚無事件」＝**宣告沒有異常發生**，那是本 UI 最強的安心宣告
  eventsFailed: boolean;
  setEventsFailed: (v: boolean) => void;
}

export const useUavStore = create<UavStore>((set) => ({
  live: null,
  primaryId: null,
  selectedId: null,
  fleet: {},
  trails: {},
  wsConnected: false,
  registry: {},
  agents: {},
  setAgent: (a) =>
    set((s) => (a.drone_id ? { agents: { ...s.agents, [a.drone_id]: a } } : s)),
  replays: {},
  pushReplay: (r) =>
    set((s) => {
      const k = r.drone_id ?? r.board_uid;
      return { replays: { ...s.replays, [k]: [...(s.replays[k] ?? []), r] } };
    }),
  clearReplays: (droneId) =>
    set((s) => {
      const next = { ...s.replays };
      delete next[droneId];
      return { replays: next };
    }),
  events: [],
  sinrHistories: {},
  panelOpen: false,
  setPanelOpen: (v) => set({ panelOpen: v }),
  denial: null,
  noticeDenied: (action, text) =>
    set({ denial: { at: Date.now(), action, text } }),
  cmdOpenReq: 0,
  requestCmdPanel: () => set((s) => ({ cmdOpenReq: s.cmdOpenReq + 1 })),
  planReq: 0,
  requestPlanRefresh: () => set((s) => ({ planReq: s.planReq + 1 })),
  formation: false,
  setFormation: (v, seedTargets) =>
    set((s) => ({ formation: v, targetIds: v ? seedTargets ?? s.targetIds : s.targetIds })),
  targetIds: [],
  toggleTarget: (id) =>
    set((s) => ({
      targetIds: s.targetIds.includes(id)
        ? s.targetIds.filter((x) => x !== id)
        : [...s.targetIds, id],
    })),
  formationCfg: { mode: "unified", base: "", spacing: 5, assign: {} },
  setFormationCfg: (p) => set((s) => ({ formationCfg: { ...s.formationCfg, ...p } })),
  runGroupId: null,
  setRunGroupId: (id) => set({ runGroupId: id }),
  execArmedUntil: 0,
  setExecArmedUntil: (t) => set({ execArmedUntil: t }),
  draftGroup: null,
  setDraftGroup: (g) => set({ draftGroup: g }),
  setLive: (t) =>
    set((s) => {
      const id = t.drone_id ?? "unknown";
      // 主機＝帶 primary 旗標的（MAVLink 機）；旗標未到前暫用第一台出現的
      const primaryId = t.primary ? id : (s.primaryId ?? id);
      const fleet = { ...s.fleet, [id]: t };
      let trails = s.trails;
      if (t.lat != null && t.lon != null) {
        const prev = s.trails[id] ?? [];
        trails = { ...s.trails,
          [id]: [...prev, { lat: t.lat, lon: t.lon, sinr: t.link?.sinr ?? null, alt: t.alt_rel }]
            .slice(-TRAIL_MAX) };
      }
      let sinrHistories = s.sinrHistories;
      if (t.link?.sinr != null) {
        sinrHistories = { ...s.sinrHistories,
          [id]: [...(s.sinrHistories[id] ?? []), t.link.sinr].slice(-120) };
      }
      // 側欄顯示選中的那台（未選＝主機）
      const effective = s.selectedId ?? primaryId;
      return { fleet, trails, primaryId, sinrHistories,
               live: id === effective ? t : s.live };
    }),
  select: (id) =>
    set((s) => ({ selectedId: id, live: s.fleet[id] ?? s.live })),
  removeDrone: (id) =>
    set((st) => {
      const drop = <T,>(m: Record<string, T>) => {
        const n = { ...m }; delete n[id]; return n;
      };
      const assign = { ...st.formationCfg.assign }; delete assign[id];
      return {
        fleet: drop(st.fleet), trails: drop(st.trails),
        registry: drop(st.registry), agents: drop(st.agents),
        replays: drop(st.replays), sinrHistories: drop(st.sinrHistories),
        // **選中／主機指到它就要放手**：留著的話側欄會鎖在一台不存在的機上，
        // 而畫面看起來只是「那台機沒有資料」
        selectedId: st.selectedId === id ? null : st.selectedId,
        primaryId: st.primaryId === id ? null : st.primaryId,
        live: st.live?.drone_id === id ? null : st.live,
        targetIds: st.targetIds.filter((t) => t !== id),
        formationCfg: { ...st.formationCfg, assign },
      };
    }),
  renameDrone: (id, name) =>
    set((st) => ({
      fleet: st.fleet[id]
        ? { ...st.fleet, [id]: { ...st.fleet[id], drone_name: name } } : st.fleet,
      live: st.live?.drone_id === id ? { ...st.live, drone_name: name } : st.live,
    })),
  setWsConnected: (v) => set({ wsConnected: v }),
  setRegistry: (droneId, r) =>
    set((s) => ({ registry: { ...s.registry, [droneId]: r } })),
  // fold＝同句 STATUSTEXT 重複：就地替換同 id 那筆（count/時間更新、位置不動）；
  // 本地找不到（開頁晚於首播）就當新事件 append
  pushEvent: (e, fold = false) =>
    set((s) => {
      if (fold) {
        const i = s.events.findIndex((x) => x.id === e.id);
        if (i >= 0) {
          const events = [...s.events];
          // 折疊就地更新會覆蓋 time——首次時間客端保留（§2.7 ×N 範圍）
          events[i] = { ...e, timeFirst: events[i].timeFirst ?? events[i].time };
          return { events };
        }
      }
      return { events: [e, ...s.events].slice(0, 100) };
    }),
  // 開頁補歷史用：只在 WS 事件先到時去重（以 id 為準），不覆蓋已收到的
  seedEvents: (es) =>
    set((s) => {
      const seen = new Set(s.events.map((e) => e.id));
      return { events: [...s.events, ...es.filter((e) => !seen.has(e.id))].slice(0, 100) };
    }),
  eventsFailed: false,
  setEventsFailed: (v) => set({ eventsFailed: v }),
}));

/** 地圖初始中心：取第一台有座標的機。
 *
 * **不寫死任何地點**——蘇黎世那組常數是 PX4 SITL 的舊出生點，機隊搬到
 * 台北後就成了「開頁第一眼在別的洲」（NLSC 境外無圖資，看起來像底圖壞掉）。
 * 沒有任何座標時回 null：呼叫端用世界視野開場、拿到資料再 jumpTo——
 * 與其指一個我們並不知道的地點，不如先不指。
 */
export function firstFleetPos(): [number, number] | null {
  for (const t of Object.values(useUavStore.getState().fleet)) {
    if (t.lat != null && t.lon != null) return [t.lon, t.lat];
  }
  return null;
}
