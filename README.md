# UAV System — 5G 鏈路品質研究平台

無人機經 5G 通訊，研究飛入高干擾場域時對訊號品質的影響。
系統記錄「飛行遙測 × 5G 鏈路品質 × 空間位置」的時序資料，並即時視覺化。

## 專案結構

```
backend/    FastAPI + MAVSDK：遙測接收、5G 鏈路模擬、WebSocket 廣播、API
frontend/   Next.js + MapLibre：即時地圖監控、SINR 上色軌跡、5G 儀表板
db/init/    TimescaleDB schema（容器首次啟動自動執行）
doc/        系統設計文件
```

| 文件 | 內容 |
|---|---|
| [doc/architecture.md](doc/architecture.md) | 系統架構、關鍵決策（單機→多機、模擬→真機的擴充路徑）|
| [doc/data-schema.md](doc/data-schema.md) | 資料表設計與理由、取樣頻率策略 |
| [doc/frontend.md](doc/frontend.md) | 前端版面、軌跡上色規則、即時資料流 |

## 快速啟動

```bash
docker compose up -d          # TimescaleDB + PX4 SITL + backend
cd frontend
cp .env.local.example .env.local
npm install && npm run dev    # http://localhost:3000
```

Backend 不進 Docker 的開發跑法：

```bash
docker compose up -d db sitl
cd backend
uv venv .venv && uv pip install -r requirements.txt --python .venv/bin/python
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## 測試場景：飛進干擾區

Seed 資料在 PX4 SITL 預設起飛點（蘇黎世）旁放了兩個 gNB 和一個干擾區，
起飛往北約 200m 進入干擾區，可觀察 SINR 驟降、`link_lost` 事件與軌跡變色：

```bash
backend/.venv/bin/python -c "
import asyncio
from mavsdk import System
async def go():
    d = System(); await d.connect('udpin://0.0.0.0:14540')
    async for s in d.core.connection_state():
        if s.is_connected: break
    await d.action.arm(); await d.action.takeoff()
    await asyncio.sleep(15)
    await d.action.goto_location(47.3995, 8.5456, 540, 0)  # 干擾區中心
    await asyncio.sleep(60); await d.action.return_to_launch()
asyncio.run(go())"
```

（或用 QGroundControl 連 `udp:14550` 手動規劃任務。）

## Roadmap

1. ✅ 資料流骨架：SITL → backend → DB + WebSocket → 前端即時地圖
2. 歷史回放頁（軌跡上色 + SINR/RTT 時序圖表）
3. 任務規劃（地圖畫航點 → MAVSDK 上傳 → SITL 執行）
4. 前端干擾區編輯
5. 真機：ModemLinkSource（AT/QMI + ping 實測）、影像 WebRTC
