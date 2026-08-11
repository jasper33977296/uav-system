# 即時路線渲染工具評估（顆粒感＋閃爍，使用者要求換工具）

- 狀態：**deck.gl 附條件核可**（PM 2026-08-11；前端成本意見無阻斷因素
  即生效）。條件：(1) bundle 自足 self-host 零外連；(2) 效能驗收沿用
  P2 標準（1200 點×多機幀率不低於現行）；(3) 與 three.js 球體層共存
  實測——兩個 GL custom layer 的 interop 是主風險，前端評估必含；
  (4) P2 光照立體感正式移出範圍（寫入 017）。interop 若阻斷，
  退回自建方案再議。
- **前端評估結論（2026-08-11）：無阻斷，deck.gl 生效**。
  估工程量：即時頁替換 1 工作段＋回放/比較共用化 1 段；拾取內建
  （比較頁選中互動受益）。兩個警示入檔：(a) interleaved 與 **P3 terrain
  疊加有已知 caveat**——P3 gate 審查必重驗；(b) PathLayer 無光照（unlit）
  ——放棄光照為本定案的明示 trade-off，不接受事後翻案理由。
- 使用者反饋：即時頁路線「顆粒感太明顯」＋「一直閃爍」，要求找工具取代現有渲染。
- 關聯：issue 017 P2（本評估取代/吸收原 three.js 圓管方案的工具選型）

## 兩個症狀的根因（已確認）

| 症狀 | 根因 | 位置 |
|---|---|---|
| 顆粒感 | fill-extrusion 每段是水平樓板：斜向段高度量化成階梯＋段間色塊；P1 修了轉角縫但天花板還在 | `geo.ts ribbon()`＋`MapView` fill-extrusion 層 |
| 閃爍 | **每個 WS tick（5Hz×機數）整條絲帶重建＋`setData` 整源替換**——MapLibre 對 fill-extrusion 無增量更新，每次都是砍掉重練＋GPU 重上傳 | `MapView.tsx:220 subscribe → :251 setData` |

結論：閃爍是**更新模式**問題、顆粒感是 **mark 選型**問題——換工具可以一次解掉兩個，但更新模式不改的話換什麼都還是會閃。

## 候選工具

### A. deck.gl（MapboxOverlay interleaved 模式疊在 MapLibre 上）— **建議**

- **PathLayer**：3D 座標（經緯度＋高度）、公尺制寬度、`jointRounded` 圓角接頭、抗鋸齒——顆粒感直接消失，斜向段是連續斜帶不是樓梯。
- **顏色（前端評估修正 2026-08-11）**：PathLayer 是 per-path 上色（無
  per-vertex 漸變）→ 實作為「同分級連續段＝一條 path」的 **run 分割**，
  rounded joints 讓 run 交界視覺無縫。這與誠實原則「分級不插值」剛好
  同構——原 P2 的漸變過渡構想作廢，CVD 驗收項改驗「run 交界清晰可辨」。
- **更新模式**：binary attributes＋`updateTriggers`，append 新點只重算 attribute buffer，**無整源替換閃爍**。
- 生態成熟（Uber 開源，MapLibre 官方文件有整合指南）、離線可用（npm bundle，無外連）。
- 即時頁、回放頁、比較頁 3D 疊圖**同一個 layer 元件通吃**，P2 工程被吸收。
- three.js 球體機體層保留不動（deck.gl 與 custom layer 可共存）。
- 成本：新依賴 `@deck.gl/core`＋`@deck.gl/layers`＋`@deck.gl/mapbox` 約 +300KB gz；PathLayer 是平面帶（無光照立體感——P2 原本想要的圓管光照放棄，換取工具成熟度）。

### B. three.js 自建 TubeGeometry（P2 原案）

- 光照圓管、立體感最好；沿用既有球體層管線、零新依賴。
- 但 miter/端蓋/拾取/增量 buffer 管理全自建自維護——工程量與後續維護都貴，且閃爍要自己另外解（persistent scene＋增量 append）。
- 使用者的兩個抱怨（顆粒、閃爍）A 案都解得掉；光照是設計端的加分項不是使用者訴求。

### C. MapLibre line 層＋line-gradient（否決）

抗鋸齒平滑、零依賴，但 line 層**忽略高度**——3D 懸浮絲帶語意全失，與
017 定案方向相反。只在「2D 俯視模式」如果未來有需求時才相關。

## 建議

1. **主方案 A（deck.gl PathLayer）**：一次解掉顆粒感＋閃爍，三頁共用，
   P2 併入且工程量比自建 tube 小。需 PM 核可新依賴（bundle +300KB）。
2. **不等選型的 hotfix（前端可先做）**：絲帶重建節流到 1Hz＋只在新增樣本
   時 rebuild（append 判斷）——閃爍立即可感改善，與 A 案不衝突。
3. 瀕斷段 relief（P2 checklist 項）在 PathLayer 上的對應：瀕斷段寬度
   加倍或加白色細邊線（PathLayer 支援 per-segment 寬度/雙層 path），
   落地時一起驗。

## 縮放自適應粗細（使用者 2026-08-11 反饋追加）

固定公尺寬的問題：近看（視野 50m）3m 線佔畫面過粗，遠看（>500m）
被壓到 minPixels 太細。解法＝**物理錨定＋螢幕像素夾限**：

- 航跡 PathLayer：`getWidth 3`（公尺不變）＋ `widthMinPixels: 3`
  ＋ `widthMaxPixels: 8`——近看最多 8px（不再肥帶）、遠看至少 3px
  （恆可見）；中間區間仍隨縮放連續變化，保留距離感。
- 機體球（droneLayer）：同原則，投影後螢幕半徑夾在 [7px, 16px]
  （球體 scale 依相機距離動態算回公尺）。點擊命中半徑跟著夾後值走。
- 地面投影線／方向箭頭：同夾限原則（line-width 用 zoom interpolate）。

## 驗收（選型落地後）

同 017 方法學：SITL 飛行截圖——斜向段無階梯、5Hz 更新無閃爍
（錄 3 秒連拍比對）、瀕斷段可辨識、幀率不低於現行。
