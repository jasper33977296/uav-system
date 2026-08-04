"use client";
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

export default function SidePanel() {
  const { live, events, sinrHistory } = useUavStore();
  const link = live?.link;
  const cls = link?.sinr != null ? classifySinr(link.sinr) : null;

  return (
    <aside className="panel">
      <div className="card">
        <h3>飛行狀態</h3>
        <div className="metrics">
          <Metric label="相對高度" value={fmt(live?.alt_rel)} unit="m" />
          <Metric label="地速" value={fmt(live?.ground_speed)} unit="m/s" />
          <Metric label="垂直速度" value={fmt(live?.vertical_speed)} unit="m/s" />
          <Metric label="電量" value={fmt(live?.battery_pct, 0)} unit="%" />
          <Metric label="衛星數" value={live?.satellites?.toString() ?? "—"} />
          <Metric label="模式" value={live?.flight_mode ?? "—"} />
        </div>
      </div>

      <div className="card">
        <h3>5G 鏈路品質</h3>
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
        <Sparkline data={sinrHistory} />
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

      <div className="card">
        <h3>事件</h3>
        <div className="events">
          {events.length === 0 && <div className="empty">尚無事件</div>}
          {events.map((e) => (
            <div className="event" key={e.id}>
              <span
                className="dot"
                style={{
                  background:
                    e.severity === "critical" ? "#a01818" : e.severity === "warning" ? "#fab219" : "#898781",
                }}
              />
              <time>{new Date(e.time).toLocaleTimeString("zh-TW", { hour12: false })}</time>
              <span className="type">{e.type}</span>
              <span className="detail">{JSON.stringify(e.detail)}</span>
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}
