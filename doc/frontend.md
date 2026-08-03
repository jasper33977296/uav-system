# 前端設計

Next.js (App Router) + MapLibre GL + zustand。地圖為中心（map-centric）的監控介面。

## 版面

```
┌─────────────────────────────────────────────┬──────────────────┐
│  地圖                                        │ 側欄 (380px)      │
│  ・無人機即時位置（機頭朝向三角形）            │ ・連線狀態 chips   │
│  ・飛行軌跡：依鏈路健康四段上色                │ ・影像預留位 16:9  │
│  ・干擾區（紅色虛線圈）                       │ ・飛行儀表        │
│  ・gNB 基地台位置                            │ ・5G 鏈路卡       │
│  ・圖例（左下）                              │ ・事件流          │
└─────────────────────────────────────────────┴──────────────────┘
```

之後的頁面：`/missions`（任務規劃）、`/flights/[sessionId]`(歷史回放）。
三頁**共用同一個地圖元件**，用 props 切換模式，不做成三個獨立地圖。

## 軌跡上色規則（研究主視覺）

鏈路健康是一種**狀態**編碼，用 status palette 四段，門檻與 backend 事件門檻一致
（`lib/signal.ts` 單一出處）：

| 分級 | SINR | 顏色 |
|---|---|---|
| 良好 | ≥ 13 dB | `#0ca30c` |
| 尚可 | 5–13 dB | `#fab219` |
| 劣化 | -2–5 dB | `#ec835a` |
| 瀕斷 | < -2 dB | `#d03b3b` |

配套規則：地圖左下常駐圖例；側欄同時顯示 SINR 數值與分級標籤（不靠顏色單獨傳達）；
面板文字一律用文字色，彩色只出現在狀態圓點與軌跡。

## 即時資料流

- `lib/useTelemetry.ts`：WebSocket 連 backend `/ws/telemetry`，自動重連（2 秒）。
  **不經過 Next.js API route**，REST 也直連 FastAPI（CORS 已開）。
- `lib/store.ts`（zustand）：`live`（最新機況）、`trail`（尾跡，上限 1200 點）、
  `sinrHistory`（sparkline 用 120 筆）、`events`。
  地圖用 `subscribe` 直接更新 MapLibre source，不觸發 React re-render；
  側欄各自訂閱需要的欄位。

## 技術注意事項

- **MapLibre 依賴 `window`，必須關 SSR**：
  `dynamic(() => import("@/components/MapView"), { ssr: false })`，
  因此 `app/page.tsx` 是 client component。
- 底圖目前用 OSM raster tile。台灣場域可換國土測繪中心（NLSC）WMTS，
  正射影像對場域研究較有用——只需換 `MapView.tsx` 的 style source。
- 深色模式：CSS variables 在 `globals.css` 依 `prefers-color-scheme` 切換，
  色彩角色（surface/ink/hairline/series）皆為 light/dark 各自選定的值。
- 環境變數：`NEXT_PUBLIC_API_URL`、`NEXT_PUBLIC_WS_URL`（見 `.env.local.example`）。

## 待做（對應 roadmap）

1. 歷史回放頁：`GET /api/sessions/{id}/track` 資料已就緒；
   需要時間軸播放控制 + SINR/RTT 時序圖（附 crosshair tooltip）。
2. 干擾區編輯：地圖畫圈 → `POST /api/zones`（API 已就緒）。
3. 任務規劃：地圖點擊畫航點 → missions/waypoints CRUD → MAVSDK 上傳。
4. 影像：placeholder 換 `<video>` + WHEP client。
