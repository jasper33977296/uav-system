"use client";
/** 錄製檔的摘要索引，在網頁上打開（使用者定案 2026-09-07：「log 只能下載來看」）。
 *
 * 資料來自 `GET .../index`（後端 `app/logindex.py` 解析 tlog）。三個分頁的
 * 順序＝打開一份陌生紀錄時的問法：
 *
 *   訊息型別 → 機上有在送什麼、多快、分布在哪一段
 *   機上訊息 → 飛控說了什麼（原文，折疊）
 *   模式與曲線 → 什麼時候換模式、高度/電壓/振動長什麼樣
 *
 * 四條規矩：
 *  1. **大檔要等就說在等**：後端回 202＋百分比時顯示進度，不假裝畫面壞了
 *     （116 MB 實測 45 秒）。
 *  2. **索引落後就說落後**：`indexed_bytes < bytes`＝檔案還在寫，畫面明說
 *     索引只到哪裡，並給重建。
 *  3. **欄位值原樣不翻譯**：raw wire 單位配 raw 值（`cdegC`、`degE7`⋯），
 *     換算是判讀的事，不在這裡偷偷做。
 *  4. **這不是逐幀瀏覽**，也不假裝是——索引回答「這份檔長什麼樣」。
 */
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import InfoTip from "@/components/InfoTip";
import { EvDensity } from "@/lib/foldEvents";

export interface LogIndex {
  file: string; bytes: number; indexed_bytes: number; frames: number;
  span: number; t_start: number; t_end: number;
  types: {
    name: string; n: number; hz: number | null; first: number; last: number;
    fields: Record<string, unknown>; units: Record<string, string>;
    buckets: number[];
  }[];
  sysids: { id: string; n: number }[];
  main_sys: string | null;
  statustext: { sev: number; text: string; n: number; t: number; last: number;
    times: number[] }[];
  statustext_total: number; statustext_dropped: number;
  modes: { sys: string; t: number; mode: string }[];
  series: { key: string; label: string; unit: string;
    points: [number, number][]; refs?: number[] }[];
  build_s: number; empty_reason?: string;
}

const mb = (b: number) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);
const dur = (s: number) => s >= 3600
  ? `${Math.floor(s / 3600)} 時 ${Math.round((s % 3600) / 60)} 分`
  : s >= 60 ? `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒` : `${Math.round(s)} 秒`;
const clock = (unix: number) =>
  new Date(unix * 1000).toLocaleTimeString("zh-TW", { hour12: false });

/** MAVLink severity（`MAV_SEVERITY`）。**這是機上的判斷，不是我方的分級** */
const SEV_NAME: Record<number, string> = {
  0: "緊急", 1: "警報", 2: "危急", 3: "錯誤",
  4: "警告", 5: "注意", 6: "資訊", 7: "除錯",
};
const sevColor = (s: number) =>
  s <= 3 ? "#a01818" : s === 4 ? "#fab219" : "var(--muted)";

const fmtVal = (v: unknown): string =>
  typeof v === "number" && !Number.isInteger(v) ? v.toFixed(4)
    : Array.isArray(v) ? JSON.stringify(v) : String(v);

