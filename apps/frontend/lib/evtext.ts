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
    // sysid 位址變更（47a384d 後 note 已是完整中文句，補來源位址即可）
    case "sysid_addr_change":
      return `${d.note ?? "sysid 來源位址變更"}`
        + `${d.from_addr && d.to_addr ? `（來源 ${d.from_addr} → ${d.to_addr}）` : ""}`;
    // 5G 細節收摺疊後，cell 變化靠事件流呈現（issue 018 簡單案例先行）
    case "cell_change":
      return `serving cell 換手：PCI ${d.from_pci ?? "?"}`
        + `${d.from_band ? `（${d.from_band}）` : ""} → PCI ${d.to_pci ?? "?"}`
        + `${d.to_band ? `（${d.to_band}）` : ""}`;
    default:              return `${e.type} ${JSON.stringify(d)}`;
  }
}
