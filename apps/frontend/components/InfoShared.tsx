"use client";
/** 資訊頁三個分頁共用的型別、格式化與小元件。
 *
 * 放在同一個檔案的理由：這些東西**只有資訊頁在用**，而且它們必須三個分頁
 * 說得一模一樣——同一個「不知道」在架次分頁寫「—」、在事件分頁寫「未知」，
 * 讀的人會以為那是兩件事（ui-spec §0.2e「兩種沒有不得同形」的反面：
 * 同一種沒有不得異形）。
 */
import { type ReactNode } from "react";

export interface SessionRow {
  id: string;
  drone_id: string;
  drone_name: string;
  mission_id: string | null;
  mission_name: string | null;
  started_at: string;
  ended_at: string | null;
  note: string | null;
  origin: string | null;
  video_mode: string | null;
  end_reason: string | null;
  summary: {
    avg_sinr?: number | null; min_sinr?: number | null; avg_rtt_ms?: number | null;
    max_alt_rel?: number | null; samples_total?: number | null;
    samples_in_zone?: number | null;
  } | null;
  events_total?: number;
  events_warning?: number;
  events_critical?: number;
}

export interface EventRow {
  id: number; time: string; severity: string; type: string;
  detail: Record<string, unknown>;
  source: string | null; drone_id: string | null; session_id: string | null;
}

export interface CommandRow {
  time: string; action: string; result: string;
  detail: string | null; client: string | null;
  params: Record<string, unknown> | null;
}

export interface DroneRow { id: string; name: string }

// ── 時間與數字 ────────────────────────────────────────────────
export const dateTime = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("zh-TW", { hour12: false }) : "—";
export const dayShort = (iso: string) =>
  new Date(iso).toLocaleString("zh-TW", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
export const hms = (iso: string) =>
  new Date(iso).toLocaleTimeString("zh-TW", { hour12: false });
export const secs = (s: number) =>
  s >= 3600 ? `${Math.floor(s / 3600)} 時 ${Math.floor((s % 3600) / 60)} 分`
    : s >= 60 ? `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`
      : `${Math.round(s)} 秒`;
/** 一趟飛了多久。**還沒結束就說「進行中」**，不要拿 now 去減出一個
 * 會自己長大的數字然後把它排在已完成的那些旁邊比較。 */
export const duration = (a: string, b: string | null) =>
  b ? secs((new Date(b).getTime() - new Date(a).getTime()) / 1000) : "進行中";
export const num = (v: number | null | undefined, d = 1) =>
  typeof v === "number" ? v.toFixed(d) : "—";

// ── 嚴重度 ───────────────────────────────────────────────────
export const SEV_COLOR: Record<string, string> = {
  critical: "#a01818", warning: "#fab219", info: "#8f8b80",
};
export const SEV_LABEL: Record<string, string> = {
  critical: "危急", warning: "警告", info: "資訊",
};

/** 三種狀態各自要有自己的話（lib/fetchJson.ts 的紀律）：載入中／取得失敗／
 * 真的沒有。**把其中兩種塞進同一句等於兩個都沒宣告。** */
export function Placeholder({ loading, err, empty, children }: {
  loading: boolean; err: string | null; empty: string; children?: ReactNode;
}) {
  if (err) return <div className="form-err">{err}</div>;
  if (loading) return <div className="empty">載入中…</div>;
  return <div className="empty">{empty}{children}</div>;
}
