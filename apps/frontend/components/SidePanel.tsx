"use client";
import { useEffect, useRef, useState } from "react";

import { colorFor } from "@/components/droneLayer";
import { classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const AP_LABELS: Record<string, string> = { px4: "PX4", ardupilot: "ArduPilot" };

function Metric({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="metric">
      <div className="label">{label}</div>
      <div className="value">
        {value}
        {unit && <span className="unit">{unit}</span>}
      </div>
    </div>
  );
}

/** SINR sparkline：原始（muted 40% 1px）＋滑動平均平滑（2px）並存——
 * 誠實原則禁止只畫平滑線；hover 出 crosshair＋原始/平滑/分級 tooltip。 */
function Sparkline({ data }: { data: number[] }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hi, setHi] = useState<number | null>(null);
  if (data.length < 2) return null;
  const w = 320, h = 40;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const X = (i: number) => (i / (data.length - 1)) * w;
  const Y = (v: number) => h - 4 - ((v - min) / span) * (h - 8);
  const pts = (vals: number[]) => vals.map((v, i) => `${X(i)},${Y(v)}`).join(" ");
  const W10 = 10;   // 5Hz × 10 樣本 ≈ 2s 窗
  const smooth = data.map((_, i) => {
    const s = data.slice(Math.max(0, i - W10 + 1), i + 1);
    return s.reduce((a, b) => a + b, 0) / s.length;
  });
  const onMove = (e: React.PointerEvent) => {
    const r = wrapRef.current!.getBoundingClientRect();
    const idx = Math.round(((e.clientX - r.left) / r.width) * (data.length - 1));
    setHi(Math.max(0, Math.min(data.length - 1, idx)));
  };
  return (
    // viewBox + 100% 寬：側欄寬度是彈性的（clamp），寫死 px 會在縮放時爆出卡片
    <div className="spark-wrap" ref={wrapRef}
      onPointerMove={onMove} onPointerLeave={() => setHi(null)}>
      <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
           role="img" aria-label="SINR 近期趨勢">
        <polyline points={pts(data)} fill="none" stroke="var(--muted)"
                  strokeWidth="1" strokeOpacity="0.4" vectorEffect="non-scaling-stroke" />
        <polyline points={pts(smooth)} fill="none" stroke="var(--series-1)"
                  strokeWidth="2" vectorEffect="non-scaling-stroke" />
        {hi != null && (
          <line x1={X(hi)} x2={X(hi)} y1={0} y2={h} stroke="var(--ink-2)"
                strokeWidth="1" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
        )}
      </svg>
      {hi != null && (
        <div className="spark-tip">
          原始 {data[hi].toFixed(1)} · 平滑(2s) {smooth[hi].toFixed(1)} dB
          · {classifySinr(data[hi]).label.split(" ")[0]}
        </div>
      )}
    </div>
  );
}

const fmt = (v: number | null | undefined, digits = 1) =>
  v == null ? "—" : v.toFixed(digits);

/** 事件以人話呈現：JSON 直出是側欄最大的視覺雜訊。 */
function evText(e: { type: string; detail: Record<string, unknown> }): string {
  const d = e.detail as Record<string, number | string | boolean | undefined>;
  const sinr = typeof d.sinr === "number" ? `SINR ${d.sinr.toFixed(1)} dB` : "";
  switch (e.type) {
    case "link_degraded": return `訊號劣化 · ${sinr}`;
    case "link_lost":     return `訊號瀕斷 · ${sinr}`;
    case "link_recovered":return `訊號恢復 · ${sinr}`;
    case "mode_change":   return `模式 ${d.from ?? "?"} → ${d.to ?? "?"}`;
    // 5G 細節收摺疊後，cell 變化靠事件流呈現（issue 018 簡單案例先行）
    case "cell_change":
      return `serving cell 換手：PCI ${d.from_pci ?? "?"}`
        + `${d.from_band ? `（${d.from_band}）` : ""} → PCI ${d.to_pci ?? "?"}`
        + `${d.to_band ? `（${d.to_band}）` : ""}`;
    default:              return `${e.type} ${JSON.stringify(d)}`;
  }
}

