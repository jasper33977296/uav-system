#!/usr/bin/env node
/** 空態鑑別測試：「畫面空白」到底是誠實空態，還是渲染/取得失敗在冒充它。
 *
 * **為什麼需要這一套**（設計師 2026-08-13 裁定，ui-spec §6b）：我們刻意設計了
 * 誠實空態（不知道≠不行、缺欄不畫、尚無事件），但**失效在畫面上與誠實空態完全
 * 同形**。使用者看到空白只會以為「本來就沒有」，不會懷疑畫面壞了——所以這類
 * 缺陷可以活很久。**空白既不能當通過條件，也不能當失敗證據**：唯一有鑑別力的
 * 做法是注入「已知必有內容」的資料，再斷言它真的出現。
 *
 * 排序依據不是實作難度，是**那個空態宣告的「沒事」有多接近「安全」**——
 * 凡失效會落在「比真實狀況更令人安心」那一側的排前面：
 *   1. 事件流「尚無事件」＝宣告沒有異常發生（本檔）——最危險：靜默失效會讓
 *      故障中的飛機看起來健康。
 *   2. 分級軌跡（無 SINR 欄就不畫）＝宣告沒量到訊號——誤導研究判讀，不涉飛安。
 *   3. 就緒三態的灰空心＝宣告資料不足——失效方向落在保守側，本身是安全的。
 *
 * 紀律沿用 scripts/conformance：**skip ≠ pass**（測不了就是沒有證據，不能算過），
 * 且內建**反向驗證**——先證明斷言在資料到不了時真的會失敗，否則整套只是在
 * 對什麼都喊 pass。
 *
 * **證據等級（設計師 2026-08-13 要求）——十一項全綠看起來等價，證據強度不**：
 *   [實測] 有修前/修後對照：`malformed-row`（即時頁事件流：注入 3 筆其中 1 筆
 *          壞，修前 0 列＋「尚無事件」→ 修後 3 列）。
 *   [讀碼] 機制由讀碼確定、只有修後綠燈：`replay-events`、`drones-list`、
 *          `missions-list`、`field-map`。這四處是**確定性拋出**（一列非法 JSON
 *          → 整批進 catch），沒有時序也沒有機率，讀碼即可證明；補基準線要在共用
 *          環境上瞬間部署壞版本（前端 --reload 即部署，使用者可能正在飛），
 *          代價與收益不成比例。
 *   判準：**修前基準線的價值取決於機制的不確定性**——症狀是「有 vs 沒有」且
 *   「沒有」也可能是合法狀態時（如熱區競態）必須量修前，否則無法區分「修好了」
 *   與「本來就沒東西」；確定性拋出則不需要。
 *   **不標的話，日後有人會把「讀碼推論」當成「實測確認」。**
 *
 * 用法：node scripts/uitest/empty_state.mjs [--url http://localhost:33000]
 */
import { chromium } from "playwright-core";
import { PNG } from "pngjs";

const URL_BASE = (() => {
  const i = process.argv.indexOf("--url");
  return i > 0 ? process.argv[i + 1] : "http://localhost:33000";
})();
const EXE = process.env.CHROME_PATH
  ?? `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`;

const ev = (id, detail, over = {}) => ({
  id, time: new Date(Date.now() - id * 1000).toISOString(),
  type: "statustext", source: "vehicle", severity: "info", drone_id: "uav-s2",
  detail, ...over,
});
const OK = (n) => JSON.stringify({ text: `訊息${n}`, count: 1 });

/** 開一頁、依 opts 佈置事件來源、展開專業面板（事件卡住抽屜最下）。 */
async function openHud(browser, { rest = [], restStatus = 200, wsEvents = [] }) {
  const page = await (await browser.newContext({
    viewport: { width: 1400, height: 900 } })).newPage();
  await page.route("**/api/events**", (r) => (restStatus === 200
    ? r.fulfill({ json: rest })
    : r.fulfill({ status: restStatus, json: { detail: "boom" } })));
  // **一律攔 WS**（即使不注入）：真後端正在跑，它推的即時事件會混進斷言——
  // 實測注入 3 筆卻量到 5 筆。測試必須自己決定畫面上有什麼，否則量到的是
  // 「我的注入＋當下環境」，換個時間就換個結果
  await page.routeWebSocket(/\/ws\/telemetry/, (ws) => {
    // WS 路徑要單獨測：REST 補歷史會遮住它的失效（兩條路都能填同一個清單，
    // 只測合併結果的話，WS 壞掉時畫面照樣有內容）
    for (const e of wsEvents) ws.send(JSON.stringify({ type: "event", event: e }));
  });
  await page.goto(`${URL_BASE}/`, { waitUntil: "networkidle", timeout: 30000 });
  const drawer = page.locator('button:has-text("▤")').first();
  if (await drawer.count()) await drawer.click();
  await page.waitForTimeout(900);
  return page;
}

