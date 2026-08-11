import { create } from "zustand";

export interface LinkMetrics {
  rsrp: number; rsrq: number; sinr: number; cqi: number;
  pci: number; band: string; nr_mode: string;
  rtt_ms: number; jitter_ms: number; packet_loss_pct: number;
  throughput_up_kbps: number; throughput_down_kbps: number;
  in_interference_zone: boolean; source: string;
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
  // 飛行就緒（QGC「Ready To Fly」同源訊號）
  ready?: boolean;
  not_ready_reasons?: string[];
  mav_state?: string | null;            // STANDBY / ACTIVE / CRITICAL…
  landed_state?: string | null;         // on_ground / in_air / takeoff / landing
  prearm_ok?: boolean | null;
  ekf_ok?: boolean | null;
  sensors_unhealthy?: string[];
  link: Partial<LinkMetrics>;
}

export interface UavEvent {
  id: number; time: string; severity: "info" | "warning" | "critical";
  type: string; detail: Record<string, unknown>;
  drone?: string | null;   // 多機時標示來源機
}

export interface TrailPoint { lat: number; lon: number; sinr: number | null; alt: number | null }

const TRAIL_MAX = 1200; // 1Hz 入庫、5Hz 推送下約 4 分鐘的尾跡

interface UavStore {
  live: Telemetry | null;                    // 主機（第一台出現的，＝MAVLink 機）
  primaryId: string | null;
  fleet: Record<string, Telemetry>;          // 全部機（含群飛僚機），鍵為 drone_id
  trails: Record<string, TrailPoint[]>;      // 每機各自的尾跡
  selectedId: string | null;                 // 側欄顯示哪台；null＝跟隨主機
  wsConnected: boolean;
  events: UavEvent[];
  sinrHistories: Record<string, number[]>;   // 每機 sparkline，各 120 筆
  // simple-first：專業數值面板是抽屜（預設關、點訊號格/▤ 開）
  panelOpen: boolean;
  setPanelOpen: (v: boolean) => void;
  setLive: (t: Telemetry) => void;
  select: (id: string) => void;
  setWsConnected: (v: boolean) => void;
  pushEvent: (e: UavEvent) => void;
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
  pushEvent: (e) => set((s) => ({ events: [e, ...s.events].slice(0, 100) })),
  // 開頁補歷史用：只在 WS 事件先到時去重（以 id 為準），不覆蓋已收到的
  seedEvents: (es) =>
    set((s) => {
      const seen = new Set(s.events.map((e) => e.id));
      return { events: [...s.events, ...es.filter((e) => !seen.has(e.id))].slice(0, 100) };
    }),
}));
