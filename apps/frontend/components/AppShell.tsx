"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useUavStore } from "@/lib/store";
import { useTelemetry } from "@/lib/useTelemetry";

// 導覽 icon 化（simple-first）：文字語意放 title/aria-label，
// 「UAV 監控」品牌字是頂欄唯一文字錨點
const ic = (paths: React.ReactNode) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2" strokeLinecap="round"
    strokeLinejoin="round" aria-hidden>{paths}</svg>
);
const TABS = [
  { href: "/", label: "即時監控",
    icon: ic(<><circle cx="12" cy="12" r="3.2" /><path d="M12 2.5v4M12 17.5v4M2.5 12h4M17.5 12h4" /></>) },
  { href: "/drones", label: "無人機",
    icon: ic(<><circle cx="7" cy="7" r="3" /><circle cx="17" cy="7" r="3" /><circle cx="7" cy="17" r="3" /><circle cx="17" cy="17" r="3" /><path d="M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6" /></>) },
  { href: "/missions", label: "路徑管理",
    icon: ic(<><path d="M4 19c6 0 2-10 8-10 5 0 3 7 8 5" /><circle cx="4" cy="19" r="1.8" fill="currentColor" /><circle cx="20" cy="14" r="1.8" fill="currentColor" /></>) },
  { href: "/compare", label: "比較",
    icon: ic(<><path d="M5 20V10M12 20V4M19 20v-7" /></>) },
];

/** 頂欄＋內容區。WebSocket 掛在這一層，切換頁面不斷線、狀態 chips 全站可見。 */
export default function AppShell({ children }: { children: React.ReactNode }) {
  useTelemetry();
  const pathname = usePathname();
  const { live, wsConnected } = useUavStore();
  // 與 backend 的入庫條件一致（armed 且已建立架次，見 issues/004）——
  // 這顆 chip 的語意就是「現在寫進資料庫的資料存不存在」，不能只看 armed
  const recording = Boolean(live?.armed && live?.session_id);

  return (
    <div className="shell">
      <nav className="nav">
        <span className="brand">UAV 監控</span>
        {TABS.map((t) => (
          <Link
            key={t.href}
            href={t.href}
            title={t.label}
            aria-label={t.label}
            className={
              (t.href === "/" ? pathname === "/" : pathname.startsWith(t.href)) ? "active" : ""
            }
          >
            {t.icon}
          </Link>
        ))}
        <span className="spacer" />
        {/* 狀態縮成純圓點（文字 label 依需求移除），語意放 title——hover 可見。
            記錄狀態（#004：待機時刻意不入庫）仍以紅點／空心圈區分。 */}
        <span
          className="status-dot"
          title={`後端 ${wsConnected ? "已連線" : "斷線"}`}
          style={{ background: wsConnected ? "#0ca30c" : "#a01818" }}
        />
        <span
          className="status-dot"
          title={`MAVLink ${live?.connected ? "已連線" : "等待中"}`}
          style={{ background: live?.connected ? "#0ca30c" : "#a01818" }}
        />
        <span
          className="status-dot"
          title={recording ? "記錄中" : "待機（不記錄）"}
          style={
            recording
              ? { background: "#d03b3b" }
              : { background: "transparent", border: "1.5px solid var(--muted)" }
          }
        />
      </nav>
      <div className="content">{children}</div>
    </div>
  );
}
