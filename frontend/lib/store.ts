import { create } from "zustand";

export interface LinkMetrics {
  rsrp: number; rsrq: number; sinr: number; cqi: number;
  pci: number; band: string; nr_mode: string;
  rtt_ms: number; jitter_ms: number; packet_loss_pct: number;
  throughput_up_kbps: number; throughput_down_kbps: number;
  in_interference_zone: boolean; source: string;
}

export interface Telemetry {
  drone_id: string; session_id: string | null; connected: boolean;
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
}

export interface TrailPoint { lat: number; lon: number; sinr: number | null; alt: number | null }

const TRAIL_MAX = 1200; // 1Hz 入庫、5Hz 推送下約 4 分鐘的尾跡

interface UavStore {
  live: Telemetry | null;
  wsConnected: boolean;
  events: UavEvent[];
  trail: TrailPoint[];
  sinrHistory: number[]; // sparkline 用，最近 120 筆
  setLive: (t: Telemetry) => void;
  setWsConnected: (v: boolean) => void;
  pushEvent: (e: UavEvent) => void;
  seedEvents: (es: UavEvent[]) => void;
}

export const useUavStore = create<UavStore>((set) => ({
  live: null,
  wsConnected: false,
  events: [],
  trail: [],
  sinrHistory: [],
  setLive: (t) =>
    set((s) => {
      const trail =
        t.lat != null && t.lon != null
          ? [...s.trail, { lat: t.lat, lon: t.lon, sinr: t.link?.sinr ?? null, alt: t.alt_rel }].slice(-TRAIL_MAX)
          : s.trail;
      const sinrHistory =
        t.link?.sinr != null ? [...s.sinrHistory, t.link.sinr].slice(-120) : s.sinrHistory;
      return { live: t, trail, sinrHistory };
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
