"use client";
import { useEffect, useState } from "react";

import { evText } from "@/lib/evtext";
import type { UavEvent } from "@/lib/store";

/** 事件詳情 modal（ui-spec §2.7，使用者核准 2026-08-12）。
 *
 * 018 分層定案：列表＝摘要人話、modal＝細節層。內容依型別：
 * STATUSTEXT＝原句全文不翻譯；vehicle_event＝event_id＋args hex＋已知解譯
 * （A.2 metadata 落地後 detail 若帶 text/message 同欄自動顯示）；其餘＝
 * detail 鍵值表。誠實原則：加工顯示與原始 JSON 並存（raw 摺疊不預設展開）。
 * 即時頁抽屜與回放頁三角共用（單一 modal，新點替換）。
 */
export type ModalEvent = Pick<UavEvent, "id" | "time" | "type"> & {
  severity: string;                      // 回放頁 Ev 的 severity 是寬字串
  detail: Record<string, unknown>;
  source?: string | null;
  drone?: string | null;
  timeFirst?: string;                    // 折疊事件的首次時間（store 客端保留）
};

const SEV: Record<string, { label: string; color: string }> = {
  critical: { label: "危急", color: "#a01818" },
  warning: { label: "警告", color: "#fab219" },
  info: { label: "資訊", color: "#8f8b80" },
};

// 精確到毫秒（§2.7 文字圖）；清單的秒級時間不夠對 log
const fmtMs = (t: string) => {
  const d = new Date(t);
  return `${d.toLocaleString("zh-TW", { hour12: false })}.`
    + `${d.getMilliseconds().toString().padStart(3, "0")}`;
};
const fmtHms = (t: string) =>
  new Date(t).toLocaleTimeString("zh-TW", { hour12: false });

// 鍵值表的數字加工顯示（≤3 位小數去雜訊）；精確原值在 raw JSON
const fmtVal = (v: unknown): string =>
  typeof v === "number" && !Number.isInteger(v) ? v.toFixed(3) : String(v);

export default function EventModal({ ev, onClose, mixed = false }: {
  ev: ModalEvent; onClose: () => void;
  mixed?: boolean;   // 混機時模式句加語意括注（§0.2d）
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const sev = SEV[ev.severity] ?? { label: ev.severity, color: "#8f8b80" };
  const d = ev.detail ?? {};
  const count = typeof d.count === "number" ? d.count : 0;
  const srcTxt = ev.source === "vehicle" ? "機上訊息" : "系統事件";
  // vehicle_event 的解譯欄（metadata 翻譯落地後後端會帶；名稱雙讀）
  const decoded = typeof d.text === "string" ? d.text
    : typeof d.message === "string" ? d.message : null;

  // 鍵值表列（主呈現）：statustext 以全文塊呈現不進表；count 由折疊列呈現
  const kvRows: [string, unknown][] =
    ev.type === "statustext" ? []
      : Object.entries(d).filter(([k]) => k !== "count"
          && !(ev.type === "vehicle_event" && (k === "text" || k === "message")));

  const copyAll = () => {
    const lines = [
      `[${sev.label}] ${evText(ev as Parameters<typeof evText>[0], { mixed })}`,
      `時間 ${fmtMs(ev.time)}`
        + (count > 1 && ev.timeFirst ? `（首次 ${fmtMs(ev.timeFirst)}，×${count}）` : ""),
      `${ev.drone ?? "—"} · ${srcTxt} · type=${ev.type} · id=${ev.id}`,
      ...(ev.type === "statustext" && typeof d.text === "string" ? [d.text] : []),
      ...kvRows.map(([k, v]) => `${k}: ${fmtVal(v)}`),
      `raw: ${JSON.stringify(d)}`,
    ];
    navigator.clipboard.writeText(lines.join("\n")).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  };

  return (
    <div className="evm-mask" onClick={onClose}>
      <div className="evm card" role="dialog" aria-modal="true"
        aria-label="事件詳情" onClick={(e) => e.stopPropagation()}>
        <div className="evm-head">
          <span className={`evm-title${d.parse_failed ? " ev-unreadable" : ""}`}>
            {evText(ev as Parameters<typeof evText>[0], { mixed })}</span>
          <span className="chip">
            <span className="dot" style={{ background: sev.color }} />{sev.label}
          </span>
          <span className="spacer" />
          <button className="btn-plain btn-sm" aria-label="關閉" onClick={onClose}>✕</button>
        </div>
        <div className="evm-meta">{fmtMs(ev.time)}</div>
        <div className="evm-meta">{ev.drone ?? "—"} · {srcTxt}</div>
        <hr className="evm-hr" />

        {ev.type === "statustext" && typeof d.text === "string" && (
          <pre className="evm-text">{d.text}</pre>
        )}
        {decoded && ev.type === "vehicle_event" && (
          <pre className="evm-text">{decoded}</pre>
        )}
        {/* 版本旗標（§2.7 c）：**去重單位是視圖不是頁面**——modal 的遮罩
            （.evm-mask，fixed inset:0）會把事件卡標頭那句蓋掉，使用者正在這裡
            讀翻譯而唯一的版本聲明看不見，所以 unknown 也要在 modal 標一次。
            回放頁沒有事件流卡，靠這裡涵蓋（不為那頁發明頁面層位置）。
            mismatch 給完整句子＋warn 底，unknown 用次要色，兩者仍分得開 */}
        {d.dict_fw_match === "mismatch" && (
          <div className="evm-mismatch">
            ⚠ 版本不符，翻譯可能不準
            {typeof d.dict_fw === "string" && (
              <span className="imu-unit">（字典 {d.dict_fw}）</span>
            )}
          </div>
        )}
        {d.dict_fw_match === "unknown" && (
          <div className="evm-dictnote">
            未能確認字典版本與機上韌體相符
            {typeof d.dict_fw === "string" && (
              <span className="imu-unit">（字典 {d.dict_fw}）</span>
            )}
          </div>
        )}
        {kvRows.length > 0 && (
          <div className="evm-kv">
            {kvRows.map(([k, v]) => (
              <div className="evm-kv-row" key={k}>
                <span className="evm-k">{k}</span>
                <span className="evm-v">{fmtVal(v)}</span>
              </div>
            ))}
          </div>
        )}

        <details className="evm-raw">
          <summary>原始 JSON</summary>
          <pre className="evm-text">{JSON.stringify(
            { id: ev.id, time: ev.time, severity: ev.severity, type: ev.type,
              source: ev.source ?? null, detail: d }, null, 2)}</pre>
        </details>

        {count > 1 && (
          <div className="evm-meta">
            重複 ×{count}
            {ev.timeFirst && `　${fmtHms(ev.timeFirst)} – ${fmtHms(ev.time)}`}
          </div>
        )}
        <div className="evm-foot">
          <span className="spacer" />
          <button className="btn-plain btn-sm" onClick={copyAll}>
            {copied ? "已複製" : "複製全文"}
          </button>
        </div>
      </div>
    </div>
  );
}
