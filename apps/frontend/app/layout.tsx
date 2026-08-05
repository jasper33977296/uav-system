import type { Metadata } from "next";
import "./globals.css";

import AppShell from "@/components/AppShell";

export const metadata: Metadata = {
  title: "UAV 監控系統",
  description: "5G 鏈路品質 × 無人機飛行監控",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-Hant">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
