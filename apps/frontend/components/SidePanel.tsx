"use client";
import { useEffect, useRef, useState } from "react";

import InfoTip from "@/components/InfoTip";
import LogIndexSheet from "@/components/LogIndexSheet";
import OnboardDataCard from "@/components/OnboardData";
import { EventsCard } from "@/components/SimpleHud";
import { API, classifySinr } from "@/lib/signal";
import { ageText, staleLevel } from "@/lib/staleness";
import { type ImuData, type Telemetry, useUavStore } from "@/lib/store";

function Metric({ label, value, unit, derived }: {
  label: string; value: string; unit?: string;
  /** 這個值是後端從 modem 原始回應解出來的，不是模組直接報的欄位 */
  derived?: boolean;
}) {
  return (
    <div className="metric">
      <div className="label">
        {label}
        {derived && <span className="metric-derived" title="由模組原始回應解出（AT+GTCCINFO?）">＊</span>}
      </div>
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
        {/* 選中機座標（§2.6：簡約重整時被收掉、使用者要求回來）。放這裡
            而非 HUD：座標是「查詢時才要」的精確值，非常駐掃視項。
            與地圖右下的游標經緯度並存——那是「你指的位置」，這是「機在
            的位置」。相對高度在此重複顯示是刻意例外：抄座標時通常連高度
            一起抄，兩個數字分處畫面兩端會抄錯。無座標整列不畫。 */}
        <PosRow live={live} />
      </div>
    </div>
  );
}

function PosRow({ live }: { live: Telemetry | null }) {
  const [copied, setCopied] = useState(false);
  if (live?.lat == null || live?.lon == null) return null;
  const txt = `${live.lat.toFixed(6)}, ${live.lon.toFixed(6)}`;
  const copy = () => {
    navigator.clipboard.writeText(
      live.alt_rel != null ? `${txt}, ${live.alt_rel.toFixed(1)}m` : txt)
      .then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })
      .catch(() => {});
  };
  return (<>
    <div className="imu-row">
      <span className="imu-lab">位置</span>
      <button className="imu-pos" onClick={copy} title="點擊複製座標（含高度）">
        {txt}<span className="imu-unit"> {copied ? "已複製" : "⧉"}</span>
      </button>
    </div>
    {live.alt_rel != null && (
      <div className="imu-row">
        <span className="imu-lab" />
        <span className="imu-sub">相對高度 <b>{live.alt_rel.toFixed(1)}</b>
          <span className="imu-unit"> m</span></span>
      </div>
    )}
  </>);
}

/** ① 機況：**只放「會變、且看一眼就要知道」的事實**（原型第一張卡）。
 *
 * 機型、板號、韌體版本這類幾乎不變的欄位在無人機頁，不佔即時畫面。
 * 過舊的數值不留在畫面上（`staleLevel`）——一個灰色的「LOITER」還是一個模式，
 * 人讀到的仍然是「它在 LOITER」。 */
function StatusCard({ live }: { live: Telemetry | null }) {
  const lv = staleLevel(live?.telem_age_s);
  const old = lv === "old" || lv === "never";
  const F = ({ k, v, dim }: { k: string; v: string; dim?: boolean }) => (
    <div className="fact">
      <span className="fact-k">{k}</span>
      <span className={`fact-v${dim ? " dim" : ""}`}>{v}</span>
    </div>
  );
  return (
    <div className="card">
      <h3>{live?.drone_name ?? "無人機"}
        <span className="spacer" />
        <InfoTip tip="這一列只放會變、而且看一眼就要知道的事實。機型、板號、韌體版本那些幾乎不變的欄位在無人機頁。數值過舊時整格換成「—」——灰色的數字讀起來還是一個數字。" />
      </h3>
      {!live ? (
        // **這句話說的是我方**：還沒收到任何一筆。它不宣告「沒有機在線」——
        // 那是另一件事，而且我們沒有依據（§0.2e）
        <div className="empty">還沒有收到任何遙測。</div>
      ) : (
        <div className="facts">
          <F k="模式" v={old || !live.flight_mode ? "—" : live.flight_mode} dim={old} />
          <F k="鎖" v={old ? "—" : live.armed ? "已解鎖" : "上鎖"} dim={old} />
          <F k="GPS" v={old || live.gps_fix == null ? "—"
            : `fix ${live.gps_fix}${live.satellites != null ? ` · ${live.satellites} 顆` : ""}`}
            dim={old} />
          <F k="電壓" v={old || live.battery_voltage == null ? "—"
            : `${live.battery_voltage.toFixed(2)} V`} dim={old} />
          <F k="最後遙測" v={ageText(live.telem_age_s)} dim={lv !== "live"} />
        </div>
      )}
    </div>
  );
}

