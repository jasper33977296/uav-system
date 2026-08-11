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
  autopilot?: string | null;         // "px4"|"ardupilot"|"unknown"（015 batch2）
  mav_sysid?: number | null;         // 當下 sysid（選中機統一 ca0a472；null=非 MAVLink）
  link_state?: string | null;        // 機上鏈路狀態（ok/stale/lost）
  link_age_s?: number | null;        // 距最後一筆機上資料的秒數（失聯預警用）
  // 飛行就緒（QGC「Ready To Fly」同源訊號）
  ready?: boolean;
  not_ready_reasons?: string[];
  mav_state?: string | null;            // STANDBY / ACTIVE / CRITICAL…
  landed_state?: string | null;         // on_ground / in_air / takeoff / landing
  prearm_ok?: boolean | null;
  ekf_ok?: boolean | null;
  sensors_unhealthy?: string[];
  imu?: ImuData | null;              // IMU 卡（§2.6）；WS telemetry 整包透傳
  link: Partial<LinkMetrics>;
}

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
  events: UavEvent[];
  sinrHistories: Record<string, number[]>;   // 每機 sparkline，各 120 筆
  // simple-first：專業數值面板是抽屜（預設關、點訊號格/▤ 開）
  panelOpen: boolean;
  setPanelOpen: (v: boolean) => void;
  // deadman 已觸發（ManualControl 判定、HUD 異常句顯示）：danger 常駐至解除
  deadman: boolean;
  setDeadman: (v: boolean) => void;
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
  pushEvent: (e: UavEvent, fold?: boolean) => void;
  seedEvents: (es: UavEvent[]) => void;
}

export const useUavStore = create<UavStore>((set) => ({
  live: null,
  primaryId: null,
  selectedId: null,
  fleet: {},
  trails: {},
  wsConnected: false,
  events: [],
  sinrHistories: {},
  panelOpen: false,
  setPanelOpen: (v) => set({ panelOpen: v }),
  deadman: false,
  setDeadman: (v) => set({ deadman: v }),
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
}));
