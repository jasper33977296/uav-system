"use client";
/** 規劃子頁（issues/048、doc/route-planning-first-principles.md）。
 *
 * **這一頁存在的理由是那條綠線。** 使用者的原話：「我一開始使用 QGC 就是
 * 不知道不能低於它下方海拔折線圖綠線的標示，一直以為我設高於地板 1 m 應該
 * 綽綽有餘，導致我的無人機執行時一直墜地但我找不出原因。」
 *
 * 所以主角是**剖面圖**，不是一份報告：門檻要求人先知道那個數字的意思，
 * 圖不用——線穿到地下、或兩條線貼在一起，看一眼就知道。
 */
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { emph } from "@/lib/emph";
import { errText, getJson } from "@/lib/fetchJson";
import { API, COMMAND_API } from "@/lib/signal";

interface Pt { d: number; ground: number | null; plan: number | null; agl: number | null; seq: number | null }
interface Profile { points: Pt[]; home_amsl_m: number | null; frames: number[] }
interface Leg {
  from: number; to: number; length_m: number; agl_m: number | null;
  speed_ms: number | null; speed_src: string; turn_deg?: number | null;
}
interface Check {
  ok: boolean; problems: string[]; warnings: string[]; legs?: Leg[];
  terrain?: { home_amsl_m?: number };
}

/** 高度基準是這一頁最該講清楚的一件事——同一個「4.6 m」在兩種 frame 下
 * 是不同的地方，而那正是使用者踩到的坑。 */
const FRAME_TEXT: Record<number, string> = {
  0: "離海平面", 3: "離起飛點", 6: "離起飛點", 10: "離地面（飛控跟隨）",
};

function frameLabel(frames: number[]): string {
  if (!frames.length) return "高度語意未知";
  if (frames.length > 1) return `高度混用 ${frames.join("/")}`;
  return `高度＝${FRAME_TEXT[frames[0]] ?? `frame ${frames[0]}`}`;
}

