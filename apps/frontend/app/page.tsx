"use client";
import dynamic from "next/dynamic";

import SidePanel from "@/components/SidePanel";
import { useUavStore } from "@/lib/store";

// MapLibre 依賴 window，關閉 SSR。WS 由 AppShell 維持，這裡不用管。
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

// simple-first：開頁即全幅地圖；專業數值側欄是抽屜（▤/訊號格開，預設關）
export default function Home() {
  const panelOpen = useUavStore((s) => s.panelOpen);
  return (
    <main className={`app ${panelOpen ? "" : "app-solo"}`}>
      <MapView />
      {panelOpen && <SidePanel />}
    </main>
  );
}
