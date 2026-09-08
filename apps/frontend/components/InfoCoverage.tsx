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

import InfoTip from "@/components/InfoTip";

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
      {/* 「上排是什麼、下排是什麼、斜線是什麼」是解釋，住 ⓘ（ui-spec §6c.7）。
          畫面上只留這一趟的時間範圍與底下那一句事實 */}
      <h3>
        {title ?? "錄製涵蓋"}
        <span className="cap-hint">
          {clock(cov.from)} – {cov.ended ? hhmm(cov.to) : "進行中"} · {dur(span)}
          <InfoTip tip={"上排＝機上錄的（飛控直接送出的那一份），"
            + "下排＝地面站錄的（送到地面站的那一份）。斜線＝那一段沒有錄到。"
            + "兩層都缺的那幾段，這個系統沒有任何備份——機上那份是唯一能補"
            + "斷線缺口的東西。"} />
        </span>
      </h3>

      <div className="cap-track">
        <div className="cap-tname">機上錄製</div>
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
        <div className="cap-tname">地面站錄製</div>
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

      {/* **一句話，不是三段**：每一段都是事實，但它們的「為什麼要在意」是
          同一個理由，那個理由住上面的 ⓘ（ui-spec §6c.7） */}
      {cov.blackouts.length === 0
        ? <p className="cap-note cap-note-ok">地面站全程都有收到。</p>
        : <p className={uncovered.length ? "cap-note cap-note-bad" : "cap-note cap-note-warn"}>
          {[
            filled.length
              ? `機上補得到 ${filled.length} 段（${dur(filled.reduce((n, b) => n + b.seconds, 0))}）`
              : null,
            uncovered.length
              ? `兩層都沒有 ${uncovered.length} 段（${dur(uncovered.reduce((n, b) => n + b.seconds, 0))}）`
              : null,
            cov.blackouts.some((b) => b.covered_onboard === null)
              ? "有幾段不知道機上補到了沒有（那些錄製檔切不動）"
              : null,
          ].filter(Boolean).join("・")}
        </p>}
    </div>
  );
}