async function readCard(page) {
  return {
    rows: await page.locator(".event").count(),
    // 「元素在但內容空」等同失效：列數對但沒有字，照樣是壞的
    texts: (await page.locator(".event .detail").allInnerTexts())
      .map((t) => t.trim()).filter(Boolean),
    empty: await page.locator(".events .empty").count(),
  };
}


/** 架次清單頁共用：一筆 summary 是壞 JSON，其餘正常。 */
const DRONE_ID = "11111111-2222-3333-4444-555555555555";
const MISSION_ID = "99999999-8888-7777-6666-555555555555";

async function openWithBrokenSummary(browser, path) {
  const page = await (await browser.newContext({
    viewport: { width: 1400, height: 900 } })).newPage();
  // 機隊頁的架次表住在「該機」的展開區裡：注入的 drone_id 必須對得上畫面上
  // 那台機，否則量到的是別台機的空表（受測物不是我以為的那個）
  await page.route("**/api/drones**", (r) => (r.request().method() === "GET"
    ? r.fulfill({ json: [{ id: DRONE_ID, name: "測試機", model: null,
        serial_no: "t-1", is_simulated: true, connection_url: "udpin://0.0.0.0:1",
        status: "idle", created_at: new Date().toISOString(), is_primary: true }] })
    : r.continue()));
  // 任務頁的架次表按 mission_id 過濾，同理必須掛在畫面上那條路徑底下
  await page.route("**/api/missions**", (r) => (r.request().method() === "GET"
    ? r.fulfill({ json: [{ id: MISSION_ID, name: "測試路徑", is_active: false,
        waypoints: [], created_at: new Date().toISOString() }] })
    : r.continue()));
  const good = (id, n) => ({ id, drone_id: DRONE_ID, started_at: new Date().toISOString(),
    ended_at: new Date().toISOString(), mission_id: MISSION_ID,
    summary: JSON.stringify({ samples_total: n, avg_sinr: 10, min_sinr: 5, avg_rtt_ms: 40,
      max_alt_rel: 12 }) });
  await page.route("**/api/sessions**", (r) => (/\/track|\/video/.test(r.request().url())
    ? r.fulfill({ json: { link: [] } })
    : r.fulfill({ json: [good("s-1", 100), { ...good("s-2", 100), summary: "{壞掉的" },
                        good("s-3", 100)] })));
  await page.goto(`${URL_BASE}${path}`, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1000);
  // 兩頁的架次表都在收合區內，先展開才量得到（抽屜關著時 DOM 在但看不到）
  const row = page.locator(".member, .mcard").first();
  if (await row.count()) { await row.click(); await page.waitForTimeout(700); }
  return page;
}


// SINR 分級色（lib/signal.ts 的 LINK_CLASSES）與「無樣本」灰
const CLS = { good: [12, 163, 12], warning: [250, 178, 25], serious: [224, 94, 14],
  critical: [160, 24, 24], unknown: [143, 139, 128] };

/** 依分級色數地圖上的像素。抗鋸齒會混色，用容差並只數強匹配。 */
function classPixels(buf) {
  const png = PNG.sync.read(buf);
  const out = { good: 0, warning: 0, serious: 0, critical: 0, unknown: 0 };
  for (let i = 0; i < png.data.length; i += 4) {
    const [r, g, b] = [png.data[i], png.data[i + 1], png.data[i + 2]];
    for (const [k, c] of Object.entries(CLS)) {
      if (Math.abs(r - c[0]) < 26 && Math.abs(g - c[1]) < 26 && Math.abs(b - c[2]) < 26) {
        out[k]++; break;
      }
    }
  }
  return out;
}

