/** 事件以人話呈現：JSON 直出是畫面最大的視覺雜訊（simple-first：
 * 事件 log 是唯一的常駐文字區，句式必須是人話）。
 * SidePanel 與簡約 HUD 的事件列共用。 */
import { modeLabel } from "@/lib/modeVerb";
import type { UavEvent } from "@/lib/store";

export function evText(
  e: Pick<UavEvent, "type" | "detail"> & { severity?: UavEvent["severity"] },
  // 混機（≥2 種 autopilot 在線）時模式名加語意括注（§0.2d 規則 3）。
  // **判斷模式請用 detail 的 *_verb，不得比對模式名字串**——PX4 HOLD 與
  // ArduPilot LOITER 是同一件事，比字串在混機必錯
  opts: { mixed?: boolean } = {},
): string {
  const d = e.detail as Record<string, number | string | boolean | undefined>;
  // detail 解析失敗（lib/jsonb.ts）：顯示狀態句，**不顯示壞掉的原文**——
  // 半截 JSON 放在內容欄位會看起來像內容，使用者會試圖從亂碼推測發生了
  // 什麼。原文留在 detail.raw，modal 的鍵值表看得到。
  // severity 不在這裡處理也不受影響：它是獨立欄位，**critical 事件即使
  // detail 壞掉紅點照樣要紅**——讀不懂內容不等於事件變不嚴重
  if (d.parse_failed === true) return "無法解讀的訊息";
  const sinr = typeof d.sinr === "number" ? `SINR ${d.sinr.toFixed(1)} dB` : "";
  switch (e.type) {
    // PX4 Events 協定（Phase A.2 0296db5）：metadata 文字解析落地前顯示
    // 人話骨架；args hex 不裸出（那是翻譯原料）。解析落地後同列自動帶全文
    case "vehicle_event": {
      const sev = e.severity === "critical" ? "危急"
        : e.severity === "warning" ? "警告" : "資訊";
      return `機上事件 #${d.event_id ?? "?"}（${sev}）`;
    }
    // 機上訊息（STATUSTEXT）：原文不翻譯（event-stream-design 定案）；
    // ×N 折疊計數由事件卡的列尾徽章呈現，不進文字
    case "statustext":
      return `${d.text ?? ""}`;
    // 影像錄製（022 §2.9）：錄影是附屬功能，句子明說主資料不受影響
    case "video_recording_failed":
    case "video_recording_interrupted":
      return `影像錄製中斷${d.reason ? `（${d.reason}）` : ""}——遙測與紀錄不受影響`;
    case "video_recording_resumed": return "影像錄製已恢復";
    case "link_degraded": return `訊號劣化 · ${sinr}`;
    case "link_lost":     return `訊號瀕斷 · ${sinr}`;
    case "link_recovered":return `訊號恢復 · ${sinr}`;
    case "mode_change": {
      // 原廠名不翻譯；verb 缺或未知一律不註記（不猜、不硬翻）
      const m = (name: unknown, verb: unknown) =>
        modeLabel(typeof name === "string" ? name : "?",
          typeof verb === "string" ? verb : null, opts.mixed === true);
      return `模式 ${m(d.from, d.from_verb)} → ${m(d.to, d.to_verb)}`;
    }
    // ── 任務進度（2026-09-06）────────────────────────────────────────
    // **說「機上第 N 項」不說「第 N 個航點」**：這是機端的 seq，ArduPilot
    // 把 home 算成 seq 0，跟我方航點索引差 1。換算是驅動層的事，在這裡
    // 直接寫「航點」等於把一個錯誤的數字講得很肯定（state.ts 同一條紀律）。
    case "mission_progress": {
      const tot = typeof d.total === "number" ? `，共 ${d.total} 項` : "";
      if (d.first_sight === true)
        return `連上時任務已在機上第 ${d.to ?? "?"} 項${tot}`;
      return `任務進度：機上第 ${d.from ?? "?"} 項 → 第 ${d.to ?? "?"} 項${tot}`;
    }
    case "waypoint_reached":
      return `已到達機上第 ${d.seq ?? "?"} 項`
        + `${typeof d.total === "number" ? `（共 ${d.total} 項）` : ""}`;
    case "mission_state": {
      // **不猜沒見過的值**：認不得就照原文顯示（後端存的是數字，翻譯在這裡，
      // 韌體新增狀態時寧可顯示原字串也不要翻錯）
      const N: Record<string, string> = {
        unknown: "不明", no_mission: "無任務", not_started: "未開始",
        active: "執行中", paused: "暫停", complete: "已完成",
      };
      const n = (v: unknown) =>
        typeof v === "string" ? (N[v] ?? v) : "不明";
      return `任務狀態：${n(d.from)} → ${n(d.to)}`;
    }
    // sysid 位址變更（47a384d 後 note 已是完整中文句，補來源位址即可）
    case "sysid_addr_change":
      return `${d.note ?? "sysid 來源位址變更"}`
        + `${d.from_addr && d.to_addr ? `（來源 ${d.from_addr} → ${d.to_addr}）` : ""}`;
    // 5G 細節收摺疊後，cell 變化靠事件流呈現（issue 018 簡單案例先行）
    case "cell_change":
      return `serving cell 換手：PCI ${d.from_pci ?? "?"}`
        + `${d.from_band ? `（${d.from_band}）` : ""} → PCI ${d.to_pci ?? "?"}`
        + `${d.to_band ? `（${d.to_band}）` : ""}`;

    // ── 以下為資訊頁（2026-09-07）補上的型別 ──────────────────────
    // 這些事件一直都在寫，只是從來沒有人回頭讀——**歷史檢視一上線，
    // 它們就成了畫面上最常出現的那幾種**，而它們原本全部落到 default
    // 的 JSON 傾印。一頁的 JSON 就是一頁沒有人會讀的東西。
    case "failsafe":
      return `機上進入緊急狀態${d.state ? `（${d.state}）` : ""}`;
    case "rc_link":
      // detail.text 是機上代理寫好的整句（「⚠ 遙控器離線——此時不得起飛」）
      return typeof d.text === "string" && d.text
        ? d.text
        : d.rc_link === false ? "遙控器離線" : "遙控器連上了";
    case "mission_shown":
      return `任務「${d.mission ?? "未命名"}」${d.why ? `：${d.why}` : ""}`;
    case "intent_sent":
      return `意圖 ${intentLabel(d.action)} 已下達`
        + `${d.executor ? `（${execLabel(d.executor)}）` : ""}`
        + `${d.reason ? `——${d.reason}` : ""}`;
    case "intent_cleared":
      return `意圖 ${intentLabel(d.action)} 結束`
        + `${d.reason ? `——${d.reason}` : ""}`;
    case "intent_guard_refused":
      // reason 很長（守門會把「該怎麼辦」一起講完）；清單截斷、modal 看全文
      return `守門擋下 ${intentLabel(d.action)}`
        + `${d.state ? `（當時 ${d.state}）` : ""}${d.reason ? `：${d.reason}` : ""}`;
    case "sysid_claimed":
      return `sysid ${d.sysid ?? "?"} 由「${d.drone ?? "?"}」認領`
        + `${d.how ? `（${d.how}）` : ""}`;
    case "sysid_reassign_needed":
    case "identity_mismatch":
      return typeof d.reason === "string" && d.reason ? d.reason : e.type;
    case "vehicle_ack":
      return typeof d.text === "string" && d.text
        ? d.text
        : `飛控回應 ${d.command_name ?? d.command ?? "指令"}`
          + `${d.result_name ? `：${d.result_name}` : ""}`;
    case "driver_disagreement": {
      const n = Array.isArray(d.fields) ? (d.fields as unknown[]).length : 0;
      return `機上與地面站對同一份遙測算出不同結果${n ? `（${n} 項）` : ""}`;
    }

    // **認不得的型別：先找它自己帶的那句話，再退回傾印。**
    // `text`／`reason`／`note` 是後端寫事件時的慣例欄位（多半已經是整句中文）。
    // 型別名照留在前面——**不知道那是什麼事件時，代號是唯一的線索**，
    // 把它藏起來只會讓人查不到源頭。
    default: {
      const said = [d.text, d.reason, d.note].find(
        (v): v is string => typeof v === "string" && v.length > 0);
      return said ? `${e.type}：${said}` : `${e.type} ${JSON.stringify(d)}`;
    }
  }
}

/** 意圖協定的動作 → 人話。**照枚舉列，不猜字串**（與 CommandPanel 的
 * INTENT_LABELS 同一份說法；漏一個就顯示原代號）。 */
function intentLabel(a: unknown): string {
  const L: Record<string, string> = {
    start_mission: "開始任務", pause: "中斷任務", resume: "繼續任務",
    change_route: "更換任務", rtl: "返航", land: "降落",
    abort: "中止（原地懸停）", disarm: "上鎖", takeoff: "起飛", arm: "解鎖",
  };
  return typeof a === "string" ? (L[a] ?? a) : "?";
}

/** 誰執行的。`agent`＝機上代理自己動手、`ground`＝地面站下的。
 * **這一格不能省**：同一個動作由誰做，事後追責與除錯的方向完全不同。 */
function execLabel(x: unknown): string {
  const L: Record<string, string> = { agent: "機上代理執行", ground: "地面站執行" };
  return typeof x === "string" ? (L[x] ?? x) : "?";
}
