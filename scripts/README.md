# scripts/

開發環境的安裝與啟動腳本。全部可重複執行。

| 腳本 | 用途 |
|---|---|
| `setup.sh` | **入口**：檢查工具 → 挑選連接埠 → 裝 Docker → backend venv → frontend 套件 |
| `install-docker.sh` | 從官方 apt repo 裝 Docker Engine + compose plugin（需 sudo，由 `setup.sh` 呼叫）|
| `dev-up.sh` | 起 TimescaleDB + PX4 SITL 容器，等 DB 健康並驗證 schema／seed |
| `dev-backend.sh` | 本機跑 FastAPI（`--reload`），自動帶入正確的 `DATABASE_URL` |
| `dev-frontend.sh` | 跑 Next.js dev server，用避開衝突後的連接埠 |
| `lib.sh` | 共用工具函式（不直接執行）|

## 安裝

```bash
./scripts/setup.sh
```

Docker 剛裝好時，群組變更要重新登入才生效——腳本會提示，或直接 `newgrp docker`。

## 啟動（三個終端）

```bash
./scripts/dev-up.sh        # 1. DB + SITL 容器
./scripts/dev-backend.sh   # 2. backend
./scripts/dev-frontend.sh  # 3. frontend
```

停止容器：`docker compose down`（加 `-v` 連 DB 資料一起清除；schema 的 init SQL
只在資料卷首次建立時執行，所以改了 `db/init/01_schema.sql` 要用 `down -v` 重來）。

## 連接埠

**慣例：自家服務一律用 30000 以上的 port。** 這台機器上 5432 有本機 PostgreSQL 16、
3000 有另一個專案的 next-server，用高位 port 一次避開所有這類衝突。

| 服務 | Port | 對應的常見預設 |
|---|---|---|
| TimescaleDB | `35432` | 5432 |
| Backend (FastAPI) | `38000` | 8000 |
| Frontend (Next.js) | `33000` | 3000 |
| MAVLink UDP | `14540` / `14550` | PX4 與 QGroundControl 的固定慣例，寫死在 SITL 映像裡，不動 |

`setup.sh` 會確認這些 port 沒被佔用（被佔用就往上找 +1、+2、+3），
結果寫進根目錄 `.env`（已 gitignore）：

```
DB_PORT=35432
BACKEND_PORT=38000
FRONTEND_PORT=33000
```

`docker-compose.yml`、三個 dev 腳本、以及 `frontend/.env.local` 都由這裡帶入，
改 port 只要改 `.env` 一處。

> 為什麼 DB 不直接用本機那套 PostgreSQL 16？因為要啟用 TimescaleDB 得改
> `shared_preload_libraries` 並**重啟整個 cluster**，會打斷正在用它的 `agentops`。
> 用容器把這個專案的 DB 完全隔開，代價只是多一個 container。
