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
 * 用法：node scripts/uitest/empty_state.mjs [--url http://localhost:33000]
 */
import { chromium } from "playwright-core";

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
  if (wsEvents.length) {
    // WS 路徑要單獨測：REST 補歷史會遮住它的失效（兩條路都能填同一個清單，
    // 只測合併結果的話，WS 壞掉時畫面照樣有內容）
    await page.routeWebSocket(/\/ws\/telemetry/, (ws) => {
      for (const e of wsEvents) ws.send(JSON.stringify({ type: "event", event: e }));
    });
  }
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
    name: "malformed-row｜一筆 detail 非法 JSON → 其餘仍在（回歸）",
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

const b = await chromium.launch({ executablePath: EXE,
  args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"] }).catch((e) => {
    console.error(`skip：無法啟動瀏覽器（${e.message.split("\n")[0]}）`);
    console.error("skip ≠ pass：測不了就是沒有證據。設 CHROME_PATH 指向 chromium。");
    process.exit(2);
  });

let failed = 0;
const d = await discrimination(b);
console.log(`${d.pass ? "✓" : "✗"} 鑑別力自檢｜${d.note}`);
if (!d.pass) failed++;
for (const c of CASES) {
  let r;
  try { r = await c.run(b); } catch (e) { r = { pass: false, note: `例外：${e.message.slice(0, 90)}` }; }
  console.log(`${r.pass ? "✓" : "✗"} ${c.name}${r.note ? `　→ ${r.note}` : ""}`);
  if (!r.pass) failed++;
}
await b.close();
console.log(failed ? `\n${failed} 項失敗` : "\n全部通過");
process.exit(failed ? 1 : 0);
