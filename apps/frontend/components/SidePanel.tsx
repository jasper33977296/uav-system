"use client";
import { useEffect, useRef, useState } from "react";

import { EventsCard } from "@/components/SimpleHud";
import { classifySinr } from "@/lib/signal";
import { type ImuData, type Telemetry, useUavStore } from "@/lib/store";

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

// ── IMU 卡（ui-spec §2.6，使用者核准 2026-08-11）────────────────────
// 三軸一律純數字（等寬對齊、x/y/z 弱字欄標一次、單位弱字）；唯一圖形＝
// 振動量條（PX4 權威門檻 30/60 才有有意義的刻度）。缺欄整列不畫、不佔位。

const DEG = 180 / Math.PI;
const has = (...v: (number | null | undefined)[]) => v.some((x) => x != null);

/** 三軸列：共用 .imu-row 網格欄位，跨列數字對齊 */
function Axis3({ label, v, unit, digits = 2, scale = 1 }: {
  label: string; v: (number | null | undefined)[];
  unit: string; digits?: number; scale?: number;
}) {
  if (!has(...v)) return null;
  return (
    <div className="imu-row">
      <span className="imu-lab">{label}</span>
      {v.map((x, i) => (
        <span className="imu-num" key={i}>
          {x == null ? "" : (x * scale).toFixed(digits)}
        </span>
      ))}
      <span className="imu-unit">{unit}</span>
    </div>
  );
}

/** 振動量條：0–90 橫軌、門檻刻度 30/60、x/y/z 三細條疊放、超標染色。
 * clipping 三軸計數列於條下。 */
function VibBar({ imu }: { imu: ImuData }) {
  const axes: [string, number | null | undefined][] = [
    ["x", imu.vibration_x], ["y", imu.vibration_y], ["z", imu.vibration_z]];
  if (!has(...axes.map(([, v]) => v))) return null;
  const FULL = 90;   // 滿刻度＝danger 門檻的 1.5 倍（門檻本身才是語意錨點）
  const color = (v: number) =>
    v >= 60 ? "var(--status-danger)" : v >= 30 ? "var(--status-warn)"
      : "var(--status-ok)";
  const worst = Math.max(...axes.map(([, v]) => v ?? 0));
  const clip = [imu.clipping_0, imu.clipping_1, imu.clipping_2];
  return (
    <>
      <div className="imu-row imu-vib">
        <span className="imu-lab">振動</span>
        <div className="vib-track">
          {axes.map(([ax, v]) => v != null && (
            <div className="vib-bar" key={ax} title={`${ax} ${v.toFixed(1)}`}
              style={{ width: `${Math.min(100, (v / FULL) * 100)}%`,
                       background: color(v) }} />
          ))}
          <span className="vib-tick" style={{ left: `${(30 / FULL) * 100}%` }}
            data-v="30" />
          <span className="vib-tick" style={{ left: `${(60 / FULL) * 100}%` }}
            data-v="60" />
        </div>
        <span className="imu-num" style={{ color: color(worst) }}>
          {worst.toFixed(1)}
        </span>
      </div>
      {has(...clip) && (
        <div className="imu-row">
          <span className="imu-lab" />
          <span className="imu-sub">
            clipping {clip.map((c) => c ?? 0).join(" / ")}
          </span>
        </div>
      )}
    </>
  );
}

