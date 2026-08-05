"use client";
import dynamic from "next/dynamic";

import SidePanel from "@/components/SidePanel";

// MapLibre 依賴 window，關閉 SSR。WS 由 AppShell 維持，這裡不用管。
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

export default function Home() {
  return (
    <main className="app">
      <MapView />
      <SidePanel />
    </main>
  );
}
