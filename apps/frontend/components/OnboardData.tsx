"use client";
import { useEffect, useMemo, useState } from "react";

import { useUavStore } from "@/lib/store";

/** 機上資料（ui-spec §2.8 Phase 1，使用者核准 2026-08-12）。
 *
 * 三層定位：儀表看 IMU 卡、log 看事件卡、**原始看這裡**。
 * 抽屜緊湊卡（事件卡上方）＝感測健康位＋訊息統計＋完整檢視入口；
 * Inspector sheet（右側全高）＝型別清單 Hz 降冪、點列展開最新欄位值
 * （方言原樣不翻譯——誠實）、未知型別顯 #id 不隱藏、齡 >5s 整列淡化。
 * 資料源＝後端登錄表廣播 1–2Hz（014 Phase B）——不是即時封包流，
 * sheet 底部如實標注。無登錄表資料＝整卡不畫（feature-detect 慣例）。
 */

// SYS_STATUS 健康位人話（缺位不畫；未知名原樣顯示）
const SENSOR_LABELS: Record<string, string> = {
  gyro: "陀螺", accel: "加速", mag: "磁力", baro: "氣壓", gps: "GPS",
  battery: "電池", motor: "馬達", rc: "遙控",
};

const fmtVal = (v: unknown): string =>
  typeof v === "number" && !Number.isInteger(v) ? v.toFixed(3)
    : Array.isArray(v) ? JSON.stringify(v)   // 陣列欄（四元數/多芯電壓）帶括號
    : String(v);

function InspectorSheet({ droneId, onClose }: { droneId: string; onClose: () => void }) {
  const reg = useUavStore((s) => s.registry[droneId]);
  const name = useUavStore((s) => s.fleet[droneId]?.drone_name) ?? "—";
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Set<number>>(new Set());
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const rows = useMemo(() => {
    // hz=null（一次性訊息無率）沉底：排序鍵取 -1
    const list = [...(reg?.messages ?? [])].sort((a, b) => (b.hz ?? -1) - (a.hz ?? -1));
    if (!q.trim()) return list;
    const needle = q.trim().toLowerCase();
    // 搜尋＝型別名/編號子字串（§2.8 規則 3）
    return list.filter((m) =>
      (m.name ?? "").toLowerCase().includes(needle)
      || `#${m.id}`.includes(needle) || `${m.id}`.includes(needle));
  }, [reg, q]);

  return (
    <div className="evm-mask insp-mask" onClick={onClose}>
      <div className="insp card" role="dialog" aria-modal="true"
        aria-label="機上資料完整檢視" onClick={(e) => e.stopPropagation()}>
        <div className="evm-head">
          <span className="evm-title">機上資料 — {name}</span>
          <span className="spacer" />
          <button className="btn-plain btn-sm" aria-label="關閉" onClick={onClose}>✕</button>
        </div>
        <input className="insp-search" placeholder="搜尋訊息型別…"
          value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="insp-list">
          <div className="insp-row insp-headrow">
            <span>型別</span><span className="num">Hz</span>
            <span className="num">齡</span><span />
          </div>
          {rows.map((m) => {
            const stale = (m.age_s ?? 0) > 5;
            const opened = open.has(m.id);
            const fields = Object.entries(m.fields ?? {});
            return (
              <div key={m.id} className={stale ? "insp-stale" : undefined}>
                <button className="insp-row insp-tap"
                  onClick={() => setOpen((s) => {
                    const n = new Set(s);
                    if (n.has(m.id)) n.delete(m.id); else n.add(m.id);
                    return n;
                  })}>
                  {/* 未知型別顯示 id 不隱藏（§2.8 文字圖）——收到什麼列什麼 */}
                  <span className="insp-name">{m.name ?? `#${m.id}`}</span>
                  {/* hz=null＝一次性/首見訊息還沒有率（實測 MISSION_ACK 炸過
                      toFixed 整頁白屏）。設計師裁定：null 欄留空——「還不知道」
                      不是 0.0 也不是 —；型別名照列（收到什麼列什麼） */}
                  <span className="num">{typeof m.hz === "number" ? m.hz.toFixed(1) : ""}</span>
                  <span className="num">{typeof m.age_s === "number" ? `${m.age_s.toFixed(1)}s` : ""}</span>
                  <span className="insp-arrow">{opened ? "▾" : "▸"}</span>
                </button>
                {opened && fields.length > 0 && (
                  <div className="insp-fields">
                    {fields.map(([k, v]) => (
                      <div className="evm-kv-row" key={k}>
                        <span className="evm-k">{k}</span>
                        <span className="evm-v">
                          {fmtVal(v)}
                          {/* bitmask 提示：附 hex（raw 值仍在前，不取代） */}
                          {m.displays?.[k] === "bitmask" && typeof v === "number"
                            && ` (0x${v.toString(16)})`}
                          {m.units?.[k] &&
                            <span className="imu-unit"> {m.units[k]}</span>}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {opened && fields.length === 0 && (
                  <div className="insp-fields hint-line">（登錄表未帶此型別欄位值）</div>
                )}
              </div>
            );
          })}
          {rows.length === 0 && <div className="empty">無符合的訊息型別</div>}
        </div>
        {/* 誠實標注：這是 1–2Hz 登錄表快照，不是即時封包流（§2.8 規則 1） */}
        <div className="hint-line">更新率 1–2Hz（登錄表快照，非即時封包流）</div>
      </div>
    </div>
  );
}

export default function OnboardDataCard() {
  const { primaryId, selectedId } = useUavStore();
  const effective = selectedId ?? primaryId;
  const reg = useUavStore((s) => (effective ? s.registry[effective] : undefined));
  const [sheetOpen, setSheetOpen] = useState(false);
  // 登錄表沒資料＝整卡不畫（feature-detect；後端 014 Phase B 上線前的空態）
  if (!effective || !reg || (!reg.messages.length && !reg.sensors.length)) return null;

  const totalHz = reg.messages.reduce((t, m) => t + (m.hz ?? 0), 0);
  return (
    <div className="card">
      <h3>機上資料</h3>
      {reg.sensors.length > 0 && (
        <div className="obd-sensors">
          <span className="imu-lab">感測</span>
          {reg.sensors.map((sn) => (
            <span key={sn.name} className={`obd-sn ${sn.ok ? "" : "obd-bad"}`}>
              {sn.ok ? "●" : "✗"}{SENSOR_LABELS[sn.name] ?? sn.name}
            </span>
          ))}
        </div>
      )}
      <div className="obd-sensors">
        <span className="imu-lab">訊息</span>
        <span className="obd-stat">
          {reg.messages.length} 型別 · 共 {totalHz.toFixed(0)} Hz
        </span>
        <span className="spacer" />
        <button className="btn-plain btn-sm" onClick={() => setSheetOpen(true)}>
          完整檢視
        </button>
      </div>
      {sheetOpen && (
        <InspectorSheet droneId={effective} onClose={() => setSheetOpen(false)} />
      )}
    </div>
  );
}