export default function SidePanel() {
  const { live, events, fleet, primaryId, selectedId, select, sinrHistories } = useUavStore();
  const link = live?.link;
  const cls = link?.sinr != null ? classifySinr(link.sinr) : null;
  const effective = selectedId ?? primaryId;
  const ids = Object.keys(fleet);

  // 5G 詳細摺疊：展開狀態記 localStorage（IA 定案配套，同起飛高度前例）
  const [sigOpen, setSigOpen] = useState(false);
  useEffect(() => { setSigOpen(localStorage.getItem("sig-detail-open") === "1"); }, []);
  const onSigToggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    const o = e.currentTarget.open;
    setSigOpen(o);
    localStorage.setItem("sig-detail-open", o ? "1" : "0");
  };

  const gpsTxt = live?.gps_fix == null ? "—"
    : live.gps_fix >= 3 ? `3D·${live.satellites ?? "—"}`
    : live.gps_fix === 2 ? `2D·${live.satellites ?? "—"}` : "無定位";

  // 事件流：連續同類（type+severity+機）摺疊成一列 ×N（events 新的在前）
  const grouped = events.reduce<{ e: (typeof events)[0]; n: number }[]>((acc, e) => {
    const last = acc[acc.length - 1];
    if (last && last.e.type === e.type && last.e.severity === e.severity
        && last.e.drone === e.drone) last.n += 1;
    else acc.push({ e, n: 1 });
    return acc;
  }, []);

  return (
    <aside className="panel">
      {/* 機隊選擇器：多機時切換側欄顯示哪台（圓點色＝該機在地圖上的球體色） */}
      {ids.length > 1 && (
        <div className="chips">
          {ids.map((id) => (
            <button key={id}
              className={`chip chip-btn ${id === effective ? "chip-on" : ""}`}
              onClick={() => select(id)}>
              <span className="dot" style={{ background: colorFor(id) }} />
              {fleet[id].drone_name ?? id.slice(0, 8)}
            </button>
          ))}
        </div>
      )}
      {/* 飛行狀態卡（restyle §2）：徽章列 → 主數字 → 徽章列。
          姿態角移出常駐版面（機名 hover 查——查問題資訊，不佔層級） */}
      <div className="card">
        <div className="badges">
          <span className="name-lg"
            title={`姿態：Roll ${fmt(live?.roll)}° · Pitch ${fmt(live?.pitch)}°`}>
            {live?.drone_name ?? "飛行狀態"}
          </span>
          {live?.autopilot && (
            <span className="chip">{AP_LABELS[live.autopilot] ?? live.autopilot}</span>
          )}
          <span className="chip">
            <span className="dot" style={{
              background: live?.ready ? "var(--status-ok)" : "var(--status-serious)" }} />
            {live?.ready ? "就緒" : "未就緒"}
          </span>
          {live?.flight_mode && <span className="chip">{live.flight_mode}</span>}
        </div>
        <div className="metrics">
          <Metric label="相對高度" value={fmt(live?.alt_rel)} unit="m" />
          <Metric label="地速" value={fmt(live?.ground_speed)} unit="m/s" />
          <Metric label="垂直速度" value={fmt(live?.vertical_speed)} unit="m/s" />
        </div>
        <div className="badges sub">
          <span className="chip">電量 {fmt(live?.battery_pct, 0)}%</span>
          <span className="chip">GPS {gpsTxt}</span>
          {/* 360°＝0°：四捨五入後也可能碰到 360（359.6°），一律正規化 */}
          <span className="chip">
            航向 {live?.heading == null ? "—" : Math.round(live.heading) % 360}°
          </span>
        </div>
      </div>

      <div className="card">
        <h3>無人機訊號品質<span className="h3-note">平滑 2s 窗</span></h3>
        <div className="hero">
          <span className="num">{fmt(link?.sinr)}</span>
          <span className="unit">dB SINR</span>
          {cls && (
            <span className="chip">
              <span className="dot" style={{ background: cls.color }} />
              {cls.label.split(" ")[0]}
            </span>
          )}
          {link?.in_interference_zone && (
            <span className="chip">
              <span className="dot" style={{ background: "#d03b3b" }} />
              干擾區內
            </span>
          )}
        </div>
        <Sparkline data={(effective && sinrHistories[effective]) || []} />
        {/* 次要數字一列；其餘收「詳細」摺疊（IA 定案：漸進揭露＋展開記憶） */}
        <div className="metrics">
          <Metric label="RSRP" value={fmt(link?.rsrp)} unit="dBm" />
          <Metric label="RTT" value={fmt(link?.rtt_ms)} unit="ms" />
          <Metric label="丟包率" value={fmt(link?.packet_loss_pct)} unit="%" />
        </div>
        <details className="sig-detail" open={sigOpen} onToggle={onSigToggle}>
          <summary>詳細（RSRQ · PCI · 頻帶 · CQI · 模式）</summary>
          <div className="metrics">
            <Metric label="RSRQ" value={fmt(link?.rsrq)} unit="dB" />
            <Metric label="PCI" value={link?.pci?.toString() ?? "—"} />
            <Metric label="頻帶" value={link?.band ?? "—"} />
            <Metric label="CQI" value={link?.cqi?.toString() ?? "—"} />
            <Metric label="模式" value={link?.nr_mode ?? "—"} />
            <Metric
              label="下行吞吐"
              value={link?.throughput_down_kbps != null ? (link.throughput_down_kbps / 1000).toFixed(0) : "—"}
              unit="Mbps"
            />
          </div>
        </details>
      </div>

      <div className="card card-grow">
        <h3>事件</h3>
        <div className="events">
          {events.length === 0 && <div className="empty">尚無事件</div>}
          {/* critical 列標 danger 底（安全資訊不漸進揭露）；連續同類 ×N */}
          {grouped.map(({ e, n }) => (
            <div className={`event ${e.severity === "critical" ? "ev-crit" : ""}`} key={e.id}>
              <span
                className="dot"
                style={{
                  background:
                    e.severity === "critical" ? "#a01818" : e.severity === "warning" ? "#fab219" : "#8f8b80",
                }}
              />
              <time>{new Date(e.time).toLocaleTimeString("zh-TW", { hour12: false })}</time>
              {e.drone && <span className="ev-drone">{e.drone}</span>}
              <span className="detail">{evText(e)}</span>
              {n > 1 && <span className="ev-n">×{n}</span>}
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}
