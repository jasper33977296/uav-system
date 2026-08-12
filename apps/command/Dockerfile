# command 服務：自製 GCS 的指令能力（與 backend ingest 分離的獨立服務）。
#
# build context 是 **repo 根**（不是 apps/command）——見 docker-compose.yml。
# 原因：共用的 `libs/autopilot` 兩個服務都要裝得到，而 Docker 不能 COPY
# context 之外的東西（issue 026 B2）。
FROM python:3.12-slim
WORKDIR /srv
COPY apps/command/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 共用驅動層。放在 app 之前：它變動頻率低，讓這層的快取活久一點。
COPY libs ./libs
COPY apps/command/app ./app
ENV PYTHONPATH=/srv/libs
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "38001"]
