# build context 是 **repo 根**（不是 apps/backend）——見 docker-compose.yml。
# 原因：共用的 `libs/autopilot` 兩個服務都要裝得到，而 Docker 不能 COPY
# context 之外的東西（issue 026 B2）。
FROM python:3.12-slim
WORKDIR /srv
COPY apps/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 共用驅動層。放在 app 之前：它變動頻率低，讓這層的快取活久一點。
COPY libs ./libs
# PX4 事件字典（執行期資料；版本綁定見該目錄 README）
COPY reference/px4-events ./reference/px4-events
COPY apps/backend/app ./app
ENV PYTHONPATH=/srv/libs
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "38000"]
