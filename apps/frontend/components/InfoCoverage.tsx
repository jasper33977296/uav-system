"use client";
/** 兩層錄製的覆蓋帶：**這是兩層並存的全部理由，所以它要能被檢驗**。
 *
 * （原本住在 app/captures/page.tsx；2026-09-07 資訊頁把「架次」與「錄製」
 * 分成兩個分頁，兩邊都要畫同一條帶子——同一件事只能有一份實作，
 * 兩份會在某一次修改後開始各說各話。）
 */

export interface Coverage {
  session_id: string; drone_name: string;
  from: number; to: number; ended: boolean;
  blackouts: {
    from: number; to: number; seconds: number; reason: string;
    recovered_by: string | null; covered_onboard: boolean | null;
  }[];
  onboard: {
    name: string; bytes: number;
    covers: { from: number; to: number } | null; url: string | null;
  }[];
  onboard_known: boolean;
}

const hhmm = (t: number) =>
  new Date(t * 1000).toLocaleTimeString("zh-TW",
    { hour: "2-digit", minute: "2-digit", hour12: false });
const clock = (t: number) =>
  new Date(t * 1000).toLocaleString("zh-TW", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false });
const dur = (s: number) =>
  s >= 60 ? `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒` : `${Math.round(s)} 秒`;

export default function CoverageCard({ cov, title }: {
  cov: Coverage;
  /** 標題（錄製分頁用「最近一趟 · 機名」；架次分頁已經有機名，只寫「錄製涵蓋」）*/
  title?: string;
}) {
  const span = Math.max(1, cov.to - cov.from);
  const pct = (t: number) => ((t - cov.from) / span) * 100;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => cov.from + f * span);
  const uncovered = cov.blackouts.filter((b) => b.covered_onboard === false);
  const filled = cov.blackouts.filter((b) => b.covered_onboard === true);

  return (
    <div className="card cap-cov">
      <h3>
        {title ?? "錄製涵蓋"}
        <span className="cap-hint">
          {clock(cov.from)} – {cov.ended ? hhmm(cov.to) : "進行中"} · {dur(span)}
        </span>
      </h3>

      <div className="cap-track">
        <div className="cap-tname">機上錄製<small>飛控送出的</small></div>
        <div className="cap-bar">
          {cov.onboard_known
            ? cov.onboard.map((o, i) => o.covers && (
              <span key={i} className="cap-seg cap-have"
                style={{ left: `${pct(o.covers.from)}%`,
                  width: `${Math.max(0.5, pct(o.covers.to) - pct(o.covers.from))}%` }} />
            ))
            /* **沒有機上錄製時整條留白並明說**，不要畫成一條灰的
               ——灰的會被讀成「錄了但品質不好」 */
            : <span className="cap-none">沒有機上錄製涵蓋這一趟</span>}
        </div>
      </div>

      <div className="cap-track">
        <div className="cap-tname">地面站錄製<small>送到地面站的</small></div>
        <div className="cap-bar">
          <span className="cap-seg cap-have" style={{ left: 0, right: 0 }} />
          {cov.blackouts.map((b, i) => (
            <span key={i} className="cap-seg cap-gap"
              style={{ left: `${pct(b.from)}%`,
                width: `${Math.max(0.5, pct(b.to) - pct(b.from))}%` }}
              title={`${hhmm(b.from)} – ${hhmm(b.to)}（${dur(b.seconds)}）`} />
          ))}
        </div>
      </div>

      <div className="cap-axis">
        {ticks.map((t, i) => <span key={i}>{hhmm(t)}</span>)}
      </div>

      {cov.blackouts.length === 0
        ? <p className="cap-note cap-note-ok">這一趟地面站沒有失明——兩層錄到的是同一段。</p>
        : <>
          {filled.length > 0 && (
            <p className="cap-note cap-note-warn">
              地面站有 <b>{filled.length}</b> 段什麼都沒收到（共 <b>
                {dur(filled.reduce((n, b) => n + b.seconds, 0))}</b>）。
              <b>那幾段只存在於機上那一份裡。</b>
            </p>
          )}
          {uncovered.length > 0 && (
            <p className="cap-note cap-note-bad">
              另有 <b>{uncovered.length}</b> 段（共 <b>
                {dur(uncovered.reduce((n, b) => n + b.seconds, 0))}</b>）
              <b>兩層都沒有</b>——機上那份沒有涵蓋到它。
            </p>
          )}
          {cov.blackouts.some((b) => b.covered_onboard === null) && (
            <p className="cap-note">
              有幾段<b>不知道</b>機上補到了沒有：那些錄製檔切不動，算不出涵蓋範圍。
            </p>
          )}
        </>}
    </div>
  );
}
