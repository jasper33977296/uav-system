"use client";
import dynamic from "next/dynamic";

// 場域訊號頁（ui-spec §6 v4）：/compare 重定義為全幅地圖頁——開頁零步驟
// 直接呈現「這個場域哪裡訊號弱」。MapLibre 依賴 window，關閉 SSR。
const FieldMap = dynamic(() => import("@/components/FieldMap"), { ssr: false });

export default function Compare() {
  return (
    <main className="app app-solo">
      <FieldMap />
    </main>
  );
}
