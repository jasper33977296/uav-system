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
    // ── 路徑進度（2026-09-06）────────────────────────────────────────
    // **說「機上第 N 項」不說「第 N 個航點」**：這是機端的 seq，ArduPilot
    // 把 home 算成 seq 0，跟我方航點索引差 1。換算是驅動層的事，在這裡
    // 直接寫「航點」等於把一個錯誤的數字講得很肯定（state.ts 同一條紀律）。
    case "mission_progress": {
      const tot = typeof d.total === "number" ? `，共 ${d.total} 項` : "";
      if (d.first_sight === true)
        return `連上時路徑已在機上第 ${d.to ?? "?"} 項${tot}`;
      return `路徑進度：機上第 ${d.from ?? "?"} 項 → 第 ${d.to ?? "?"} 項${tot}`;
    }
    case "waypoint_reached":
      return `已到達機上第 ${d.seq ?? "?"} 項`
        + `${typeof d.total === "number" ? `（共 ${d.total} 項）` : ""}`;
    case "mission_state": {
      // **不猜沒見過的值**：認不得就照原文顯示（後端存的是數字，翻譯在這裡，
      // 韌體新增狀態時寧可顯示原字串也不要翻錯）
      const N: Record<string, string> = {
        unknown: "不明", no_mission: "無路徑", not_started: "未開始",
        active: "執行中", paused: "暫停", complete: "已完成",
      };
      const n = (v: unknown) =>
        typeof v === "string" ? (N[v] ?? v) : "不明";
      return `路徑狀態：${n(d.from)} → ${n(d.to)}`;
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
      return `路徑「${d.mission ?? "未命名"}」${d.why ? `：${d.why}` : ""}`;
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
    // 機上代理才知道的事（issues/057）：地面失聯、接管、失聯處置……
    // text 是代理寫好的整句。**晚到、缺號、時間不可信都要講出來**——
    // 這些事多半是失聯期間記下、恢復後才送到的，不說的話看起來像即時的
    case "agent_notice": {
      const notes: string[] = [];
      if (typeof d.late_s === "number") notes.push(`晚 ${durText(d.late_s)}才送到`);
      if (typeof d.gap_before === "number" && d.gap_before > 0)
        notes.push(`這之前有 ${d.gap_before} 則沒收到`);
      if (d.at_unsynced === true) notes.push("機上時鐘未對時，時間不可信");
      const text = typeof d.text === "string" && d.text ? d.text : `代理事件 ${d.kind ?? "?"}`;
      return notes.length ? `${text}（${notes.join("；")}）` : text;
    }
    // 飛控參數（issues/058 A）。**「是不是我們改的」一定要說**——2026-09-21
    // 花了數小時才發現參數被系統以外的人改過，而畫面一個字都沒說
    case "param_changed": {
      const via = typeof d.via_command === "number"
        ? "經由地面站指令服務" : "不是經由地面站指令服務";
      // 舊值是從上一趟的架次快照借來的，還是我們一路看著的——**意思不同**：
      // 前者只說得出「上一趟飛的時候是這樣」
      const since = typeof d.last_confirmed_at === "string"
        ? (d.compared_to === "session_snapshot"
          ? `；對照上一趟 ${shortTime(d.last_confirmed_at)} 解鎖時的快照`
          : `；上次確認舊值是 ${shortTime(d.last_confirmed_at)}`) : "";
      return `參數 ${d.name ?? "?"}：${pv(d.old)} → ${pv(d.new)}（${via}${since}）`;
    }
    case "params_changed":
      return `${d.count ?? "?"} 個參數一次改變`
        + (typeof d.not_via_command === "number" && d.not_via_command > 0
          ? `（其中 ${d.not_via_command} 個不是經由地面站指令服務）` : "")
        + "——詳情看明細";
    case "param_baseline":
      return `第一次記下這台機的 ${d.count ?? "?"} 個參數（之後變了才說得出來）`;
    case "params_added":
      return `出現 ${d.count ?? "?"} 個以前沒見過的參數（多半是韌體更新）`;
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

/** 秒數 → 「N 秒」「M 分」「H 小時 M 分」。**不寫小數**（067：「7.4 小時」
 * 要心算，而且小數點看起來像精確量測）。 */
/** 參數值：整數照整數寫，其餘最多 6 位有效數字。**null 寫「?」**——
 * NaN 過 JSON 邊界會變 null（lib/jsonsafe），那是「讀到了但不是數字」，
 * 寫成 0 或空白都是在編一個值。 */
function pv(v: unknown): string {
  if (typeof v !== "number") return "?";
  return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(6)));
}

/** ISO 時刻 → 「9/21 15:12」（本地時區）。事件列要短；完整時刻在明細裡。 */
function shortTime(iso: string): string {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${t.getMonth() + 1}/${t.getDate()} ${p(t.getHours())}:${p(t.getMinutes())}`;
}

export function durText(s: number): string {
  if (s < 60) return `${Math.round(s)} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s - h * 3600) / 60);
  return m ? `${h} 小時 ${m} 分` : `${h} 小時`;
}

/** 意圖協定的動作 → 人話。**照枚舉列，不猜字串**（與 CommandPanel 的
 * INTENT_LABELS 同一份說法；漏一個就顯示原代號）。 */
function intentLabel(a: unknown): string {
  const L: Record<string, string> = {
    start_mission: "開始執行路徑", pause: "中斷路徑", resume: "繼續路徑",
    change_route: "更換路徑", rtl: "返航", land: "降落",
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
