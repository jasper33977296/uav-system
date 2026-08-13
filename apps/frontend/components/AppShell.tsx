"use client";
import Link from "next/link";
import { useEffect } from "react";
import { usePathname } from "next/navigation";

import { buildLabel } from "@/lib/buildInfo";
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
  // 機隊＝四旋翼頂視 silhouette（icon spec：小旋翼＋機身＋斜臂，避免 ⌘ 感）
  { href: "/drones", label: "無人機",
    icon: ic(<><circle cx="5.5" cy="5.5" r="2.4" /><circle cx="18.5" cy="5.5" r="2.4" /><circle cx="5.5" cy="18.5" r="2.4" /><circle cx="18.5" cy="18.5" r="2.4" /><path d="M7.5 7.5l2.6 2.6M16.5 7.5l-2.6 2.6M7.5 16.5l2.6-2.6M16.5 16.5l-2.6-2.6" /><rect x="10" y="10" width="4" height="4" rx="1.4" /></>) },
  { href: "/missions", label: "路徑管理",
    icon: ic(<><path d="M4 19c6 0 2-10 8-10 5 0 3 7 8 5" /><circle cx="4" cy="19" r="1.8" fill="currentColor" /><circle cx="20" cy="14" r="1.8" fill="currentColor" /></>) },
  // 比較＝雙折線疊影（icon spec：長條圖意象偏「統計」，換折線）
  { href: "/compare", label: "比較",
    icon: ic(<><path d="M3 16l5-6 4 3 6-8" /><path d="M3 20l5-4 4 2 6-6" opacity="0.5" /></>) },
];

/** 頂欄＋內容區。WebSocket 掛在這一層，切換頁面不斷線、狀態 chips 全站可見。 */
export default function AppShell({ children }: { children: React.ReactNode }) {
  useTelemetry();
  const pathname = usePathname();
  const live = useUavStore((s) => s.live);
  // 與 backend 的入庫條件一致（armed 且已建立架次，見 issues/004）——
  // 這顆 chip 的語意就是「現在寫進資料庫的資料存不存在」，不能只看 armed
  const recording = Boolean(live?.armed && live?.session_id);
  // §2.9：錄影不加燈，只擴充記錄燈 title；video_mode 缺（後端未發）＝
  // 維持原文案，不宣告不知道的事
  const recTitle = !recording ? "待機（不記錄）"
    : live?.video_mode === "on" ? "記錄中——遙測＋影像"
    : "記錄中（armed 且入庫）";

  // 版本進 console：調查時最常拿得到的就是 console 截圖（§0.2f）
  useEffect(() => {
    console.info(`[uav-frontend] 版本 ${buildLabel}`);
  }, []);

  return (
    <div className="shell">
      <nav className="nav">
        {/* §0.2f：版本已知時只住 tooltip（零版面成本）；未知／dirty 的
            主動告示在機隊頁，不進飛行畫面 */}
        <span className="brand" title={`UAV 監控 · ${buildLabel}`}>UAV 監控</span>
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
        {/* 狀態燈只留記錄燈（ui-spec §1）：後端/資料流異常已由 toast 與
            HUD 失聯樣式涵蓋。記錄＝armed 且入庫（#004：待機刻意不入庫） */}
        <span className="rec-light" title={recTitle}>
          <span className={`rec-dot ${recording ? "on" : ""}`} />
          <span className={`rec-txt ${recording ? "on" : ""}`}>
            {recording ? "記錄中" : "未記錄"}
          </span>
        </span>
      </nav>
      <div className="content">{children}</div>
    </div>
  );
}
