# build context 是 **repo 根**（不是 apps/backend）——見 docker-compose.yml。
# 原因：共用的 `libs/autopilot` 兩個服務都要裝得到，而 Docker 不能 COPY
# context 之外的東西（issue 026 B2）。
FROM python:3.12-slim
WORKDIR /srv
# ffmpeg：把機上送來的 H.264 轉成 MJPEG 給外部（issue 022，2026-09-23）。
# **放在最前面**：它幾百 MB 又幾乎不會變，放這裡讓這層的快取活得最久。
# 為什麼不在機上直接送 MJPEG：720p30 的 MJPEG 約 10–20 Mbps，會把這套系統
# 要量測的那條 5G 上行吃垮。上行維持 H.264，轉碼在地面站做。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY apps/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 共用驅動層。放在 app 之前：它變動頻率低，讓這層的快取活久一點。
COPY libs ./libs
# PX4 事件字典（執行期資料；版本綁定見該目錄 README）
COPY reference/px4-events ./reference/px4-events
COPY apps/backend/app ./app
ENV PYTHONPATH=/srv/libs
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "38000"]
