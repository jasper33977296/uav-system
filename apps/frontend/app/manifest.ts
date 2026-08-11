import type { MetadataRoute } from "next";

// 消除瀏覽器自動請求的 404（manifest/apple-touch-icon/favicon.ico 同批補齊）
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "UAV 監控",
    short_name: "UAV 監控",
    description: "5G 訊號品質 × 無人機飛行監控",
    start_url: "/",
    display: "standalone",
    background_color: "#1b1a17",
    theme_color: "#1b1a17",
    icons: [{ src: "/icon.svg", type: "image/svg+xml", sizes: "any" }],
  };
}