/** ④ 紀錄：**現在有沒有在記、機上那份回來了沒、要看就在這裡打開**。
 *
 * 這張卡不輪詢清單端點（`/api/captures` 會掃目錄，它的 docstring 明說「是人
 * 按出來的，不是熱路徑」）——回傳現況直接讀代理推上來的狀態（WS），檔案清單
 * 只在按下「看最近一份」時抓一次。 */
function RecordCard({ live }: { live: Telemetry | null }) {
  const agents = useUavStore((s) => s.agents);
  const [sheet, setSheet] = useState<{ url: string; title: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const rec = Boolean(live?.armed && live?.session_id);
  const ag = live?.drone_id ? agents[live.drone_id] : undefined;
  const up = ag?.record_upload;

  const openLatest = async () => {
    setBusy(true); setNote(null);
    try {
      const r = await fetch(`${API}/api/onboard-captures`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const files = (d.files ?? []).filter((f: { drone_id: string; status: string }) =>
        f.status === "complete" && (!live?.drone_id || f.drone_id === live.drone_id));
      if (!files.length) {
        // **「還沒有回傳」與「取不到」不同形**：這句話說的是前者
        setNote("這台機還沒有回傳過完整的機上錄製——飛一趟落地後再看。");
      } else {
        const f = files[0];
        setSheet({ url: `${API}${f.url}/index`, title: `${f.name} · ${f.drone_name ?? ""}` });
      }
    } catch (e) {
      setNote(`取不到錄製清單：${(e as Error).message}`);
    }
    setBusy(false);
  };

  return (
    <div className="card">
      <h3>紀錄
        <span className="spacer" />
        <InfoTip tip="「記錄中」的語意是現在的資料有沒有被寫進資料庫（armed 且已建立架次——待機時系統刻意不入庫）。機上錄製是另一份：飛控送出的東西，斷線那幾段只有它有。回傳要落地才會開始。" />
      </h3>
      <div className="facts">
        <div className="fact">
          <span className="fact-k">遙測</span>
          <span className={`fact-v${rec ? " rec-on" : " dim"}`}>
            {rec ? "記錄中" : "未記錄"}
          </span>
        </div>
        {live?.video_mode && (
          <div className="fact">
            <span className="fact-k">影像</span>
            <span className="fact-v">
              {live.video_mode === "on" ? "有錄"
                : live.video_mode === "off" ? "未錄影" : "沒有影像源"}
            </span>
          </div>
        )}
        <div className="fact">
          <span className="fact-k">機上</span>
          <span className={`fact-v${ag ? "" : " dim"}`}>
            {!ag ? "沒有代理"
              : !ag.fresh ? "不知道（代理沒在推）"
                : !up ? "看不到回傳狀況"
                  : up.current ? `回傳中 ${up.current}`
                    : up.pending > 0 ? `待回傳 ${up.pending} 份`
                      : "已同步"}
          </span>
        </div>
      </div>
      <div className="cmd-row" style={{ marginTop: 8 }}>
        <button className="btn-plain btn-sm" disabled={busy} onClick={openLatest}>
          {busy ? "查詢中…" : "看最近一份紀錄"}
        </button>
      </div>
      {note && <div className="hint-line">{note}</div>}
      {sheet && (
        <LogIndexSheet url={sheet.url} title={sheet.title}
          onClose={() => setSheet(null)} />
      )}
    </div>
  );
}

export default function SidePanel() {
  const { live, primaryId, selectedId, sinrHistories } = useUavStore();
  const link = live?.link;
  const cls = link?.sinr != null ? classifySinr(link.sinr) : null;
  // **分得出「模組報的」與「我方算的」**：後端在 raw._derived 記下解了哪幾欄
  // （app/modem_raw.py）。舊資料沒有這個鍵＝那些值本來就是模組填的
  const rawLink = link?.raw as { _derived?: { fields?: string[] } } | null | undefined;
  const derived = new Set<string>(rawLink?._derived?.fields ?? []);
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
      <StatusCard live={live} />

      <div className="card">
        <h3>訊號品質<InfoTip tip="大字＝最新一筆 SINR。走勢圖裡細線是原始樣本、粗線是 2 秒滑動平均——只畫平滑線會產生量測點之間沒有量到的值。分級門檻與 backend 的事件門檻同一出處。" /></h3>
        <div className="hero">
          <span className="num">{fmt(link?.sinr)}</span>
          <span className="unit">dB SINR</span>
          {cls && (
            <span className="chip">
              <span className="dot" style={{ background: cls.color }} />
              {cls.label.split(" ")[0]}
            </span>
          )}
          {/* 「干擾區內」標籤移除（使用者定案 2026-08-13：全站不做先驗
              成因標注）。機只知道訊號變差、不知道原因——這個 chip 是拿
              模擬器的場景設定當成對現場的事實斷言，真機上根本沒有這種
              資訊。訊號本身的呈現（SINR 分級、走勢、熱區）全部保留，
              那些是量測結果不是推測。後端 in_interference_zone 欄位仍在，
              若日後研究需要對照，應標成「模擬設定」而非現場事實 */}
        </div>
        <Sparkline data={(effective && sinrHistories[effective]) || []} />
        {/* 次要數字一列；其餘收「詳細」摺疊（IA 定案：漸進揭露＋展開記憶） */}
        <div className="metrics">
          <Metric label="RSRP" value={fmt(link?.rsrp)} unit="dBm" />
          <Metric label="RTT" value={fmt(link?.rtt_ms)} unit="ms" />
          <Metric label="丟包率" value={fmt(link?.packet_loss_pct)} unit="%" />
        </div>
        <details className="sig-detail" open={sigOpen} onToggle={onSigToggle}>
          <summary>Serving cell &amp; band</summary>
          <div className="metrics">
            <Metric label="RSRQ" value={fmt(link?.rsrq)} unit="dB" />
            <Metric label="PCI" value={link?.pci?.toString() ?? "—"}
              derived={derived.has("pci")} />
            <Metric label="NCI" value={link?.cell_id?.toString() ?? "—"}
              derived={derived.has("cell_id")} />
            <Metric label="Band" value={link?.band ?? "—"}
              derived={derived.has("band")} />
            <Metric label="CQI" value={link?.cqi?.toString() ?? "—"} />
            <Metric label="NR mode" value={link?.nr_mode ?? "—"} />
            <Metric
              label="下行吞吐"
              value={link?.throughput_down_kbps != null ? (link.throughput_down_kbps / 1000).toFixed(0) : "—"}
              unit="Mbps"
            />
          </div>
          {derived.size > 0 && (
            <div className="hint-line">
              ＊ 由模組原始回應解出（<code>AT+GTCCINFO?</code>）——欄位本身沒有值
            </div>
          )}
        </details>
        {/* 圖例回歸地圖左下常駐（ui-spec §2 使用者定案）——不在卡內 */}
      </div>

      {/* 事件卡：**訊號之後就是它**（原型第③段，使用者核准 2026-09-08 常駐版）。
          原本排在 IMU 之下——而 IMU 是排查用的原始層、不是飛行中一直在變的東西，
          把每次都要掃的事件流擠到第三張卡以下 */}
      <EventsCard />

      {/* 紀錄（原型第④段）：現在有沒有在記、機上那份回來了沒、要看就在這裡開 */}
      <RecordCard live={live} />

      {/* 以下是排查層，順序照使用者 2026-09-07 的指示：IMU 在上、機上資料最後
          （「這個沒那重要」）*/}
      <ImuCard live={live} />
      <OnboardDataCard />
    </aside>
  );
}