/** 回放頁注入一條軌跡；sinrOf 回 null＝該點無樣本，key 可改欄位名（反向驗證用）。 */
async function openReplayTrack(browser, { sinrOf, key = "sinr" }) {
  const page = await (await browser.newContext({
    viewport: { width: 1200, height: 800 } })).newPage();
  const t0 = Date.now() - 60000;
  const link = Array.from({ length: 40 }, (_, i) => ({
    t: new Date(t0 + i * 1000).toISOString(),
    lat: 25.0553 + i * 4e-5, lon: 121.5067 + i * 1e-5, alt_rel: 5,
    [key]: sinrOf(i), rtt_ms: 40 }));
  await page.route("**/api/sessions/*/track*", (r) =>
    r.fulfill({ json: { link, telemetry: link } }));
  await page.route("**/api/sessions/*/video*", (r) => r.fulfill({ status: 404, json: {} }));
  await page.route("**/api/events**", (r) => r.fulfill({ json: [] }));
  await page.routeWebSocket(/\/ws\/telemetry/, () => {});
  await page.goto(`${URL_BASE}/replay/test-session`, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(2500);
  const px = classPixels(await page.screenshot({
    clip: { x: 0, y: 90, width: 900, height: 500 } }));
  await page.context().close();
  return px;
}

// 四個分級各佔一段（值挑在各級中央，不踩邊界）
const FOUR = (i) => [20, 9, 1, -8][Math.floor(i / 10)];

const CASES = [
  {
    name: "rest-history｜REST 補歷史 3 筆 → 3 列有字、無空態",
    async run(b) {
      const p = await openHud(b, { rest: [ev(1, OK(1)), ev(2, OK(2)), ev(3, OK(3))] });
      const r = await readCard(p); await p.context().close();
      return r.rows === 3 && r.texts.length === 3 && r.empty === 0
        ? { pass: true, note: `3 列有字` }
        : { pass: false, note: `列=${r.rows} 有字=${r.texts.length} 空態=${r.empty}` };
    },
  },
  {
    name: "ws-live｜REST 空、WS 推 2 筆 → 2 列（單獨驗即時路徑）",
    async run(b) {
      const p = await openHud(b, { rest: [],
        wsEvents: [{ ...ev(11, null), detail: { text: "即時一", count: 1 } },
                   { ...ev(12, null), detail: { text: "即時二", count: 1 } }] });
      const r = await readCard(p); await p.context().close();
      return r.rows === 2 && r.texts.length === 2
        ? { pass: true, note: "2 列有字" }
        : { pass: false, note: `列=${r.rows} 有字=${r.texts.length}` };
    },
  },
  {
    name: "[實測] malformed-row｜一筆 detail 非法 JSON → 其餘仍在（回歸）",
    async run(b) {
      // 2026-08-13 實測過的真實失效：JSON.parse 在 map 裡拋出 → 整批 seed 被
      // catch 吞掉 → 3 筆全消失、畫面「尚無事件」。這是最危險的那種空態
      const p = await openHud(b, { rest: [ev(1, OK(1)), ev(2, "{壞掉的"), ev(3, OK(3))] });
      const r = await readCard(p); await p.context().close();
      return r.rows === 3 && r.empty === 0
        ? { pass: true, note: "壞列不吃掉整批" }
        : { pass: false, note: `列=${r.rows} 空態=${r.empty}（一筆壞掉毀掉整批）` };
    },
  },
  {
    name: "honest-empty｜真的沒有事件 → 恰好顯示「尚無事件」",
    async run(b) {
      const p = await openHud(b, { rest: [] });
      const r = await readCard(p); await p.context().close();
      return r.rows === 0 && r.empty === 1
        ? { pass: true, note: "誠實空態本身正常" }
        : { pass: false, note: `列=${r.rows} 空態元素=${r.empty}` };
    },
  },
  {
    name: "filter-subsets｜三個篩選各自對，子集為空才顯示空態",
    async run(b) {
      const p = await openHud(b, { rest: [
        ev(1, OK(1)), ev(2, OK(2)),                                    // vehicle
        ev(3, JSON.stringify({ sinr: -3 }), { type: "link_degraded", source: "system" }),
      ] });
      const out = {};
      for (const [label, want] of [["全部", 3], ["機上訊息", 2], ["系統", 1]]) {
        await p.locator(`.ev-filter button:has-text("${label}")`).click();
        await p.waitForTimeout(250);
        const r = await readCard(p);
        out[label] = `${r.rows}/${want}${r.empty ? "＋空態" : ""}`;
        if (r.rows !== want || r.empty !== 0) out.bad = true;
      }
      await p.context().close();
      return out.bad
        ? { pass: false, note: JSON.stringify(out) }
        : { pass: true, note: Object.entries(out).map(([k, v]) => `${k} ${v}`).join("、") };
    },
  },
  {
    name: "unreadable-row｜壞列顯示狀態句、severity 不降級、不加符號",
    async run(b) {
      const p = await openHud(b, { rest: [
        { ...ev(1, "{壞掉的"), severity: "critical" }, ev(2, OK(2))] });
      const txt = (await p.locator(".event .detail").allInnerTexts()).map((t) => t.trim());
      const crit = await p.locator(".event.ev-crit").count();      // 紅列＝severity 保住
      const muted = await p.locator(".event .detail.ev-unreadable").count();
      const symbols = await p.locator(".event .ev-mismatch, .event .ev-unknown").count();
      const rawLeak = txt.some((t) => t.includes("壞掉的"));
      await p.context().close();
      const ok = txt.includes("無法解讀的訊息") && crit === 1 && muted === 1
        && symbols === 0 && !rawLeak;
      return { pass: ok, note: ok
        ? "狀態句＋次要色、critical 仍為紅列、無符號、原文不外洩"
        : `文字=${JSON.stringify(txt)} 紅列=${crit} 次要色=${muted} 符號=${symbols} 原文外洩=${rawLeak}` };
    },
  },
  {
    name: "[讀碼] replay-events｜回放頁一列壞 → 其餘事件標記仍在（輪詢路徑）",
    async run(b) {
      const page = await (await b.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
      const t0 = Date.now() - 60000;
      const link = Array.from({ length: 20 }, (_, i) => ({
        t: new Date(t0 + i * 1000).toISOString(), lat: 25.0553 + i * 1e-5, lon: 121.5067,
        alt_rel: i, sinr: 10 - i * 0.2, rtt_ms: 40 }));
      await page.route("**/api/sessions/*/track*", (r) =>
        r.fulfill({ json: { link, telemetry: link } }));
      await page.route("**/api/sessions/*/video*", (r) => r.fulfill({ status: 404, json: {} }));
      await page.route("**/api/events**", (r) => r.fulfill({ json: [
        { ...ev(1, OK(1)), time: new Date(t0 + 5000).toISOString() },
        { ...ev(2, "{壞掉的"), time: new Date(t0 + 10000).toISOString() },
        { ...ev(3, OK(3)), time: new Date(t0 + 15000).toISOString() }] }));
      await page.goto(`${URL_BASE}/replay/test-session`, { waitUntil: "networkidle", timeout: 30000 });
      await page.waitForTimeout(1500);
      // 事件標記住在「〓 圖表」抽屜裡：抽屜關著時 DOM 在但看不到，
      // 先開抽屜才是量到真的（2026-08-13 踩過一次）
      const sum = page.locator('summary:has-text("圖表")').first();
      if (await sum.count() && !(await page.locator("details.replay-drawer[open]").count()))
        await sum.click();
      await page.waitForTimeout(600);
      const n = await page.locator("details.replay-drawer polygon").count();
      await page.context().close();
      return n === 3 ? { pass: true, note: "3 個事件標記" }
        : { pass: false, note: `事件標記=${n}（應為 3）` };
    },
  },
  {
    name: "[讀碼] drones-list｜一筆 summary 壞 → 架次清單不消失",
    async run(b) {
      const p = await openWithBrokenSummary(b, "/drones");
      const n = await p.locator("tbody tr").count();
      await p.context().close();
      // 恰好 3：整批消失（0）與「壞列被靜默丟掉」（2）都要抓得到——
      // 後者是同一種病的縮小版，一樣是無聲的資料損失
      return n === 3 ? { pass: true, note: `${n} 列` }
        : { pass: false, note: `列=${n}（3 筆架次應全在，壞的那筆數值顯示「—」）` };
    },
  },
  {
    name: "[讀碼] missions-list｜一筆 summary 壞 → 架次清單不消失",
    async run(b) {
      const p = await openWithBrokenSummary(b, "/missions");
      const n = await p.locator("tbody tr").count();
      await p.context().close();
      return n === 3 ? { pass: true, note: `${n} 列` } : { pass: false, note: `列=${n}` };
    },
  },
  {
    name: "[讀碼] field-map｜一筆 summary 壞 → 場域頁不整頁白（render 期解析）",
    async run(b) {
      const p = await openWithBrokenSummary(b, "/compare");
      const txt = (await p.locator("body").innerText()).replace(/\s+/g, " ").trim();
      const canvas = await p.locator("canvas").count();
      await p.context().close();
      return txt.length > 20 && canvas > 0
        ? { pass: true, note: `頁面有內容、canvas ${canvas}` }
        : { pass: false, note: `文字長度=${txt.length} canvas=${canvas}（白畫面）` };
    },
  },
  {
    name: "[實測] graded-track｜四個分級都畫得出來（軌跡分級色）",
    async run(b) {
      const px = await openReplayTrack(b, { sinrOf: FOUR });
      const missing = ["good", "warning", "serious", "critical"].filter((k) => px[k] < 30);
      return missing.length === 0
        ? { pass: true, note: `良好 ${px.good}／尚可 ${px.warning}／劣化 ${px.serious}／瀕斷 ${px.critical} px` }
        : { pass: false, note: `缺分級：${missing.join("、")}（${JSON.stringify(px)}）` };
    },
  },
  {
    name: "[實測] no-sinr-gray｜整條無 SINR 樣本 → 灰線，且不冒充任何分級",
    async run(b) {
      const px = await openReplayTrack(b, { sinrOf: () => null });
      const colored = px.good + px.warning + px.serious + px.critical;
      // 灰線要真的畫出來（軌跡本身不能消失），且一格分級色都不准出現——
      // 「沒量到訊號」不得被畫成任何一個分級（不造假）
      return px.unknown > 30 && colored === 0
        ? { pass: true, note: `灰線 ${px.unknown} px、分級色 0` }
        : { pass: false, note: `灰=${px.unknown} 分級色=${colored}` };
    },
  },
];

/** 反向驗證：資料存在但拿不到（後端 500）時，rest-history 的斷言必須失敗。
 * 這一項若「通過」，代表整套測試沒有鑑別力——比任何單一測項失敗都嚴重。 */
async function discrimination(b) {
  const p = await openHud(b, { rest: [ev(1, OK(1)), ev(2, OK(2))], restStatus: 500 });
  const r = await readCard(p); await p.context().close();
  const assertionWouldPass = r.rows === 2 && r.texts.length === 2;
  return { pass: !assertionWouldPass, note: assertionWouldPass
    ? "斷言在後端 500 時仍通過＝這套測試量不出東西"
    : `後端 500 → 列=${r.rows}、畫面${r.empty ? "顯示「尚無事件」" : "無空態"}，斷言確實失敗` };
}

/** 第二個鑑別力自檢——這一項同時是本套的**主題實例**：
 * 把注入資料的欄位名從 sinr 改成 snr（＝上游改欄位、我方沒跟上），畫面會變成
 * **整條灰線**——與「這趟真的沒量到訊號」在畫面上完全同形。使用者只會以為
 * 沒量到，不會懷疑對映壞了。所以分級色的斷言必須在這個情境下失敗；
 * 若仍量到分級色，代表那些顏色不是來自注入資料，整套沒有鑑別力。 */
async function discriminationGraded(b) {
  const px = await openReplayTrack(b, { sinrOf: FOUR, key: "snr" });
  const colored = px.good + px.warning + px.serious + px.critical;
  return { pass: colored === 0, note: colored === 0
    ? `欄位名 sinr→snr 時整條變灰（${px.unknown} px）、分級色 0，斷言確實失敗`
    : `欄位名改掉仍量到 ${colored} 分級色＝顏色不是來自注入資料` };
}

const b = await chromium.launch({ executablePath: EXE,
  args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"] }).catch((e) => {
    console.error(`skip：無法啟動瀏覽器（${e.message.split("\n")[0]}）`);
    console.error("skip ≠ pass：測不了就是沒有證據。設 CHROME_PATH 指向 chromium。");
    process.exit(2);
  });

let failed = 0;
for (const [label, fn] of [["取得失敗", discrimination], ["欄位對映失敗", discriminationGraded]]) {
  const d = await fn(b);
  console.log(`${d.pass ? "✓" : "✗"} 鑑別力自檢（${label}）｜${d.note}`);
  if (!d.pass) failed++;
}
for (const c of CASES) {
  let r;
  try { r = await c.run(b); } catch (e) { r = { pass: false, note: `例外：${e.message.slice(0, 90)}` }; }
  console.log(`${r.pass ? "✓" : "✗"} ${c.name}${r.note ? `　→ ${r.note}` : ""}`);
  if (!r.pass) failed++;
}
await b.close();
console.log(failed ? `\n${failed} 項失敗` : "\n全部通過");
process.exit(failed ? 1 : 0);
