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
  wsConnected: boolean;
  events: UavEvent[];
  sinrHistory: number[]; // sparkline 用（主機），最近 120 筆
  setLive: (t: Telemetry) => void;
  setWsConnected: (v: boolean) => void;
  pushEvent: (e: UavEvent) => void;
  seedEvents: (es: UavEvent[]) => void;
}

export const useUavStore = create<UavStore>((set) => ({
  live: null,
  primaryId: null,
  fleet: {},
  trails: {},
  wsConnected: false,
  events: [],
  sinrHistory: [],
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
      const isPrimary = id === primaryId;
      const sinrHistory = isPrimary && t.link?.sinr != null
        ? [...s.sinrHistory, t.link.sinr].slice(-120) : s.sinrHistory;
      return { fleet, trails, primaryId,
               live: isPrimary ? t : s.live, sinrHistory };
    }),
  setWsConnected: (v) => set({ wsConnected: v }),
  pushEvent: (e) => set((s) => ({ events: [e, ...s.events].slice(0, 100) })),
  // 開頁補歷史用：只在 WS 事件先到時去重（以 id 為準），不覆蓋已收到的
  seedEvents: (es) =>
    set((s) => {
      const seen = new Set(s.events.map((e) => e.id));
      return { events: [...s.events, ...es.filter((e) => !seen.has(e.id))].slice(0, 100) };
    }),
}));
