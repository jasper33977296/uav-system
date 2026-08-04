"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useUavStore } from "@/lib/store";
import { useTelemetry } from "@/lib/useTelemetry";

const TABS = [
  { href: "/", label: "即時監控" },
  { href: "/flights", label: "架次" },
  { href: "/scene", label: "場景" },
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
        <span className="chip">
          <span className="dot" style={{ background: wsConnected ? "#0ca30c" : "#a01818" }} />
          後端 {wsConnected ? "已連線" : "斷線"}
        </span>
        <span className="chip">
          <span className="dot" style={{ background: live?.connected ? "#0ca30c" : "#a01818" }} />
          MAVLink {live?.connected ? "已連線" : "等待中"}
        </span>
        {/* 記錄狀態是最重要的一顆：待機時系統刻意不入庫（#004），
            操作員必須隨時知道「現在飛的東西有沒有被記下來」 */}
        <span className={`chip ${recording ? "chip-rec" : ""}`}>
          <span
            className="dot"
            style={
              recording
                ? { background: "#d03b3b" }
                : { background: "transparent", border: "1.5px solid var(--muted)" }
            }
          />
          {recording ? "記錄中" : "待機（不記錄）"}
        </span>
      </nav>
      <div className="content">{children}</div>
    </div>
  );
}
