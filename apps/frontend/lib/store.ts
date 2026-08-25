import { create } from "zustand";

export interface LinkMetrics {
  rsrp: number; rsrq: number; sinr: number; cqi: number;
  pci: number; band: string; nr_mode: string;
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
  derived?: Record<string, unknown> | null;
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
  events: UavEvent[];
  sinrHistories: Record<string, number[]>;   // 每機 sparkline，各 120 筆
  // simple-first：專業數值面板是抽屜（預設關、點訊號格/▤ 開）
  panelOpen: boolean;
  setPanelOpen: (v: boolean) => void;
  // 起飛被拒（CommandPanel 判定 → HUD toast「點這裡看原因」）
  takeoffDeniedAt: number;
  noticeTakeoffDenied: () => void;
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
  events: [],
  sinrHistories: {},
  panelOpen: false,
  setPanelOpen: (v) => set({ panelOpen: v }),
  takeoffDeniedAt: 0,
  noticeTakeoffDenied: () => set({ takeoffDeniedAt: Date.now() }),
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