export default function LogIndexSheet({ url, title, onClose }: {
  url: string; title: string; onClose: () => void;
}) {
  const [idx, setIdx] = useState<LogIndex | null>(null);
  const [pct, setPct] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<"types" | "text" | "curve">("types");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [sys, setSys] = useState<string | null>(null);

  const load = useCallback(async (refresh = false) => {
    try {
      const r = await fetch(url + (refresh ? "?refresh=1" : ""));
      // **202 不是錯誤**：大檔在背景解析，帶著百分比回來
      if (r.status === 202) {
        const d = await r.json();
        setPct(typeof d.percent === "number" ? d.percent : 0);
        return false;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = (await r.json()) as LogIndex;
      setIdx(d); setPct(null);
      return true;
    } catch (e) {
      // 取不到不得長得像「這份檔是空的」
      setErr(`索引取不到：${(e as Error).message}`);
      return true;
    }
  }, [url]);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const done = await load();
      if (!done && !stop) timer = setTimeout(tick, 2000);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const mainSys = sys ?? idx?.main_sys ?? null;
  const types = useMemo(() => {
    const needle = q.trim().toUpperCase();
    return (idx?.types ?? []).filter((t) => !needle || t.name.includes(needle));
  }, [idx, q]);

  return (
    <div className="evm-mask" onClick={onClose}>
      <div className="logx card" role="dialog" aria-modal="true"
        aria-label="紀錄索引" onClick={(e) => e.stopPropagation()}>
        {/* ✕ 固定右上，label 在它左邊（與事件 modal 同一形狀） */}
        <div className="evm-head">
          <span className="evm-title">{title}</span>
          <span className="spacer" />
          {idx && (
            <span className="logx-headmeta">
              {mb(idx.bytes)} · {idx.frames.toLocaleString()} frames · {dur(idx.span)}
            </span>
          )}
          <InfoTip tip="欄位值與單位都是線上原樣，沒有換算（cdegC、degE7 那些就是機上送出來的形式）。曲線是原始樣本的抽樣，不做平滑也不插值。只有振動畫參考線——30／60 是 PX4 與 ArduPilot 共用的判讀門檻，其餘幾條沒有權威門檻就不畫。" />
          <button className="modal-close" aria-label="關閉（Esc）" title="關閉（Esc）" onClick={onClose}>✕</button>
        </div>

        {err && <div className="form-err">{err}</div>}

        {!idx && !err && (
          <div className="logx-wait">
            <div>解析中… {pct != null ? `${pct.toFixed(0)}%` : ""}</div>
            <div className="logx-bar"><i style={{ width: `${pct ?? 0}%` }} /></div>
            <div className="hint-line">
              大檔要一點時間（116 MB 約 45 秒）。解析完會存起來，下次立刻打開。
            </div>
          </div>
        )}

        {idx && (
          <>
            <div className="logx-meta">
              {idx.types.length} 種訊息 · {clock(idx.t_start)}–{clock(idx.t_end)}
              {/* **索引落後要說**：地面站那份 tlog 整天都在寫 */}
              {idx.indexed_bytes < idx.bytes && (
                <span className="logx-stale">
                  　索引只到 {mb(idx.indexed_bytes)}，之後還有{" "}
                  {mb(idx.bytes - idx.indexed_bytes)} 沒進索引
                  <button className="btn-plain btn-sm"
                    onClick={() => { setIdx(null); load(true); }}>重建</button>
                </span>
              )}
            </div>
            {idx.sysids.length > 1 && (
              <div className="logx-sys">
                來源
                {idx.sysids.slice(0, 6).map((s) => (
                  <button key={s.id} className={s.id === mainSys ? "on" : ""}
                    title={`${s.n.toLocaleString()} 則訊息`}
                    onClick={() => setSys(s.id)}>sysid {s.id}</button>
                ))}
                {/* 一份地面站 tlog 裡不只一台機的訊息——**混在一起看會把兩台機
                    的模式讀成同一台在亂換** */}
              </div>
            )}

            <div className="logx-tabs" role="tablist">
              {([["types", `訊息型別 ${idx.types.length}`],
                 ["text", `機上訊息 ${idx.statustext.length}`],
                 ["curve", "模式與曲線"]] as const).map(([k, lab]) => (
                <button key={k} role="tab" aria-selected={tab === k}
                  onClick={() => setTab(k)}>{lab}</button>
              ))}
            </div>

            <div className="logx-body">
              {tab === "types" && (
                <>
                  <input className="insp-search" placeholder="搜尋訊息型別…"
                    value={q} onChange={(e) => setQ(e.target.value)} />
                  <table className="table logx-table">
                    <thead><tr>
                      <th>訊息型別</th><th className="num">筆數</th>
                      <th className="num">頻率</th><th>在紀錄中的分布</th>
                      <th>最後一筆</th><th />
                    </tr></thead>
                    <tbody>
                      {types.map((t) => {
                        const on = open.has(t.name);
                        const max = Math.max(...t.buckets, 1);
                        const entries = Object.entries(t.fields);
                        return (
                          <Fragment key={t.name}>
                            <tr className="logx-tap" onClick={() => setOpen((s) => {
                              const n = new Set(s);
                              if (n.has(t.name)) n.delete(t.name); else n.add(t.name);
                              return n;
                            })}>
                              <td><span className="insp-name">{t.name}</span></td>
                              <td className="num">{t.n.toLocaleString()}</td>
                              {/* 一次性訊息沒有頻率——**留空，不寫 0.0** */}
                              <td className="num">{t.hz != null ? `${t.hz.toFixed(1)} Hz` : ""}</td>
                              <td>
                                <svg className="logx-dens" viewBox="0 0 192 15"
                                  preserveAspectRatio="none" aria-hidden="true">
                                  {t.buckets.map((v, i) => v > 0 && (
                                    <rect key={i} x={i * 3.2} width="2.4"
                                      y={15 - Math.max(1.5, (v / max) * 13)}
                                      height={Math.max(1.5, (v / max) * 13)}
                                      fill="var(--series-1)" fillOpacity="0.62" />
                                  ))}
                                </svg>
                              </td>
                              <td className="logx-first">
                                {entries.slice(0, 2)
                                  .map(([k, v]) => `${k} ${fmtVal(v)}`).join("　")}
                              </td>
                              <td className="num insp-arrow">{on ? "▾" : "▸"}</td>
                            </tr>
                            {on && (
                              <tr>
                                <td colSpan={6}>
                                  <div className="logx-fields">
                                    {entries.map(([k, v]) => (
                                      <div className="evm-kv-row" key={k}>
                                        <span className="evm-k">{k}</span>
                                        <span className="evm-v">
                                          {fmtVal(v)}
                                          {t.units?.[k] && (
                                            <span className="imu-unit"> {t.units[k]}</span>
                                          )}
                                        </span>
                                      </div>
                                    ))}
                                    <div className="hint-line">
                                      首見 +{t.first}s · 最後 +{t.last}s（相對紀錄開頭）
                                      　欄位值與單位都是線上原樣，沒有換算
                                    </div>
                                  </div>
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                  {!types.length && <div className="empty">沒有符合的訊息型別</div>}
                </>
              )}

              {tab === "text" && (
                <>
                  <div className="logx-note">
                    {idx.statustext_total.toLocaleString()} 則折成 {idx.statustext.length} 句
                    {idx.statustext_dropped > 0
                      && `，另有 ${idx.statustext_dropped} 句沒進索引（超過上限）`}
                    　原文不翻譯，嚴重度是 MAVLink 的 severity
                  </div>
                  <div className="logx-texts">
                    {idx.statustext.map((s, i) => (
                      <div className="logx-trow" key={`${s.sev}:${s.text}:${i}`}>
                        <span className="dot" style={{ background: sevColor(s.sev) }} />
                        <time title={`+${s.t}s${s.n > 1 ? ` – +${s.last}s` : ""}`}>
                          {clock(idx.t_start + s.t)}
                        </time>
                        <span className="logx-text">{s.text}</span>
                        <span className="logx-sev">{SEV_NAME[s.sev] ?? s.sev}</span>
                        {s.n > 1 && <span className="ev-count">×{s.n}</span>}
                        {s.n > 1 && (
                          <EvDensity times={s.times} color={sevColor(s.sev)} />
                        )}
                      </div>
                    ))}
                    {!idx.statustext.length && (
                      <div className="empty">這份紀錄裡沒有 STATUSTEXT。</div>
                    )}
                  </div>
                </>
              )}

              {tab === "curve" && (
                <>
                  <ModeBand idx={idx} sys={mainSys} />
                  {idx.series.map((s) => <Chart key={s.key} s={s} span={idx.span} />)}
                </>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** 模式帶：由 HEARTBEAT 的 custom_mode 還原，**切換時才畫一段**。 */
function ModeBand({ idx, sys }: { idx: LogIndex; sys: string | null }) {
  const segs = useMemo(() => {
    const ms = idx.modes.filter((m) => !sys || m.sys === sys);
    return ms.map((m, i) => ({
      ...m, end: i + 1 < ms.length ? ms[i + 1].t : idx.span,
    }));
  }, [idx, sys]);
  if (!segs.length) {
    return <div className="hint-line">
      這份紀錄裡沒有可解讀的模式心跳{sys ? `（sysid ${sys}）` : ""}。
    </div>;
  }
  const COL: Record<string, string> = {
    AUTO: "var(--series-3)", GUIDED: "var(--series-1)",
    LAND: "var(--series-2)", RTL: "var(--status-warn)",
  };
  return (
    <div className="logx-chart">
      <h4>模式<span>由 HEARTBEAT 還原{sys ? ` · sysid ${sys}` : ""} · {segs.length} 段</span></h4>
      <svg viewBox="0 0 1000 38" preserveAspectRatio="none" className="logx-band">
        {segs.map((g, i) => {
          const x = (g.t / idx.span) * 1000;
          const w = Math.max(1, ((g.end - g.t) / idx.span) * 1000);
          return (
            <g key={i}>
              <rect x={x} y="2" width={w} height="20"
                fill={COL[g.mode] ?? "var(--surface-2)"} fillOpacity="0.55" />
              {w > 52 && (
                <text x={x + 5} y="16" fill="var(--ink-2)" fontSize="10">{g.mode}</text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function Chart({ s, span }: { s: LogIndex["series"][number]; span: number }) {
  const pts = s.points;
  if (pts.length < 2) return null;
  const W = 1000, H = 104, L = 46, R = 10, T = 10, B = 18;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs) || span;
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (s.refs?.length) y1 = Math.max(y1, s.refs[s.refs.length - 1] * 1.05);
  const pad = (y1 - y0) * 0.12 || 1;
  y0 -= pad; y1 += pad;
  const X = (v: number) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R);
  const Y = (v: number) => T + (1 - (v - y0) / (y1 - y0 || 1)) * (H - T - B);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join(" ");
  const area = `${d} L${X(x1).toFixed(1)} ${Y(y0).toFixed(1)} L${X(x0).toFixed(1)} ${Y(y0).toFixed(1)} Z`;
  const ticks = [y0 + (y1 - y0) * 0.1, (y0 + y1) / 2, y1 - (y1 - y0) * 0.1];
  return (
    <div className="logx-chart">
      <h4>{s.label}<span>{s.unit ? `單位 ${s.unit} · ` : ""}{pts.length} 點</span></h4>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
        aria-label={s.label}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={L} x2={W - R} y1={Y(v)} y2={Y(v)} stroke="var(--hairline)" strokeWidth="1" />
            <text x={L - 6} y={Y(v) + 3.5} fill="var(--muted)" fontSize="10"
              textAnchor="end">{v.toFixed(1)}</text>
          </g>
        ))}
        {s.refs?.map((v) => (v >= y0 && v <= y1 ? (
          <g key={`r${v}`}>
            <line x1={L} x2={W - R} y1={Y(v)} y2={Y(v)} stroke="var(--status-warn)"
              strokeWidth="1" strokeDasharray="4 4" />
            <text x={W - R - 2} y={Y(v) - 4} fill="var(--status-warn)" fontSize="10"
              textAnchor="end">{v}</text>
          </g>
        ) : null))}
        <path d={area} fill="var(--series-1)" fillOpacity="0.13" />
        <path d={d} fill="none" stroke="var(--series-1)" strokeWidth="1.6"
          vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}
