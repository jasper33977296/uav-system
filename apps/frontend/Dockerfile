# 部署形映像：依賴安裝＋production build 都在建構時完成，
# 執行 `next start` 服務預編譯產物——不跑 dev server。
# 開發模式由 docker-compose.dev.yml 覆寫（bind mount 原始碼 + next dev）。
FROM node:24-bookworm
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build
EXPOSE 33000
CMD ["npm", "run", "start", "--", "-p", "33000", "-H", "0.0.0.0"]
