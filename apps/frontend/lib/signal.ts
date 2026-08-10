// 鏈路健康分級：門檻對齊 backend 的事件門檻 (sinr_degraded_db=5, sinr_lost_db=-2)
// 顏色為 status palette（狀態專用色，不與資料序列色混用）。
//
// 色值經 CVD 驗證選定（2026-08-04，validate_palette.js）：
//   - 原 serious #ec835a 與 warning 的 ΔE 只有 13.6，連正常色覺都難分，
//     改 #e05e0e 後 ΔE 19+、CVD 全過。
//   - warning 的亮黃**不能改深**：它的亮度是紅綠色盲（protan）分辨
//     good↔warning 的唯一線索，改深會讓 ΔE 崩到 4。
// 配套規則：顏色永遠不單獨傳達語意（圖例＋文字標籤並行）。
export type LinkClass = "good" | "warning" | "serious" | "critical";

export const LINK_CLASSES: {
  key: LinkClass; label: string; color: string; min: number;
}[] = [
  { key: "good",     label: "良好 (SINR ≥ 13dB)",  color: "#0ca30c", min: 13 },
  { key: "warning",  label: "尚可 (5–13dB)",       color: "#fab219", min: 5 },
  { key: "serious",  label: "劣化 (-2–5dB)",       color: "#e05e0e", min: -2 },
  { key: "critical", label: "瀕斷 (< -2dB)",       color: "#a01818", min: -Infinity },
];

export function classifySinr(sinr: number): (typeof LINK_CLASSES)[number] {
  return LINK_CLASSES.find((c) => sinr >= c.min) ?? LINK_CLASSES[3];
}

// API/WS 位址：未以環境變數指定時，從瀏覽器網址推導 host——
// 寫死 localhost 的話，從區網其他機器開頁面會連到「那台機器自己」。
// backend 與 frontend 約定跑在同一台主機（分工見 doc/architecture.md），
// 只有 port 不同，因此拿 window.location.hostname 換 port 即可。
// SSR 階段沒有 window，給 localhost 佔位——實際請求都在 client 端發生。
const host = typeof window === "undefined" ? "localhost" : window.location.hostname;

export const API = process.env.NEXT_PUBLIC_API_URL ?? `http://${host}:38000`;
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? `ws://${host}:38000/ws/telemetry`;
// command 服務（GCS 取代階段 2/3）：與 backend 分離的獨立服務，同主機不同 port
export const COMMAND_API = process.env.NEXT_PUBLIC_COMMAND_API ?? `http://${host}:38001`;
