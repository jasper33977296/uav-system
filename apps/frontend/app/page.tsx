"use client";
import dynamic from "next/dynamic";
import { useEffect } from "react";

import MissionPrompt from "@/components/MissionPrompt";
import SidePanel from "@/components/SidePanel";
import { useUavStore } from "@/lib/store";

// MapLibre 依賴 window，關閉 SSR。WS 由 AppShell 維持，這裡不用管。
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

/** 即時頁：地圖＋常駐側欄（使用者定案 2026-09-08）。
 *
 * 原本是「開頁即全幅地圖，側欄是抽屜、預設關」（08-04 simple-first）。改的理由
 * 是實際使用：訊號、事件、紀錄是飛行中**一直要掃**的三件事，藏在抽屜後面等於
 * 每次都得先點一下才開始工作。▤ 仍然收得起來（要全幅地圖時），而且記住選擇。
 */
export default function Home() {
  const panelOpen = useUavStore((s) => s.panelOpen);
  // 記住上次的選擇。**讀在 effect 裡而不是 store 初值**：SSR 沒有 localStorage，
  // 初值讀它會讓伺服器與瀏覽器算出不同的 HTML（hydration 不一致）
  useEffect(() => {
    try {
      const v = localStorage.getItem("panel-open");
      if (v === "0") useUavStore.getState().setPanelOpen(false);
    } catch { /* 隱私模式讀不到就用預設 */ }
  }, []);
  return (
    <main className={`app ${panelOpen ? "" : "app-solo"}`}>
      <MapView />
      {panelOpen && <SidePanel />}
      {/* 任務的生命週期：起飛時問名字、落地時問要不要結束（§4.5）。
          **不擋飛行**——任務是紀錄，取消掉照樣飛，事後在資訊頁補歸 */}
      <MissionPrompt />
    </main>
  );
}
