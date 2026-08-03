"use client";
import dynamic from "next/dynamic";

import SidePanel from "@/components/SidePanel";
import { useTelemetry } from "@/lib/useTelemetry";

// MapLibre 依賴 window，關閉 SSR
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

export default function Home() {
  useTelemetry();
  return (
    <main className="app">
      <MapView />
      <SidePanel />
    </main>
  );
}