function ImuCard({ live }: { live: Telemetry | null }) {
  const imu = live?.imu ?? ({} as ImuData);
  // 航向本就是 yaw——併入姿態列（§2.6 安置 3）；360°＝0° 正規化照舊
  const yaw = live?.heading == null ? null : Math.round(live.heading) % 360;
  const showAxisHead =
    has(imu.xacc, imu.yacc, imu.zacc, imu.xgyro, imu.ygyro, imu.zgyro,
        imu.xmag, imu.ymag, imu.zmag);
  return (
    <div className="card">
      <h3>IMU</h3>
      <div className="imu-grid">
        {has(live?.roll, live?.pitch, yaw) && (
          <div className="imu-row imu-att">
            <span className="imu-lab">姿態</span>
            <span className="imu-sub">
              Roll <b>{fmt(live?.roll)}°</b>　Pitch <b>{fmt(live?.pitch)}°</b>
              　Yaw <b>{yaw ?? "—"}°</b><span className="imu-unit">（航向）</span>
            </span>
          </div>
        )}
        <Axis3 label="角速率" unit="°/s" digits={1} scale={DEG}
          v={[imu.rollspeed, imu.pitchspeed, imu.yawspeed]} />
        {showAxisHead && (
          <div className="imu-row imu-axhead">
            <span className="imu-lab" />
            <span className="imu-num">x</span>
            <span className="imu-num">y</span>
            <span className="imu-num">z</span>
            <span className="imu-unit" />
          </div>
        )}
        <Axis3 label="加速度" unit="m/s²" v={[imu.xacc, imu.yacc, imu.zacc]} />
        <Axis3 label="陀螺" unit="rad/s" digits={3}
          v={[imu.xgyro, imu.ygyro, imu.zgyro]} />
        <Axis3 label="磁力" unit="µT" digits={1} v={[imu.xmag, imu.ymag, imu.zmag]} />
        {imu.temperature != null && (
          <div className="imu-row">
            <span className="imu-lab">溫度</span>
            <span className="imu-sub"><b>{imu.temperature.toFixed(1)}</b>
              <span className="imu-unit"> °C</span></span>
          </div>
        )}
        <VibBar imu={imu} />
        {imu.abs_pressure != null && (
          <div className="imu-row">
            <span className="imu-lab">氣壓</span>
            <span className="imu-sub"><b>{imu.abs_pressure.toFixed(1)}</b>
              <span className="imu-unit"> hPa</span>
              {imu.pressure_alt != null &&
                <span className="imu-unit">（氣壓高度 {imu.pressure_alt.toFixed(0)} m）</span>}
            </span>
          </div>
        )}
        {/* 導航估計：EKF 融合值，括注來源語意、不冒充原始感測（§2.6 安置）。
            相對高度住 HUD（唯一的家）；海拔 alt_msl 原本無家，入此。 */}
        {has(live?.vertical_speed, live?.alt_msl) && (
          <div className="imu-row">
            <span className="imu-lab">導航估計</span>
            <span className="imu-sub">
              {live?.vertical_speed != null &&
                <>垂直速度 <b>{fmt(live.vertical_speed)}</b>
                  <span className="imu-unit"> m/s</span>　</>}
              {live?.alt_msl != null &&
                <>海拔 <b>{fmt(live.alt_msl)}</b>
                  <span className="imu-unit"> m</span></>}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

export default function SidePanel() {
  const { live, primaryId, selectedId, sinrHistories } = useUavStore();
  const link = live?.link;
  const cls = link?.sinr != null ? classifySinr(link.sinr) : null;
  const effective = selectedId ?? primaryId;

  // 5G 詳細摺疊：展開狀態記 localStorage（IA 定案配套，同起飛高度前例）
  const [sigOpen, setSigOpen] = useState(false);
  useEffect(() => { setSigOpen(localStorage.getItem("sig-detail-open") === "1"); }, []);
  const onSigToggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    const o = e.currentTarget.open;
    setSigOpen(o);
    localStorage.setItem("sig-detail-open", o ? "1" : "0");
  };

  // 單一住所（simple-first 第五輪）：高度/速度/電量/訊號格住 HUD、事件住
  // 底部單行、機隊選擇住左上色點——本抽屜只留「訊號品質」與「專業數值」，
  // 不與畫面上任何元素重複
  return (
    <aside className="panel">
      <div className="card">
        <h3>訊號品質<span className="h3-note">平滑 2s 窗</span></h3>
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
        {/* 圖例回歸地圖左下常駐（ui-spec §2 使用者定案）——不在卡內 */}
      </div>

      {/* IMU 卡（§2.6，原專業數值卡改造）：就緒/模式/GPS 與面板狀態列重複
          已刪；機型 chip 移任務控制面板對象行；航向併入 Yaw */}
      <ImuCard live={live} />

      {/* 事件卡（使用者二次修訂）：住抽屜、IMU 卡下方 */}
      <EventsCard />
    </aside>
  );
}