/** 剖面圖：地面一條、規劃一條，中間就是離地空間。 */
function Profile({ p }: { p: Profile }) {
  const pts = p.points.filter((x) => x.ground != null);
  if (pts.length < 2) {
    return <div className="empty">{emph(
      "沒有地形資料，畫不出剖面——**這不代表航線沒問題**，是這一段沒被檢查過")}</div>;
  }
  const W = 900, H = 260, PAD_L = 46, PAD_R = 12, PAD_T = 14, PAD_B = 26;
  const dMax = Math.max(...pts.map((x) => x.d), 1);
  const vals = pts.flatMap((x) => [x.ground!, x.plan ?? x.ground!]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  // **y 軸至少 6 公尺**：一條平坦航線若照資料自動縮放，2 m 的起伏會被拉滿
  // 整個圖高，看起來像懸崖——那是用版面製造出來的恐慌
  if (hi - lo < 6) { const c = (hi + lo) / 2; lo = c - 3; hi = c + 3; }
  const pad = (hi - lo) * 0.12;
  lo -= pad; hi += pad;
  const X = (d: number) => PAD_L + (d / dMax) * (W - PAD_L - PAD_R);
  const Y = (v: number) => PAD_T + (1 - (v - lo) / (hi - lo)) * (H - PAD_T - PAD_B);

  const gLine = pts.map((x, i) => `${i ? "L" : "M"}${X(x.d).toFixed(1)},${Y(x.ground!).toFixed(1)}`).join("");
  const gFill = `${gLine}L${X(pts[pts.length - 1].d).toFixed(1)},${H - PAD_B}L${X(pts[0].d).toFixed(1)},${H - PAD_B}Z`;
  const planPts = pts.filter((x) => x.plan != null);
  const pLine = planPts.map((x, i) => `${i ? "L" : "M"}${X(x.d).toFixed(1)},${Y(x.plan!).toFixed(1)}`).join("");
  // **兩條線中間那一塊才是主角。** 只畫兩條線，看到的是「兩條線」；
  // 把中間填起來，看到的才是「離地空間」——那正是這一頁存在的理由。
  const band = planPts.length
    ? pLine + planPts.slice().reverse().map(
      (x) => `L${X(x.d).toFixed(1)},${Y(x.ground!).toFixed(1)}`).join("") + "Z"
    : "";
  const under = planPts.filter((x) => (x.agl ?? 1) < 0);
  // 最窄的地方直接標在圖上：使用者要的是「哪裡最危險」，不是自己去比對兩條線
  const tight = planPts.reduce<Pt | null>(
    (m, x) => (x.agl == null ? m : m == null || x.agl < (m.agl ?? 9e9) ? x : m), null);
  const ticks = [lo, (lo + hi) / 2, hi];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="profile" role="img"
      aria-label="航線剖面：地面高程與規劃高度">
      {ticks.map((v, i) => (
        <g key={i}>
          <line x1={PAD_L} x2={W - PAD_R} y1={Y(v)} y2={Y(v)} stroke="var(--hairline)" strokeWidth="1" />
          <text x={4} y={Y(v) + 4} fill="var(--muted)" fontSize="11">{v.toFixed(0)} m</text>
        </g>
      ))}
      {/* 離地帶用**規劃線自己的顏色**（低透明度），不用 accent——
          `accent` 只准互動 chrome（ui-spec §4.6 的 design-tokens 鐵則），
          而這一塊是資料 */}
      {band && <path d={band} fill="var(--series-1)" opacity="0.14" />}
      <path d={gFill} fill="var(--hairline)" />
      <path d={gLine} stroke="var(--ink-2)" strokeWidth="1.5" fill="none" />
      <path d={pLine} stroke="var(--series-1)" strokeWidth="2" fill="none"
        strokeLinejoin="round" />
      {tight?.agl != null && (
        <g>
          <line x1={X(tight.d)} x2={X(tight.d)} y1={Y(tight.plan!)} y2={Y(tight.ground!)}
            stroke={tight.agl < 0 ? "var(--status-danger)" : "var(--status-warn)"}
            strokeWidth="1.5" strokeDasharray="3 2" />
          <text x={X(tight.d) + 5} y={(Y(tight.plan!) + Y(tight.ground!)) / 2 + 4}
            fill={tight.agl < 0 ? "var(--status-danger)" : "var(--status-warn)"} fontSize="12">
            最窄 {tight.agl} m
          </text>
        </g>
      )}
      {under.map((x, i) => (
        <circle key={i} cx={X(x.d)} cy={Y(x.plan!)} r="3" fill="var(--status-danger)" />
      ))}
      {(() => {
        // 航點編號**擠在一起就不標**：重疊的數字讀不出來，還會讓人以為
        // 那裡有什麼特別的東西。點照畫，只是不標號
        let lastX = -99;
        return pts.filter((x) => x.seq != null && x.plan != null).map((x) => {
          const px = X(x.d), room = px - lastX > 22;
          if (room) lastX = px;
          return (
            <g key={x.seq}>
              <circle cx={px} cy={Y(x.plan!)} r="3" fill="var(--series-1)" />
              {room && (
                <text x={px} y={H - 8} fill="var(--muted)" fontSize="10" textAnchor="middle">
                  {x.seq}
                </text>
              )}
            </g>
          );
        });
      })()}
    </svg>
  );
}

