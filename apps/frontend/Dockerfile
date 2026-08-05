# 依賴烤進映像：`next` 在建構時就安裝完成，執行期不依賴網路或 host npm。
# 開發模式的原始碼由 bind mount 提供；node_modules 用匿名 volume 保護
# （見 docker-compose.yml），避免被 bind mount 遮蔽。
FROM node:24-bookworm
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
EXPOSE 33000
CMD ["npm", "run", "dev"]
