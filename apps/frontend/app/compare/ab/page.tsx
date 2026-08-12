"use client";
import dynamic from "next/dynamic";

// 前後比較頁（ui-spec §6b，023）：與 §6 v4 場域頁並存、不取代——
// v4 答「這場域哪裡弱」，本頁答「這條路徑改善前後差多少」。
// MapLibre 依賴 window，關閉 SSR。
const AbCompare = dynamic(() => import("@/components/AbCompare"), { ssr: false });

export default function CompareAb() {
  return (
    <main className="app app-solo">
      <AbCompare />
    </main>
  );
}
