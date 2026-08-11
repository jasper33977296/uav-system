"use client";
import { colorFor } from "@/components/droneLayer";
import { classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

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

function Sparkline({ data }: { data: number[] }) {
  if (data.length < 2) return null;
  const w = 320, h = 40;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const pts = data
    .map((v, i) => `${(i / (data.length - 1)) * w},${h - 4 - ((v - min) / span) * (h - 8)}`)
    .join(" ");
  return (
    // viewBox + 100% 寬：側欄寬度是彈性的（clamp），寫死 px 會在縮放時爆出卡片
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
         role="img" aria-label="SINR 近期趨勢">
      <polyline points={pts} fill="none" stroke="var(--series-1)" strokeWidth="2"
                vectorEffect="non-scaling-stroke" />
    </svg>
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
    default:              return `${e.type} ${JSON.stringify(d)}`;
  }
}

export default function SidePanel() {
  const { live, events, fleet, primaryId, selectedId, select, sinrHistories } = useUavStore();
  const link = live?.link;
  const cls = link?.sinr != null ? classifySinr(link.sinr) : null;
  const effective = selectedId ?? primaryId;
  const ids = Object.keys(fleet);

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
      <div className="card">
        <h3>飛行狀態{live?.drone_name ? ` · ${live.drone_name}` : ""}</h3>
        <div className="metrics">
          <Metric label="相對高度" value={fmt(live?.alt_rel)} unit="m" />
          <Metric label="地速" value={fmt(live?.ground_speed)} unit="m/s" />
          <Metric label="垂直速度" value={fmt(live?.vertical_speed)} unit="m/s" />
          <Metric label="電量" value={fmt(live?.battery_pct, 0)} unit="%" />
          <Metric label="衛星數" value={live?.satellites?.toString() ?? "—"} />
          <Metric label="模式" value={live?.flight_mode ?? "—"} />
          <Metric label="橫滾 Roll" value={fmt(live?.roll)} unit="°" />
          <Metric label="俯仰 Pitch" value={fmt(live?.pitch)} unit="°" />
          {/* 360°＝0°：四捨五入後也可能碰到 360（359.6°），一律正規化 */}
          <Metric label="航向"
            value={live?.heading == null ? "—" : (Math.round(live.heading) % 360).toString()}
            unit="°" />
        </div>
      </div>

      <div className="card">
        <h3>無人機訊號品質</h3>
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
        <div className="metrics">
          <Metric label="RSRP" value={fmt(link?.rsrp)} unit="dBm" />
          <Metric label="RSRQ" value={fmt(link?.rsrq)} unit="dB" />
          <Metric label="RTT" value={fmt(link?.rtt_ms)} unit="ms" />
          <Metric label="丟包率" value={fmt(link?.packet_loss_pct)} unit="%" />
          <Metric label="PCI" value={link?.pci?.toString() ?? "—"} />
          <Metric label="頻帶" value={link?.band ?? "—"} />
          <Metric
            label="下行吞吐"
            value={link?.throughput_down_kbps != null ? (link.throughput_down_kbps / 1000).toFixed(0) : "—"}
            unit="Mbps"
          />
          <Metric label="CQI" value={link?.cqi?.toString() ?? "—"} />
          <Metric label="模式" value={link?.nr_mode ?? "—"} />
        </div>
      </div>

      <div className="card card-grow">
        <h3>事件</h3>
        <div className="events">
          {events.length === 0 && <div className="empty">尚無事件</div>}
          {events.map((e) => (
            <div className="event" key={e.id}>
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
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}
