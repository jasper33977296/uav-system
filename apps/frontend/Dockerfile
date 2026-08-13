# 部署形映像：依賴安裝＋production build 都在建構時完成，
# 執行 `next start` 服務預編譯產物——不跑 dev server。
# 開發模式由 docker-compose.dev.yml 覆寫（bind mount 原始碼 + next dev）。
FROM node:24-bookworm
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
# 版本標記（ui-spec §0.2f）：**必須在 build 之前**——NEXT_PUBLIC_* 是在
# 建置時被內聯進產物的，run 期才給的環境變數進不去前端 bundle。
# 未給即為空，UI 會顯示「未知版本」而不是留白（不知道要說出來）。
ARG BUILD_SHA=""
ENV NEXT_PUBLIC_BUILD_SHA=$BUILD_SHA
COPY . .
RUN npm run build
EXPOSE 33000
CMD ["npm", "run", "start", "--", "-p", "33000", "-H", "0.0.0.0"]