export default function PlanPage() {
  // Next 15 的 page props `params` 是 Promise；client component 用 useParams 取
  const id = String(useParams()?.id ?? "");
  const [name, setName] = useState("");
  const [prof, setProf] = useState<Profile | null>(null);
  const [chk, setChk] = useState<Check | null>(null);
  const [spd, setSpd] = useState<{ wp: number | null; rad: number | null; src: string }>(
    { wp: null, rad: null, src: "還沒讀過這台機" });
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    (async () => {
      try {
        // **速度只有飛機說得準。** 連得到就讀，讀不到就讓後端把那幾段標成
        // 「沒有檢查」——不是當成通過（issues/048 C5）
        let wp: number | null = null, rad: number | null = null, src = "還沒讀過這台機";
        // **sysid 直接問指令服務**，不靠全域 store：那條 WS 在這一頁不一定
        // 已經連上，而「讀不到」與「還沒連上」在畫面上會長得一樣
        let sid: string | null = null;
        try {
          const h = await getJson<{ drones: Record<string, unknown> }>(
            `${COMMAND_API}/healthz`);
          sid = Object.keys(h.drones ?? {})[0] ?? null;
        } catch { /* 指令服務沒開就是讀不到，下面照樣顯示「沒檢查」 */ }
        if (sid) {
          try {
            const p = await getJson<{ values: Record<string, number> }>(
              `${COMMAND_API}/api/command/${sid}/params?names=WP_SPD,WP_RADIUS_M`);
            wp = p.values.WP_SPD ?? null;
            rad = p.values.WP_RADIUS_M ?? null;
            if (wp != null) src = "取自機上（現在讀的）";
          } catch { /* 讀不到就維持 null，下面會顯示「沒有檢查」 */ }
        }
        const q = wp != null ? `?wp_spd=${wp}${rad != null ? `&wp_radius=${rad}` : ""}` : "";
        const [ms, pr, ck] = await Promise.all([
          getJson<{ name: string }[]>(`${API}/api/missions`),
          getJson<Profile>(`${API}/api/missions/${id}/profile`),
          getJson<Check>(`${API}/api/missions/${id}/check${q}`),
        ]);
        if (stop) return;
        setName((ms as any).find?.((m: any) => m.id === id)?.name ?? id);
        setProf(pr); setChk(ck); setSpd({ wp, rad, src });
      } catch (e) {
        if (!stop) setErr(errText((e as Error).message, "讀不到這份航線"));
      }
    })();
    return () => { stop = true; };
  }, [id]);

  const legs = chk?.legs ?? [];
  const worst = legs.reduce<number | null>(
    (m, l) => (l.agl_m == null ? m : m == null || l.agl_m < m ? l.agl_m : m), null);

  return (
    <div className="page">
      <div className="plan-head">
        <Link href="/missions" className="btn-plain btn-sm">← 路徑管理</Link>
        <h1 className="mtitle">{name || "…"}</h1>
      </div>

      {err && <div className="form-err">{err}</div>}

      <div className="plan-facts">
        <span className="chip">{prof ? frameLabel(prof.frames) : "…"}</span>
        {prof?.home_amsl_m != null && (
          <span className="chip">起飛點 {prof.home_amsl_m} m（海拔）</span>
        )}
        {worst != null && <span className="chip">最低離地 {worst} m</span>}
        <span className="chip" style={spd.wp == null ? { opacity: 0.6 } : undefined}
          title={spd.wp == null
            ? "航線裡的 DO_CHANGE_SPEED 只從它被執行到的那一項之後才生效；在那之前用的是機上的 WP_SPD。讀不到它，速度相關的判定就不做——讀不到不等於沒問題"
            : "第一段永遠用這個值：航線裡的 DO_CHANGE_SPEED 管不到起飛之後那一段"}>
          {spd.wp == null ? "機上速度未讀到" : `機上 WP_SPD ${spd.wp} m/s`} · {spd.src}
        </span>
      </div>

      {prof && <Profile p={prof} />}
      <div className="hint-line">
        {emph("地面線來自 SRTM（水平約 30 m），**只有地形，不含樹木、電線、建物**。")}
      </div>

      {(chk?.problems?.length || chk?.warnings?.length) ? (
        <div className="plan-findings">
          {/* 後端文案用 `**` 當強調記號，而畫面不解析 Markdown（ui-spec §0.3c）*/}
          {chk.problems.map((p, i) => <div key={i} className="form-err">✕ {emph(p)}</div>)}
          {chk.warnings.map((w, i) => <div key={i} className="hint-line">⚠ {emph(w)}</div>)}
        </div>
      ) : chk ? <div className="hint-line">這份航線沒有發現。</div> : null}

      {legs.length > 0 && (
        <table className="plan-legs">
          <thead>
            <tr><th>段</th><th>長度</th><th>離地</th><th>有效速度</th>
              <th>速度來源</th><th>轉角</th></tr>
          </thead>
          <tbody>
            {legs.map((l) => (
              <tr key={`${l.from}-${l.to}`}>
                <td>seq {l.from}→{l.to}</td>
                <td>{l.length_m} m</td>
                <td className={l.agl_m != null && l.agl_m < 3 ? "bad" : undefined}>
                  {l.agl_m == null ? "—" : `${l.agl_m} m`}
                </td>
                <td>{l.speed_ms == null ? "—" : `${l.speed_ms} m/s`}</td>
                {/* **「來源」這一欄是這張表存在的理由**：一眼看出這一段用的是
                    機上的預設值，不是航線裡寫的那個（2026-09-07 的誤會） */}
                <td className={l.speed_src === "unknown" ? "muted" : undefined}>
                  {l.speed_src === "unknown" ? "未讀到（沒檢查）" : l.speed_src}
                </td>
                <td>{l.turn_deg == null ? "—" : `${l.turn_deg}°`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
