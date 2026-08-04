"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useUavStore } from "@/lib/store";
import { useTelemetry } from "@/lib/useTelemetry";

const TABS = [
  { href: "/", label: "即時監控" },
  { href: "/drones", label: "無人機" },
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
            className={
              (t.href === "/" ? pathname === "/" : pathname.startsWith(t.href)) ? "active" : ""
            }
          >
            {t.label}
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
