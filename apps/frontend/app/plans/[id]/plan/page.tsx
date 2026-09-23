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
import { useCallback, useEffect, useRef, useState } from "react";

import TerrainStage, { type BuildingFeat, type FenceShape, type StageHit,
  type StageTip, type StageWp } from "@/components/TerrainStage";
import InfoTip from "@/components/InfoTip";
import { emph } from "@/lib/emph";
import { errText, getJson } from "@/lib/fetchJson";
import { API, COMMAND_API } from "@/lib/signal";

interface Pt { d: number; lat?: number; lon?: number; ground: number | null;
  /** 這一點上方最高的東西（含建物）。**建物高度未知時是 null**——
   *  那是「有東西、不知道多高」，不是「什麼都沒有」 */
  top: number | null; obst?: "building" | "assumed" | "unknown";
  obst_name?: string | null;
  /** 這個高度的出處（`srtm`／`osm:height`／`osm:levels`／`unknown`）。
   *  **樓層數推算的與量到的不是同一件事**，畫面要說得出來 */
  src?: string;
  plan: number | null; agl: number | null; seq: number | null;
  /** 這一點是什麼（後端給）；`auto`＝系統補的中繼／進場點 */
  kind?: "takeoff" | "wp" | "land"; auto?: boolean;
  /** 操作員的第幾個點（後端 `src_i`）。系統補的沒有 */
  src_i?: number | null;
  /** 到點停留秒數（062，`NAV_WAYPOINT` 的 param1）。後端只在大於 0 時給 */
  hold_s?: number;
  /** **從這一點失聯返航會怎樣。** RTL 爬到 `RTL_ALT_M`（離起飛點）之後
   *  直線飛回起飛點——那條線在同一片地形上，起伏會撞。`rtl_agl` 是那條
   *  線上最低的離地；null ＝沒讀到 `RTL_ALT_M`，**沒判不是安全** */
  rtl_amsl?: number | null; rtl_agl?: number | null; rtl_blind?: boolean }
interface Profile { points: Pt[]; home_amsl_m: number | null; frames: number[];
  /** 這份航線是用哪個政策產生的。**有政策就以它為準**——離地面的
   *  航線寫進去也是 frame 3，只看 frame 會顯示「離起飛點」 */
  policy?: Policy | null;
  /** 這份剖面是用哪個返航高度算的。null ＝沒讀到，返航那一層不畫 */
  rtl_alt_m?: number | null;
  /** 這份航線宣告的圍欄（`plans.fence`）。null ＝沒宣告 */
  fence?: StoredFence | null }
/** 存進 `plans.fence` 的形狀（QGC `.plan` 的 geoFence 也是這個形狀）。 */
interface StoredFence {
  inclusion_circles?: { lat: number; lon: number; radius: number }[];
  inclusion_polygons?: [number, number][][];
  exclusion_circles?: unknown[];
  exclusion_polygons?: unknown[];
  alt_max?: number | null;
}
interface Leg {
  from: number; to: number; length_m: number; agl_m: number | null;
  speed_ms: number | null; speed_src: string; turn_deg?: number | null;
  /** **判定由後端給**（`plan_check.leg_profile`）。前端不要照門檻自己再判
   *  一次——改了後端的常數，畫面不會跟著變，而且看起來完全正常 */
  low_fast?: boolean;
}
/** 高度與速度的**政策**：操作員的意圖。逐點的 alt 是它解出來的結果。 */
interface Policy {
  mode: "agl" | "home" | "amsl"; height_m: number; speed_ms: number;
  /** null ＝跟著政策算。**離地 3 m 的航線就從 3 m 起飛**——用 1.5 m 起飛
   *  再飛向 3 m 離地的航點，中間那一段會在地面爬升處貼地 */
  takeoff_alt_m: number | null; land_at_home: boolean; land_mode: "vert" | "glide";
}
/** 系統替你決定了什麼（redesign §3 動作 3）。 */
interface Decision { what: string; value: string; why: string; seq: number | null }
/** 簽核：**這一份在什麼假設下被誰看過**。綁在 waypoints_hash 上——
 *  航點一改就失效，不然它只是「曾經有人在某個版本上按過 OK」。 */
interface Sign {
  signed: boolean; stale: boolean; hash: string; ok?: boolean;
  checked_at?: string; signed_by?: string | null; assumed_m?: number | null;
  acknowledged?: string[]; problems?: string[]; why?: string | null;
}

const MODE_TEXT: Record<Policy["mode"], string> = {
  agl: "離地面", home: "離起飛點", amsl: "固定海拔",
};

interface Limits { low_alt_m: number; low_speed_ms: number; min_takeoff_alt_m: number;
  /** 未量測建物假設高度的**預設值**（後端給，前端不要自己抄一份） */
  assumed_default_m?: number }
interface Check {
  ok: boolean; problems: string[]; warnings: string[]; legs?: Leg[];
  limits?: Limits; terrain?: { home_amsl_m?: number };
  /** 這份判定是用哪個假設高度算的；null ＝沒有假設，未量測的樓直接擋下 */
  assumed_m?: number | null;
  /** 失效處置（C7）：從航線上任何一點返航，那條線最低離地多少 */
  terrain_rtl?: { rtl_alt_m: number; min_agl_m: number | null; at_seq: number | null } | null;
  terrain_blind?: { id: string; name: string }[];
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

/** 剖面圖：地面一條、屋頂一條、規劃一條，中間就是離地空間。 */
function Profile({ p, ceilM }: { p: Profile;
  /** 圍欄的高度上限，**離起飛點**。畫成一條水平線——它與地面線之間的
   *  距離會沿路變，那正是「上限比看起來緊」的地方 */
  ceilM?: number | null }) {
  const pts = p.points.filter((x) => x.ground != null);
  if (pts.length < 2) {
    return <div className="empty">{emph(
      "沒有地形資料，畫不出剖面——**這不代表航線沒問題**，是這一段沒被檢查過")}</div>;
  }
  const W = 900, H = 260, PAD_L = 46, PAD_R = 12, PAD_T = 14, PAD_B = 26;
  const dMax = Math.max(...pts.map((x) => x.d), 1);
  const surf = (x: Pt) => x.top ?? x.ground!;
  const ceilAmsl = ceilM != null && p.home_amsl_m != null
    ? p.home_amsl_m + ceilM : null;
  const vals = pts.flatMap((x) => [x.ground!, surf(x), x.plan ?? x.ground!,
    ...(x.rtl_amsl != null ? [x.rtl_amsl] : [])]);
  if (ceilAmsl != null) vals.push(ceilAmsl);
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
  // 離地帶量到的是**屋頂**，不是地面——飛過一棟樓的時候，兩者差一整棟樓
  const band = planPts.length
    ? pLine + planPts.slice().reverse().map(
      (x) => `L${X(x.d).toFixed(1)},${Y(surf(x)).toFixed(1)}`).join("") + "Z"
    : "";
  // 有量到高度的建物：地面到屋頂之間填實。**連續的才算一棟**，
  // 中間斷掉就另起一段，否則兩棟樓之間的空地會被填成實心
  // **牆是垂直的。** 每 30 m 才取樣一次，照取樣點連線會把一棟樓畫成
  // 一根尖錐——那是取樣造成的形狀，不是那棟樓的形狀
  const half = dMax / Math.max(1, pts.length - 1) / 2;
  type Roof = { d0: number; d1: number; y: number; g: number;
                name?: string | null; src?: string; est?: boolean; h: number };
  const roofs: Roof[] = [];
  pts.forEach((x, i) => {
    if (x.top == null || x.top <= x.ground! + 0.05) return;
    const prev = pts[i - 1];
    const cont = roofs.length && prev && prev.top != null
      && prev.top > prev.ground! + 0.05 && prev.obst_name === x.obst_name;
    if (cont) {
      const c = roofs[roofs.length - 1];
      c.d1 = x.d + half;
      c.y = Math.min(c.y, Y(x.top));
      c.g = Math.max(c.g, Y(x.ground!));
      c.h = Math.max(c.h, x.top - x.ground!);
    } else {
      roofs.push({ d0: x.d - half, d1: x.d + half, y: Y(x.top), g: Y(x.ground!),
                   name: x.obst_name, src: x.src, est: x.obst === "assumed",
                   h: x.top - x.ground! });
    }
  });
  // 高度未知的建物：**開口向上的柱子**，不是一條線——`assumed-default`
  // 不得用來放行，畫面也不能把它畫成一個數字（§9-A）
  const blind: { d0: number; d1: number; g: number; name?: string | null }[] = [];
  pts.forEach((x) => {
    if (x.obst !== "unknown") return;
    const last = blind[blind.length - 1];
    if (last && x.d - last.d1 < 2 * (dMax / Math.max(1, pts.length))) {
      last.d1 = x.d; last.g = Math.max(last.g, Y(x.ground!));
    } else {
      blind.push({ d0: x.d, d1: x.d, g: Y(x.ground!), name: x.obst_name });
    }
  });
  const under = planPts.filter((x) => (x.agl ?? 1) < 0);
  // 最窄的地方直接標在圖上：使用者要的是「哪裡最危險」，不是自己去比對兩條線
  const tight = planPts.reduce<Pt | null>(
    (m, x) => (x.agl == null ? m : m == null || x.agl < (m.agl ?? 9e9) ? x : m), null);
  const ticks = [lo, (lo + hi) / 2, hi];
  // 返航：巡航高度一條線 ＋ X 軸下面一條「從這裡返航安不安全」的帶子。
  // **餘裕不是這張圖的 Y**（返航飛的是另一個方向的地形），所以它只能
  // 用顏色表示——硬畫成 Y 會讓人以為那是同一條剖面上的高度
  const rtlPts = pts.filter((x) => x.rtl_amsl != null);
  const rtlLine = rtlPts.map((x, i) =>
    `${i ? "L" : "M"}${X(x.d).toFixed(1)},${Y(x.rtl_amsl!).toFixed(1)}`).join("");
  const rtlBand = pts.filter((x) => x.rtl_agl != null);
  const BAND_Y = H - PAD_B + 6, BAND_H = 5;

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
      <defs>
        <pattern id="blindhatch" width="6" height="6" patternUnits="userSpaceOnUse"
          patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--status-warn)" strokeWidth="1.4" />
        </pattern>
      </defs>
      {band && <path d={band} fill="var(--series-1)" opacity="0.14" />}
      <path d={gFill} fill="var(--hairline)" />
      {/* 量到的是實心；**假設的是虛線外框**——同一個形狀但一眼分得出來，
          因為改一下旋鈕它就會變高變矮，而實心的那些不會 */}
      {roofs.map((r, i) => (
        <g key={`r${i}`}>
          <rect x={X(r.d0)} y={r.y} width={Math.max(3, X(r.d1) - X(r.d0))}
            height={Math.max(1, r.g - r.y)}
            fill={r.est ? "url(#blindhatch)" : "var(--ink-2)"}
            opacity={r.est ? 0.3 : 0.55}
            stroke={r.est ? "var(--status-warn)" : "var(--ink-2)"} strokeWidth="1.5"
            strokeDasharray={r.est ? "4 3" : undefined} />
          <text x={(X(r.d0) + X(r.d1)) / 2} y={r.y - 5}
            fill={r.est ? "var(--status-warn)" : "var(--ink-2)"}
            fontSize="10" textAnchor="middle">
            {r.name ?? "建物"}
            {r.est ? `・假設 ${Math.round(r.h)} m`
              : r.src === "osm:levels" ? "・樓層數推算" : ""}
          </text>
        </g>
      ))}
      {/* 開口向上：只畫左右與底，**沒有頂**——頂在哪裡就是不知道 */}
      {blind.map((b, i) => {
        const x0 = X(b.d0) - 3, x1 = X(b.d1) + 3;
        return (
          <g key={`b${i}`}>
            <rect x={x0} y={PAD_T} width={Math.max(4, x1 - x0)} height={b.g - PAD_T}
              fill="url(#blindhatch)" opacity="0.35" />
            <path d={`M${x0.toFixed(1)},${PAD_T}L${x0.toFixed(1)},${b.g.toFixed(1)}`
              + `L${x1.toFixed(1)},${b.g.toFixed(1)}L${x1.toFixed(1)},${PAD_T}`}
              fill="none" stroke="var(--status-warn)" strokeWidth="1.5" />
            <text x={(x0 + x1) / 2} y={PAD_T + 12} fill="var(--status-warn)"
              fontSize="10" textAnchor="middle">
              {b.name ?? "建物"}・高度未知
            </text>
          </g>
        );
      })}
      <path d={gLine} stroke="var(--ink-2)" strokeWidth="1.5" fill="none" />
      <path d={pLine} stroke="var(--series-1)" strokeWidth="2" fill="none"
        strokeLinejoin="round" />
      {tight?.agl != null && (
        <g>
          <line x1={X(tight.d)} x2={X(tight.d)} y1={Y(tight.plan!)} y2={Y(surf(tight))}
            stroke={tight.agl < 0 ? "var(--status-danger)" : "var(--status-warn)"}
            strokeWidth="1.5" strokeDasharray="3 2" />
          <text x={X(tight.d) + 5} y={(Y(tight.plan!) + Y(surf(tight))) / 2 + 4}
            fill={tight.agl < 0 ? "var(--status-danger)" : "var(--status-warn)"} fontSize="12">
            最窄 {tight.agl} m
          </text>
        </g>
      )}
      {/* 返航巡航高度。**`RTL_ALT_M` 是離起飛點的**，所以它在圖上是一條
          （幾乎）水平線，而地面不是——兩者交叉的地方就是返航會撞的地方 */}
      {rtlLine && (
        <>
          <path d={rtlLine} stroke="var(--status-warn)" strokeWidth="1.5"
            strokeDasharray="6 4" fill="none" opacity="0.9" />
          <text x={PAD_L + 4} y={Y(rtlPts[0].rtl_amsl!) - 5}
            fill="var(--status-warn)" fontSize="10">返航高度</text>
        </>
      )}
      {ceilAmsl != null && (
        <>
          <line x1={PAD_L} x2={W - PAD_R} y1={Y(ceilAmsl)} y2={Y(ceilAmsl)}
            stroke="var(--status-warn)" strokeWidth="1" strokeDasharray="2 3" />
          <text x={W - PAD_R - 4} y={Y(ceilAmsl) - 4} textAnchor="end"
            fill="var(--status-warn)" fontSize="10">圍欄上限</text>
        </>
      )}
      {rtlBand.length > 0 && (
        <>
          {rtlBand.map((x, i) => {
            const nx = rtlBand[i + 1];
            const w = Math.max(2, (nx ? X(nx.d) : X(x.d) + 4) - X(x.d));
            const c = x.rtl_agl! < 0 ? "var(--status-danger)"
              : x.rtl_agl! < 2 ? "var(--status-warn)" : "var(--hairline)";
            return <rect key={i} x={X(x.d)} y={BAND_Y} width={w} height={BAND_H}
              fill={c} />;
          })}
          <text x={4} y={BAND_Y + BAND_H} fill="var(--muted)" fontSize="9">返航</text>
        </>
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

interface Fence {
  shape: "none" | "circle" | "polygon";
  radius_m: number | null;
  points: [number, number][];
  alt_max_m: number | null;
}

/** 資料庫裡那份圍欄能不能給這一頁的編輯器編。**排除區與多個形狀不行**
 *  ——那是 QGC 畫的，這裡畫不出來，回 null 讓畫面別去碰它。 */
function editable(f: StoredFence | null | undefined): Fence | null {
  if (!f) return null;
  const ic = f.inclusion_circles ?? [], ip = f.inclusion_polygons ?? [];
  if ((f.exclusion_circles?.length ?? 0) || (f.exclusion_polygons?.length ?? 0)) return null;
  if (ic.length + ip.length !== 1) return null;
  const alt = f.alt_max ?? null;
  if (ic.length) return { shape: "circle", radius_m: ic[0].radius, points: [], alt_max_m: alt };
  return { shape: "polygon", radius_m: null,
           points: ip[0].map((p) => [p[0], p[1]] as [number, number]),
           alt_max_m: alt };
}

/** 新頂點插進**讓周長增加最少的那條邊**，不是一律接在最後——照點擊順序接，
 *  點的順序一亂圈就交叉（使用者 2026-09-11：每放一點就重算圍起來的範圍）。
 *  凹的形狀照樣留著：場地不是凸的，不用凸包。 */
function insertVertex(pts: [number, number][], p: [number, number]) {
  if (pts.length < 3) return { points: [...pts, p], at: pts.length };
  const k = Math.cos(p[0] * Math.PI / 180);
  const xy = (q: [number, number]) => [q[1] * 111320 * k, q[0] * 110574];
  const d = (a: number[], b: number[]) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const P = xy(p);
  let best = 0, cost = Infinity;
  pts.forEach((q, i) => {
    const a = xy(q), b = xy(pts[(i + 1) % pts.length]);
    const c = d(a, P) + d(P, b) - d(a, b);
    if (c < cost) { cost = c; best = i; }
  });
  return { points: [...pts.slice(0, best + 1), p, ...pts.slice(best + 1)], at: best + 1 };
}

/** 數字欄位（使用者 2026-09-23：拉條改成輸入）。
 *
 *  **打字期間不攔任何東西**：可以清空、可以只打一個 `0`、可以打到一半。
 *  受控元件直接把 `Number(e.target.value)` 寫回狀態時，「0」會被當成 0 而
 *  「0.」「-」「空白」會變成 NaN 或 0——使用者打不完一個數字。
 *
 *  **離開欄位（或按 Enter）才檢查**：超出範圍就夾住，並且**說出它被改成什麼**
 *  ——默默夾值等於畫面上的數字不是你填的那個。空白＝不改，退回原值。 */
function NumField({ label, tip, value, min, max, step = 0.1, unit, onCommit, hint }: {
  label: React.ReactNode; tip?: string; value: number; min: number; max: number;
  step?: number; unit?: string; onCommit: (v: number) => void; hint?: React.ReactNode;
}) {
  const [draft, setDraft] = useState(String(value));
  const [note, setNote] = useState<string | null>(null);
  const seen = useRef(value);
  // 外面改了值（選了別的點、政策重算）就跟上；**打字期間不要被蓋掉**
  useEffect(() => {
    if (seen.current !== value) { seen.current = value; setDraft(String(value)); setNote(null); }
  }, [value]);
  const commit = () => {
    const t = draft.trim();
    if (t === "") { setDraft(String(value)); setNote(null); return; }   // 空白＝不改
    const n = Number(t);
    if (!Number.isFinite(n)) {
      setDraft(String(value)); setNote("這不是一個數字，維持原值");
      return;
    }
    const c = Math.min(max, Math.max(min, n));
    setNote(c === n ? null : `${n} 超出 ${min}–${max}${unit ? " " + unit : ""}，已改成 ${c}`);
    setDraft(String(c));
    seen.current = c;
    onCommit(c);
  };
  return (
    <div className="rail-field">
      <div className="rail-row"><span>{label}{tip && <InfoTip tip={tip} />}</span>
        <input className="numin" type="number" step={step} value={draft}
          onChange={(e) => { setDraft(e.target.value); setNote(null); }}
          onBlur={commit}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }} />
      </div>
      {note && <div className="hint-line num-note">{note}</div>}
      {hint}
    </div>
  );
}

export default function PlanPage() {
  // Next 15 的 page props `params` 是 Promise；client component 用 useParams 取
  const id = String(useParams()?.id ?? "");
  /** **從零產生**（使用者 2026-09-08：兩個入口都要）。`/plans/new/plan`。 */
  const isNew = id === "new";
  /** 起飛點。**沒有預設**（使用者裁定 2026-09-09）——它是解鎖的地方，
   *  由操作員在地圖上放。空字串＝還沒放。 */
  const [home, setHome] = useState({ lat: "", lon: "" });
  //: 地圖一開始看哪裡。**這只是視角，不是起飛點**——場域中心，讓人看得到
  //: 地形才放得下第一個點
  const VIEW = { lat: 24.773449, lon: 121.045864 };
  // **先畫線，數字後到**（使用者裁定 2026-09-09）：政策有預設值，
  // 所以放完點就已經是一條合法航線，沒有一個欄位需要先填
  const [pol, setPol] = useState<Policy>({
    mode: "agl", height_m: 3, speed_ms: 1, takeoff_alt_m: null,
    land_at_home: false, land_mode: "vert",
  });
  const [pts, setPts] = useState<
    { lat: number; lon: number; h?: number; alt_source?: string; kind: string;
      /** 到點停留秒數（062）。沒有＝飛過去 */
      hold_s?: number }[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  /** 這條航線怎麼結束（後端 `build_plan` 給）：`home`／`marked`／**`none`＝沒有結尾**。
   *  沒有結尾就警告並擋下存檔（使用者 2026-09-23）——**不替操作員挑一個點來降落**：
   *  以前是「最後一個放的點自動變成降落點」，於是每放一個新點，前一個變回航點、
   *  新的那個變成降落，看起來就是「我放的航點被改掉了」，而且它真的被改寫了 */
  const [ending, setEnding] = useState<string | null>(null);
  const [applied, setApplied] = useState<{ note?: string } | null>(null);
  const [sign, setSign] = useState<Sign | null>(null);
  const [blds, setBlds] = useState<BuildingFeat[]>([]);
  const [railOpen, setRailOpen] = useState(true);
  /** 標題雙擊改名（使用者 2026-09-09）。null＝沒在改 */
  const [renaming, setRenaming] = useState<string | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const onBlds = useCallback((b: BuildingFeat[]) => setBlds(b), []);
  const [ack, setAck] = useState<Set<string>>(new Set());
  const [started, setStarted] = useState(false);
  const hasHome = !!(home.lat && home.lon);
  const [placeKind, setPlaceKind] = useState("home");
  const [naming, setNaming] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [prof, setProf] = useState<Profile | null>(null);
  // 未量測建物的假設高度。**它是規劃時的旋鈕，不是那些樓的高度**——
  // 真正的答案要等光達實測，所以每一份用到它的判定都帶著出處出去。
  // null ＝不假設：那時未量測的樓是擋下，不是通過
  const [assume, setAssume] = useState<number | null>(null);
  const assumeRef = useRef<number | null>(null);
  assumeRef.current = assume;
  // 圍欄。**畫在這裡的圍欄不會讓飛機停下來**——飛控照的是它自己的
  // `FENCE_*`（使用者裁定 2026-09-09 選 M：先只做規劃端）。所以畫面上
  // 永遠跟著一顆「飛控不擋」的晶片，不然畫了一個圈會被讀成飛不出去
  const [fence, setFence] = useState<Fence>(
    { shape: "none", radius_m: 120, points: [], alt_max_m: null });
  /** 多邊形的頂點靠點地圖加。開著時地圖的點擊給圍欄，不給航點 */
  const [fenceDraw, setFenceDraw] = useState(false);
  const [fenceSel, setFenceSel] = useState(-1);
  const fenceRef = useRef<Fence>(fence);
  fenceRef.current = fence;
  /** 這一頁的編輯器**能不能代表**這份航線的圍欄。QGC 匯進來的可以有排除區、
   *  多個形狀——那些畫面畫不出來，就不要送 `fence` 過去覆蓋掉它們 */
  const [fenceOwn, setFenceOwn] = useState(isNew);
  const fenceOwnRef = useRef(fenceOwn);
  fenceOwnRef.current = fenceOwn;
  const editFence = useCallback((f: Fence) => {
    setFence(f); setFenceOwn(true);
  }, []);
  const fenceBody = () => (fenceOwnRef.current ? fenceRef.current : undefined);
  const [geoQ, setGeoQ] = useState("");
  const [geo, setGeo] = useState<{ query: string; used: string | null;
    fallback: boolean; err?: string;
    results: { name: string; lat: number; lon: number; precision: string }[] } | null>(null);
  const [flyTo, setFlyTo] = useState<{ lat: number; lon: number; n: number } | null>(null);
  const geocode = async () => {
    const q = geoQ.trim();
    if (!q) return;
    try {
      const r = await fetch(`${API}/api/geocode?q=${encodeURIComponent(q)}`);
      const d = await r.json();
      setGeo(r.ok ? d : { query: q, used: null, fallback: false, results: [],
        err: errText(d.detail, "查不到") });
    } catch (e) {
      setGeo({ query: q, used: null, fallback: false, results: [],
               err: `查不到：${(e as Error).message}` });
    }
  };
  const [chk, setChk] = useState<Check | null>(null);
  const [spd, setSpd] = useState<{ wp: number | null; rad: number | null;
    rtl?: number | null; src: string }>(
    { wp: null, rad: null, src: "還沒讀過這台機" });
  const [err, setErr] = useState<string | null>(null);
  // **預設不選任何一個。** 原本預設選 seq 0（起飛點），於是它一直是選取色
  // ——那顆綠色的環從來沒出現過，使用者也就一直看不出哪個是起飛點
  const [selWp, setSelWp] = useState(-1);
  /** **改動只存在畫面上**（使用者裁定 2026-09-08：先只算不存）。
   *  每次變動送去後端試算——規則只有一份，前端不自己再算一次。 */
  const [ov, setOv] = useState<Record<number,
    { alt?: number; speed?: number; lat?: number; lon?: number; hold?: number }>>({});
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const spdRef = useRef<{ wp: number | null; rad: number | null;
    rtl?: number | null }>({ wp: null, rad: null });

  useEffect(() => {
    if (isNew) {
      setName("新航線");
      // 從零模式一樣要讀機上的 WP_SPD／WP_RADIUS_M——不然速度相關的判定
      // 一律是「沒有檢查」，而那不等於沒問題
      (async () => {
        try {
          const h = await getJson<{ drones: Record<string, unknown> }>(
            `${COMMAND_API}/healthz`);
          const sid = Object.keys(h.drones ?? {})[0];
          if (!sid) return;
          const p = await getJson<{ values: Record<string, number> }>(
            `${COMMAND_API}/api/command/${sid}/params?names=WP_SPD,WP_RADIUS_M,RTL_ALT_M`);
          spdRef.current = { wp: p.values.WP_SPD ?? null,
                             rad: p.values.WP_RADIUS_M ?? null };
          if (p.values.WP_SPD != null)
            setSpd({ wp: p.values.WP_SPD, rad: p.values.WP_RADIUS_M ?? null,
                     src: "取自機上（現在讀的）" });
        } catch { /* 讀不到就維持「沒有檢查」 */ }
      })();
      return;
    }
    let stop = false;
    (async () => {
      try {
        // **速度只有飛機說得準。** 連得到就讀，讀不到就讓後端把那幾段標成
        // 「沒有檢查」——不是當成通過（issues/048 C5）
        let wp: number | null = null, rad: number | null = null;
        let rtl: number | null = null, src = "還沒讀過這台機";
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
              `${COMMAND_API}/api/command/${sid}/params?names=WP_SPD,WP_RADIUS_M,RTL_ALT_M`);
            wp = p.values.WP_SPD ?? null;
            rad = p.values.WP_RADIUS_M ?? null;
            rtl = p.values.RTL_ALT_M ?? null;
            if (wp != null) src = "取自機上（現在讀的）";
          } catch { /* 讀不到就維持 null，下面會顯示「沒有檢查」 */ }
        }
        const q = `?${[wp != null ? `wp_spd=${wp}` : "",
                       rad != null ? `wp_radius=${rad}` : "",
                       rtl != null ? `rtl_alt_m=${rtl}` : ""]
                      .filter(Boolean).join("&")}`;
        // 開頁時**先不假設**：第一眼看到的是「這幾棟沒量過」，
        // 而不是一份用假設值算出來的「通過」
        const [ms, pr, ck] = await Promise.all([
          getJson<{ name: string }[]>(`${API}/api/plans`),
          // **剖面與檢查要用同一組參數**，不然圖上畫的返航跟報告說的
          // 不是同一件事——那種不一致比單一個算錯更難查
          getJson<Profile>(`${API}/api/plans/${id}/profile${q}`),
          getJson<Check>(`${API}/api/plans/${id}/check${q}`),
        ]);
        if (stop) return;
        setName((ms as any).find?.((m: any) => m.id === id)?.name ?? id);
        getJson<Sign>(`${API}/api/plans/${id}/sign`)
          .then((sg) => { if (!stop) { setSign(sg); setAck(new Set(sg.acknowledged ?? [])); } })
          .catch(() => { /* 讀不到就當作沒簽核——**不是當作簽過** */ });
        setProf(pr); setChk(ck); setSpd({ wp, rad, rtl, src });
        const ef = editable(pr.fence);
        if (ef) { setFence(ef); setFenceOwn(true); }
        spdRef.current = { wp, rad, rtl };
      } catch (e) {
        if (!stop) setErr(errText((e as Error).message, "讀不到這份航線"));
      }
    })();
    return () => { stop = true; };
  }, [id, isNew]);

  // 從零模式：點一變就重算（同樣去抖、同樣不寫資料庫）
  useEffect(() => {
    // **沒有起飛點就沒有航線可算**：起飛點是解鎖的地方，少了它後端只會
    // 拿到 (0, 0)。畫面在那之前說「先放起飛點」，不是回一份算好的東西
    if (!isNew || !started || !(home.lat && home.lon)) return;
    const t = setTimeout(async () => {
      setBusy(true);
      try {
        const r = await fetch(`${API}/api/plans/draft`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            home: [Number(home.lat), Number(home.lon)], points: pts,
            policy: pol,
            wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
            rtl_alt_m: spdRef.current.rtl,
            assume_m: assumeRef.current, fence: fenceBody() }),
        });
        const d = await r.json();
        if (r.ok) { setChk(d.check); setProf(d.profile); setDecisions(d.decisions ?? []);
                    setEnding(d.ending ?? null); }
      } finally { setBusy(false); }
    }, 220);
    return () => clearTimeout(t);
  }, [isNew, started, pts, pol, home.lat, home.lon, assume, fence]);

  /** 改假設高度 → 重算。**既有航線也要能改**，不然這個旋鈕只有從零模式
   *  用得到，而使用者最常做的事是拿既有航線來看。 */
  const firstAssume = useRef(true);
  useEffect(() => {
    if (isNew) return;
    if (firstAssume.current) { firstAssume.current = false; return; }
    const t = setTimeout(async () => {
      setBusy(true);
      try {
        const list = Object.entries(ov).map(([seq, v]) => ({ seq: Number(seq), ...v }));
        const a = assume == null ? "" : `&assume_m=${assume}`;
        const sp = spdRef.current.wp;
        const q = `?wp_spd=${sp ?? ""}${spdRef.current.rad != null
          ? `&wp_radius=${spdRef.current.rad}` : ""}${a}`;
        // 畫面接手圍欄之後就一律走 preview——GET 那條讀的是資料庫裡的
        // 圍欄，畫面上剛畫的那個它看不到
        if (list.length || fenceOwnRef.current) {
          const r = await fetch(`${API}/api/plans/${id}/preview`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ overrides: list, wp_spd: sp,
                                   wp_radius: spdRef.current.rad, assume_m: assume,
                                   rtl_alt_m: spdRef.current.rtl,
                                   fence: fenceBody() }),
          });
          const d = await r.json();
          if (r.ok) { setChk(d.check); setProf(d.profile); }
        } else {
          const [pr, ck] = await Promise.all([
            getJson<Profile>(`${API}/api/plans/${id}/profile?${
              [assume == null ? "" : `assume_m=${assume}`,
               spdRef.current.rtl != null
                 ? `rtl_alt_m=${spdRef.current.rtl}` : ""]
                .filter(Boolean).join("&")}`),
            getJson<Check>(`${API}/api/plans/${id}/check${q}`),
          ]);
          setProf(pr); setChk(ck);
        }
      } catch { /* 讀不到就維持上一份，畫面不要空掉 */ } finally { setBusy(false); }
    }, 260);
    return () => clearTimeout(t);
  }, [assume, id, isNew, ov, fence]);

  // 改動 → 試算。**去抖**：拖滑桿一秒會產生幾十次變動，而每一次都要
  // 沿線取樣 DEM——沒有去抖等於用滑桿打後端
  useEffect(() => {
    const list = Object.entries(ov).map(([seq, v]) => ({ seq: Number(seq), ...v }));
    if (!list.length || isNew) return;
    const t = setTimeout(async () => {
      setBusy(true);
      try {
        const r = await fetch(`${API}/api/plans/${id}/preview`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ overrides: list, wp_spd: spdRef.current.wp,
                                 wp_radius: spdRef.current.rad,
                                 rtl_alt_m: spdRef.current.rtl,
                                 assume_m: assumeRef.current,
                                 fence: fenceBody() }),
        });
        const d = await r.json();
        if (r.ok) { setChk(d.check); setProf(d.profile); }
      } finally { setBusy(false); }
    }, 260);
    return () => clearTimeout(t);
  }, [ov, id, isNew]);

  const defAssume = chk?.limits?.assumed_default_m ?? 9;
  /** 從**結構化的發現**推出可以按的按鈕。文字訊息不解析——那會跟著文案漂 */
  const fixes = (() => {
    if (!chk) return [] as { name: string; seq: number; label: string; hint: string }[];
    const out: { name: string; seq: number; label: string; hint: string }[] = [];
    const ls = chk.legs ?? [];
    const worst = ls.reduce<Leg | null>(
      (m, l) => (l.agl_m == null ? m : m == null || l.agl_m < m.agl_m! ? l : m), null);
    const lim = chk.limits;
    if (worst?.agl_m != null && lim && worst.agl_m < 2) {
      out.push({ name: "raise_all", seq: -1, label: "整條抬高",
        hint: `最低那一段現在 ${worst.agl_m} m` });
      out.push({ name: "raise_leg", seq: worst.from,
        label: `只抬 seq ${worst.from}→${worst.to}`,
        hint: "那兩個點會變成例外，之後改政策不會動它們" });
    }
    const fast = ls.find((l) => l.low_fast);
    if (fast && lim) {
      out.push({ name: "slow_all", seq: -1,
        label: `整條降到 ${lim.low_speed_ms} m/s`, hint: "低空帶速的門檻" });
      out.push({ name: "slow_leg", seq: fast.from,
        label: `只降 seq ${fast.from}→${fast.to}`,
        hint: "會在那個航點之前多插一個改速度項" });
      out.push({ name: "raise_leg_low", seq: fast.from,
        label: `把 seq ${fast.from}→${fast.to} 抬到 ${lim.low_alt_m} m 以上`,
        hint: "另一條路：不降速，改成飛高一點" });
    }
    if ((chk.terrain_blind?.length ?? 0) > 0 && assume == null) {
      out.push({ name: "assume", seq: -1, label: `套用假設高度 ${defAssume} m`,
        hint: "未量測的建物都當成這個高度算。**那是旋鈕不是量測值**" });
    }
    return out;
  })();

  const act = async (name: string, seq: number) => {
    setBusy(true);
    try {
      const r = await fetch(`${API}/api/plans/draft`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          home: [Number(home.lat), Number(home.lon)], points: pts, policy: pol,
          wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
          rtl_alt_m: spdRef.current.rtl,
          assume_m: assume, action: { name, seq: seq < 0 ? null : seq } }),
      });
      const d = await r.json();
      if (!r.ok) return;
      // **後端算完之後把新的狀態拿回來**：政策、點、假設值都可能被改
      setChk(d.check); setProf(d.profile); setDecisions(d.decisions ?? []);
      setEnding(d.ending ?? null);
      setApplied(d.applied ?? null);
      if (d.policy) setPol(d.policy);
      if (d.points) setPts(d.points);
      if (d.assume_m != null) setAssume(d.assume_m);
    } finally { setBusy(false); }
  };
  const legs = chk?.legs ?? [];
  // 3D 要的是「航點」，而剖面回的是沿線取樣——帶 seq 的那幾筆就是航點。
  // **高度換算在這裡做一次**（profile 的 `plan` 已經是 AMSL），
  // 3D 元件不猜高度基準
  const lowAlt = chk?.limits?.low_alt_m ?? 3;
  const badSeq = new Set(legs.filter((l) => l.low_fast).map((l) => l.to));
  /** 滑鼠指到東西時要顯示什麼。**由這一頁決定**：航段的長度、速度、來源、
   *  判定都住在這裡，讓 3D 那個元件自己再查一次就會有兩份可能不同步的資料。 */
  const tipFor = (h: StageHit): StageTip | null => {
    if (h.kind === "wp") {
      const w = stageWps[h.i];
      if (!w) return null;
      const p = (prof?.points ?? []).find((x) => x.seq === w.seq);
      return {
        title: `seq ${w.seq}`,
        rows: [
          ["離地", p?.agl != null ? `${p.agl} m` : "沒有地形資料"],
          ["規劃高度", `${w.amsl.toFixed(1)} m 海拔`],
          ["地面", w.ground != null ? `${w.ground} m 海拔` : "—"],
        ],
      };
    }
    // 第 i 段＝ stageWps[i-1] → stageWps[i]；逐段表用 seq 對得起來
    const a = stageWps[h.i - 1], b = stageWps[h.i];
    if (!a || !b) return null;
    const leg = legs.find((l) => l.from === a.seq && l.to === b.seq);
    if (!leg) return null;
    return {
      title: `seq ${leg.from}→${leg.to}`,
      rows: [
        ["長度", `${leg.length_m} m`],
        ["離地", leg.agl_m != null ? `${leg.agl_m} m` : "沒有地形資料"],
        ["速度", leg.speed_ms != null ? `${leg.speed_ms} m/s` : "未讀到"],
        // **「來源」是這個 tooltip 最重要的一行**：一眼看出這一段用的是
        // 機上的 WP_SPD，還是航線裡寫的那個（2026-09-07 的誤會）
        ["來源", leg.speed_src === "unknown" ? "未讀到（沒檢查）" : leg.speed_src],
        ...(leg.turn_deg != null
          ? ([["轉角", `${leg.turn_deg}°`]] as [string, string][]) : []),
      ],
      bad: !!leg.low_fast,
    };
  };

  /** **加上回程**（issues/065／066，使用者 2026-09-22：規劃動作，不是路徑屬性）。
   *  從零模式：直接在點列上接反序的去程點（去掉折返點本身）。**回程點不帶停留**——
   *  使用者裁定回程停留可分別設定，照抄去程會讓每個點被量兩倍久而不自知。
   *  既有航線：後端另存一份「（含回程）」再打開它——原本那份與它的簽核不動 */
  const addReturn = async () => {
    if (isNew) {
      setPts((p) => {
        const land = p.length && p[p.length - 1].kind === "land" ? p[p.length - 1] : null;
        const body = land ? p.slice(0, -1) : p;
        if (body.length < 2) return p;
        const back = body.slice(0, -1).reverse().map((q) => ({
          lat: q.lat, lon: q.lon, h: q.h, alt_source: q.alt_source, kind: "wp" }));
        return [...body, ...back, ...(land ? [land] : [])];
      });
      return;
    }
    setBusy(true);
    try {
      const r = await fetch(`${API}/api/plans/${id}/add-return`, { method: "POST" });
      const j = await r.json().catch(() => null);
      if (!r.ok) { setErr(`加上回程失敗：${j?.detail ?? r.status}`); return; }
      window.location.href = `/plans/${j.id}/plan`;
    } catch (e) {
      setErr(`加上回程失敗：${e}`);
    } finally {
      setBusy(false);
    }
  };

  /** 從零畫的航線還沒有結尾——存檔要擋（使用者 2026-09-23）。
   *  既有航線不在此列：它本來就有自己的結尾項 */
  const noEnding = isNew && pts.length > 0 && ending === "none";

  const stageWps: StageWp[] = (prof?.points ?? [])
    .filter((p) => p.seq != null && p.plan != null)
    .map((p) => ({ seq: p.seq as number, lat: p.lat ?? 0, lon: p.lon ?? 0,
      amsl: p.plan as number, ground: p.ground,
      bad: badSeq.has(p.seq as number), fixed: !isNew && p.seq === 0,
      kind: p.kind, auto: p.auto, srcI: p.src_i ?? null, hold: p.hold_s ?? 0 }))
    .map((w) => {
      // **位置直接讀操作員那份，不等後端。** 剖面要跑一趟後端才回來，
      // 中間那幾百毫秒點不動，拖起來像卡住（使用者 2026-09-09）。
      // 系統補的中繼點沒有 `srcI`，它們本來就要重算才知道在哪
      if (isNew) {
        if (w.kind === "takeoff" && hasHome)
          return { ...w, lat: Number(home.lat), lon: Number(home.lon) };
        const m = w.srcI != null ? pts[w.srcI] : null;
        return m ? { ...w, lat: m.lat, lon: m.lon } : w;
      }
      const o = ov[w.seq];
      return o?.lat != null ? { ...w, lat: o.lat, lon: o.lon as number } : w;
    });
  /** 圍欄圓心永遠是起飛點——它是唯一飛機一定經過的地方，也對得上飛控的
   *  `FENCE_RADIUS`（那顆也是以 home 為心） */
  const fenceHome: [number, number] | null = isNew
    ? (hasHome ? [Number(home.lat), Number(home.lon)] : null)
    : (() => {
        const t = stageWps.find((w) => w.kind === "takeoff") ?? stageWps[0];
        return t ? [t.lat, t.lon] : null;
      })();
  const fenceShape: FenceShape | null =
    fence.shape === "circle"
      ? (fenceHome && fence.radius_m
          ? { shape: "circle", center: fenceHome, radius_m: fence.radius_m } : null)
      : fence.shape === "polygon" && fence.points.length
        ? { shape: "polygon", points: fence.points }
        : null;
  return (
    <div className="page">
      {/* **結論在前，出處收成一顆。**（使用者裁定 2026-09-09，選項 A）
          原本是三列共 100 px，而地圖只有 456 px。晶片本身都是事實、不能刪
          ——問題是它們**一樣大聲**：「最低離地 2 m」是這一頁在回答的事，
          「取自機上（現在讀的）」是出處。前面幾顆會變紅、會變；
          不會變的脈絡併成一顆灰的，想知道才去碰。
          「← 路徑管理」縮成箭頭：那幾個字每一頁都一樣，佔的是標題的位置。 */}
      {/* **名稱與按鈕選項同一列**（使用者 2026-09-09）：原本表頭一列、
          工具列一列，兩列加起來 100 px，而地圖只剩一小條 */}
      <div className="plan-head newform">
        <Link href="/plans" className="btn-plain btn-sm" title="回路徑管理">←</Link>
        {renaming == null ? (
          <h1 className="mtitle" title={isNew ? undefined : "雙擊改名"}
            onDoubleClick={() => { if (!isNew) setRenaming(name); }}>
            {name || "…"}</h1>
        ) : (
          <input className="mtitle title-edit" autoFocus value={renaming}
            onChange={(e) => setRenaming(e.target.value)}
            onBlur={() => setRenaming(null)}
            onKeyDown={async (e) => {
              if (e.key === "Escape") { setRenaming(null); return; }
              if (e.key !== "Enter") return;
              const v = renaming.trim();
              if (!v) { setRenaming(null); return; }
              const r = await fetch(`${API}/api/plans/${id}`, {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: v }),
              });
              if (r.ok) setName(v);
              else setErr(errText((await r.json()).detail, "改名失敗"));
              setRenaming(null);
            }} />
        )}
        {renaming != null && (
          <span className="hint-line">Enter 存・Esc 取消</span>
        )}
        {isNew && (
          <>
          {/* **使用者不會記得經緯度**（2026-09-11）：先用地址把地圖移到大致的
              地方，起飛點還是自己點。座標也收在同一格——貼上就跳過去 */}
          <div className="f geo-box"><span>地址、地標或座標</span>
            <input value={geoQ} placeholder="竹東鎮中興路四段" style={{ width: 150 }}
              onChange={(e) => setGeoQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") geocode(); }} />
            {geo && (
              <div className="geo-list">
                {geo.err
                  ? <div className="hint-line">{geo.err}</div>
                  : !geo.results.length
                    ? <div className="hint-line">找不到。換個寫法試試（路名、地標）</div>
                    : <>
                        {geo.fallback && (
                          <div className="hint-line">「{geo.query}」查不到，改查「{geo.used}」</div>
                        )}
                        {geo.results.map((g, i) => (
                          <div key={i} className="geo-row">
                            {/* 同名的結果可以差一公里以上（實測「中興路四段」兩筆
                                差 1.2 km），名字一樣時座標是唯一分得開的東西 */}
                            <span>{g.name}
                              {geo.results.filter((x) => x.name === g.name).length > 1 && (
                                <span className="muted num">　{g.lat.toFixed(4)}, {g.lon.toFixed(4)}</span>
                              )}</span>
                            <span className="chip">{g.precision === "座標" ? "座標"
                              : g.precision === "門牌" ? "門牌" : `只到${g.precision}`}</span>
                            <button className="btn-plain btn-sm" onClick={() => {
                              setFlyTo({ lat: g.lat, lon: g.lon, n: Date.now() });
                              setStarted(true); setGeo(null);
                            }}>移過去</button>
                            {g.precision === "座標" && (
                              <button className="btn-plain btn-sm" onClick={() => {
                                setHome({ lat: g.lat.toFixed(7), lon: g.lon.toFixed(7) });
                                setFlyTo({ lat: g.lat, lon: g.lon, n: Date.now() });
                                setPlaceKind("wp"); setStarted(true); setGeo(null);
                              }}>設為起飛點</button>
                            )}
                          </div>
                        ))}
                      </>}
              </div>
            )}
          </div>
          {/* **開始畫之前只問起飛點。** 高度與速度在看到地形之後才談——
              舊版要人在放第一個點以前就填那兩個數字，順序是反的 */}
          {started && (
            <>
              {/* **高度基準只留兩個**（使用者 2026-09-23）：「固定海拔」日常規劃用不到，
                  而且在這個場地填 3 會變成地面以下。匯入的 .plan 仍可能是絕對高度
                  （frame 0），那是讀進來的事實，不受這裡影響 */}
              <div className="f"><span>高度基準<InfoTip
                tip={"你在右欄填的高度，是從哪裡量起：\n"
                  + "**離地面**＝離那個點正下方的地面（用地面站的地形資料逐點換算，"
                  + "兩點之間隔著土坡會自動補中繼點）。\n"
                  + "**離起飛點**＝離你解鎖的那個位置（地面起伏不管）。\n"
                  + "兩者算出來的都是規劃時就定死的數字，飛行中不會再變。"} /></span>
                <div className="seg2">
                  {(["agl", "home"] as const).map((k) => (
                    <button key={k} aria-pressed={pol.mode === k}
                      onClick={() => setPol((q) => ({ ...q, mode: k }))}>
                      {MODE_TEXT[k]}</button>
                  ))}
                </div>
              </div>
              {/* **高度與速度的欄位拿掉了**（使用者 2026-09-23：「如果右邊欄位可以調整
                  高度跟速度了，上面那一排應該就不用有」）。新放的點用預設值
                  （3 m、1 m/s），要改就選那個點在右欄改——**一個入口，不是兩個** */}
              <div className="f"><span>放點類型</span>
                <div className="seg2">
                  {[["home", "起飛點"], ["wp", "航點"], ["land", "降落點"]].map(([k, t]) => (
                    <button key={k} aria-pressed={placeKind === k}
                      onClick={() => setPlaceKind(k)}>{t}</button>
                  ))}
                </div>
              </div>
            </>
          )}
          {/* **提示字拿掉**（使用者 2026-09-09）：「先點一下放起飛點」「降落
              在最後一個航點」那幾句每一列都在，而它們說的事「放點類型」
              那組按鈕與決策表都已經寫著。留下的只有真的要按的東西 */}
          {!started
            ? <button className="btn-accent btn-sm" onClick={() => setStarted(true)}>
                開始畫線</button>
            : pts.length > 0 && <button className="btn-plain btn-sm"
                onClick={() => setPts((p) => p.slice(0, -1))}>移除上一個</button>}
          </>
        )}
        {/* 圍欄（使用者裁定 2026-09-09：圓形＋多邊形，只做規劃端）。
            **飛控不照這個擋**——那句話跟著晶片走，見這一列尾巴的晶片。
            它跟其他選項同一列：自己佔一列的話地圖又矮 60 px */}
        {(stageWps.length > 0 || (isNew && started)) && (
          <>
            <span className="head-sep" />
          <div className="f"><span>圍欄<InfoTip tip={"航點超出圍欄，這一頁會擋下。\n**飛控不照它擋**：這個圍欄不會寫進飛控，飛機飛出去時沒有東西攔它。\n高度上限是**離起飛點**的高度；地形跟隨（frame 10）的航點高度不是離起飛點的，比不了，會照實說。"} /></span>
            <div className="seg2">
              {([["circle", "圓形"], ["polygon", "多邊形"],
                 ["none", "不設"]] as const).map(([k, t]) => (
                <button key={k} aria-pressed={fence.shape === k}
                  onClick={() => { editFence({ ...fence, shape: k });
                                   setFenceDraw(k === "polygon");
                                   if (k === "polygon") setSelWp(-1); else setFenceSel(-1); }}>{t}</button>
              ))}
            </div>
          </div>
          {fence.shape === "circle" && (
            <label className="f f-num"><span>半徑 m<InfoTip tip="圓心是起飛點" /></span>
              <input type="number" step="10" value={fence.radius_m ?? ""}
                onChange={(e) => editFence({ ...fence,
                  radius_m: e.target.value === "" ? null : Number(e.target.value) })} />
            </label>
          )}
          {fence.shape === "polygon" && (
            <>
              {/* **按下去時只編輯圍欄**：航線變淡、點不到；放開時反過來 */}
              <button className="btn-plain btn-sm" aria-pressed={fenceDraw}
                onClick={() => {
                  const on = !fenceDraw;
                  setFenceDraw(on);
                  if (on) setSelWp(-1); else setFenceSel(-1);
                }}>
                編輯圍欄</button>
              <button className="btn-plain btn-sm"
                disabled={!fence.points.length}
                onClick={() => { editFence({ ...fence, points: [] }); setFenceSel(-1); }}>
                清掉重畫</button>
            </>
          )}
          {fence.shape !== "none" && (
            <label className="f f-num"><span>高度上限 m</span>
              <input type="number" step="5" placeholder="不設"
                value={fence.alt_max_m ?? ""}
                onChange={(e) => editFence({ ...fence,
                  alt_max_m: e.target.value === "" ? null : Number(e.target.value) })} />
            </label>
          )}
          </>
        )}
        {/* **最低離地／返航／脈絡那三顆晶片砍了**（使用者 2026-09-09）：
            它們佔掉一整列，而地圖被擠扁。同樣的數字剖面圖上都畫得出來
            ——最窄那一段有標、返航是圖上那條虛線、高度基準與起飛點海拔
            在剖面的 Y 軸上。這一列只留**看圖看不出來的**：圍欄與審查狀態 */}
        {fenceShape && (
          // 半徑與上限在旁邊的輸入格裡看得到，這一顆只說看不到的兩件事：
          // 多邊形成不成立，以及**飛控不照它擋**——不然畫了一個圈會被讀成飛不出去
          <span className={`chip${fence.shape === "polygon" && fence.points.length < 3 ? "" : " bad"}`}>
            {fence.shape === "polygon"
              ? `${fence.points.length} 點・${fence.points.length < 3 ? "還不成立" : "飛控不擋"}`
              : "飛控不擋"}</span>
        )}
        {!isNew && sign && (
          <span className={`chip${sign.signed && !sign.stale ? "" : " bad"}`}
            title={sign.why ?? undefined}>
            {sign.signed && !sign.stale
              ? `已審查 ${(sign.checked_at ?? "").slice(11, 16)}`
              : sign.stale ? "簽核已失效" : "未審查"}
          </span>
        )}
      </div>

      {err && <div className="form-err">{err}</div>}

      {/* 3D 地形（issues/048 F1）。**地形是真的**：maplibre 吃我們自己從
          `.hgt` 產的圖磚。原型那張手繪線框到此為止 */}
      {/* 只有起飛點時也要畫得出來——`stageWps.length` 會是 1 */}
      {(stageWps.length > 0 || (isNew && started)) && (
        <div className="plan-work">
          <TerrainStage wps={stageWps} sel={selWp}
            onSelect={(i) => { setSelWp(i); setFenceSel(-1); }}
            assumeM={assume} onBuildings={onBlds}
            tipFor={tipFor}
            fence={fenceShape}
            editTarget={fenceDraw ? "fence" : "route"}
            fenceSel={fence.shape === "polygon" ? fenceSel : -1}
            onFenceSelect={(i) => { setFenceSel(i); setSelWp(-1); }}
            onFenceMove={(i, l) => {
              setFence((f) => ({ ...f,
                points: f.points.map((p, j) => (j === i ? [l.lat, l.lng] : p)) }));
              setFenceOwn(true);
            }}
            placing={(isNew && started) || fenceDraw}
            flyTo={flyTo}
            center={isNew
              ? [hasHome ? Number(home.lon) : flyTo?.lon ?? VIEW.lon,
                 hasHome ? Number(home.lat) : flyTo?.lat ?? VIEW.lat]
              : undefined}
            onPlace={(l) => {
              // 畫圍欄的時候地圖的點擊是頂點，不是航點——**一次只能在畫
              // 一種東西**，不然使用者分不出下一下會加到哪裡
              if (fenceDraw && fence.shape === "polygon") {
                const r = insertVertex(fence.points, [l.lat, l.lng]);
                editFence({ ...fence, points: r.points });
                setFenceSel(r.at);
                return;
              }
              // **起飛點是解鎖的地方，不是航線上的一個點**——它寫進 home，
              // 不進 pts。放完自動切回航點，不然下一下又蓋掉起飛點
              if (placeKind === "home") {
                setHome({ lat: String(l.lat.toFixed(7)),
                          lon: String(l.lng.toFixed(7)) });
                setPlaceKind("wp");
                // **放完就選中它**：右欄因此立刻可以設起飛高度，
                // 不必再回頭點一次（使用者 2026-09-09）。剖面回來之後
                // stageWps[0] 就是起飛點
                setSelWp(0);
                return;
              }
              setPts((p) => {
                setSelWp(p.length + 1);
                return [...p, { lat: l.lat, lon: l.lng, kind: placeKind }];
              });
            }}
            onMove={(i, l) => {
              if (isNew) {
                if (stageWps[i]?.kind === "takeoff") {
                  setHome({ lat: String(l.lat.toFixed(7)),
                            lon: String(l.lng.toFixed(7)) });
                  return;
                }
                const k = stageWps[i]?.srcI ?? -1;
                if (k < 0 || k >= pts.length) return;
                setPts((p) => p.map((q, j) =>
                  j === k ? { ...q, lat: l.lat, lon: l.lng } : q));
                return;
              }
              const w = stageWps[i];
              if (!w || w.fixed) return;
              setOv((o) => ({ ...o,
                [w.seq]: { ...o[w.seq], lat: l.lat, lon: l.lng } }));
            }} />
          {/* **地圖拿回整個寬度，右欄浮在上面**（使用者裁定 2026-09-09，
              選項 F）。右欄本來就只在「選到一個航點」時才有內容——讓它蓋住
              一小塊地形，比永久佔掉 264 px 划算。可以收起來看底下那塊。 */}
          <aside className={`plan-rail${railOpen ? "" : " shut"}`}>
            <h2>航點
              <button className="rail-toggle" title={railOpen ? "收起" : "展開"}
                onClick={() => setRailOpen((v) => !v)}>{railOpen ? "▸" : "◂"}</button>
            </h2>
            {/* **航點列表：不會失敗的那條路**（issues/065 A、063、066）。地圖上點不到
                的時候——來回路徑的點疊在一起、點在建物後面——這裡永遠點得到。
                只列操作員放的點（系統補的中繼點會被重算掉，選了也沒用） */}
            {(() => {
              const rows = stageWps.map((w, i) => ({ w, i })).filter(({ w }) => !w.auto);
              if (!rows.length) return null;
              const homeAmsl = prof?.home_amsl_m ?? null;
              const kindText = (k?: string) => k === "takeoff" ? "起飛" : k === "land" ? "降落" : "航點";
              const holdOf = (w: StageWp) => isNew
                ? (w.srcI != null ? pts[w.srcI]?.hold_s ?? 0 : 0)
                : (ov[w.seq]?.hold ?? w.hold ?? 0);
              // **列表與右欄要用同一個基準**（2026-09-23 實測：政策是「離地面 3 m」時，
              // 右欄寫 3、列表寫 −1.2，同一個點兩個數字——列表寫死成「離起飛點」造成的）
              const basis = isNew ? MODE_TEXT[pol.mode] : "離起飛點";
              const altOf = (w: StageWp) => {
                if (w.kind === "land") return null;            // 降落是降到地面
                if (isNew) {
                  if (w.kind === "takeoff") return pol.takeoff_alt_m ?? Math.max(1.5, pol.height_m);
                  return w.srcI != null ? pts[w.srcI]?.h ?? pol.height_m : pol.height_m;
                }
                if (ov[w.seq]?.alt != null) return ov[w.seq].alt!;
                return homeAmsl != null ? Math.round((w.amsl - homeAmsl) * 10) / 10 : null;
              };
              const setHold = (w: StageWp, v: number) => {
                const hv = Math.max(0, Math.min(3600, Number.isFinite(v) ? v : 0));
                if (isNew) {
                  if (w.srcI == null) return;
                  setPts((p) => p.map((q, j) =>
                    j === w.srcI ? { ...q, hold_s: hv > 0 ? hv : undefined } : q));
                } else {
                  setOv((o) => ({ ...o, [w.seq]: { ...o[w.seq], hold: hv } }));
                }
              };
              const canHoldRow = (w: StageWp) =>
                w.kind === "wp" && !w.auto && (!isNew || w.srcI != null);
              return (
                <div className="wp-list">
                  <div className="wp-list-acts">
                    <button className="btn-plain btn-sm" disabled={busy}
                      title={isNew
                        ? "把去程的航點反序接在後面（回程點不帶停留，之後逐點設）"
                        : "把去程的航點反序接在後面，**另存成新的一份**（原本那份與它的簽核不動）"}
                      onClick={addReturn}>↩ 加上回程{isNew ? "" : "（另存一份）"}</button>
                  </div>
                  <table>
                    <thead><tr><th>#</th><th>類型</th>
                      <th className="num" title={`高度（${basis}，公尺）——與右欄同一個基準`}>
                        高度<span className="muted">（{basis}）</span></th>
                      <th className="num" title="到點停留（秒）：可以直接在這裡改">停留</th>
                      {isNew && <th />}</tr></thead>
                    <tbody>
                      {rows.map(({ w, i }, n) => {
                        const h = holdOf(w);
                        const alt = altOf(w);
                        return (
                          <tr key={`${w.seq}-${i}`} className={i === selWp ? "on" : undefined}
                            onClick={() => {
                              setSelWp(i); setFenceSel(-1);
                              if (w.lat && w.lon) setFlyTo({ lat: w.lat, lon: w.lon, n: Date.now() });
                            }}>
                            <td className="num">{n + 1}</td>
                            <td>{kindText(w.kind)}</td>
                            {/* 降落點是降到地面，不是它在剖面圖上借來的那個高度 */}
                            <td className="num">{w.kind === "land" ? "地面" : alt ?? "—"}</td>
                            {/* **停留可以直接在列上改**（使用者 2026-09-23：「沒有按鈕或其他
                                操作方法讓我設定點位停留時間」——原本只有右欄最底下那一欄，
                                要先選點、還要捲到看得見）。離開欄位才套用 */}
                            <td className="num">
                              {canHoldRow(w)
                                ? <input className="numin numin-cell" type="number" min={0}
                                    max={3600} step={1} defaultValue={h || ""} placeholder="0"
                                    onClick={(e) => e.stopPropagation()}
                                    onBlur={(e) => setHold(w, Number(e.target.value))}
                                    onKeyDown={(e) => {
                                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                                    }} />
                                : h ? `${h} s` : ""}
                            </td>
                            {isNew && (
                              <td>{w.kind === "wp" && w.srcI != null && (
                                <button className="btn-plain btn-sm" title="刪除這個航點"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    const k = w.srcI!;
                                    setPts((p) => p.filter((_, j) => j !== k)); setSelWp(-1);
                                  }}>✕</button>)}</td>)}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              );
            })()}
            <h2 className="rail-sub">{fenceDraw && fence.shape === "polygon" ? "選取的圍欄頂點" : "選取的航點"}</h2>
            {(() => {
              const fv = fenceDraw && fence.shape === "polygon" ? fence.points[fenceSel] : undefined;
              if (fv) return (
                <>
                  <div className="rail-row"><span>圍欄頂點</span>
                    <b className="num">{fenceSel + 1}／{fence.points.length}</b></div>
                  <div className="rail-row"><span>位置</span>
                    <b className="num">{fv[0].toFixed(6)}, {fv[1].toFixed(6)}</b></div>
                  <button className="btn-plain btn-sm" onClick={() => {
                    setFence((f) => ({ ...f, points: f.points.filter((_, j) => j !== fenceSel) }));
                    setFenceOwn(true); setFenceSel(-1);
                  }}>刪除這個頂點</button>
                </>
              );
              const w = stageWps[selWp];
              if (!w) return <div className="hint-line">在 3D 上點一個航點</div>;
              const out = legs.find((l) => l.from === w.seq);   // 從它出發的那一段
              const cur = ov[w.seq] ?? {};
              // 從零模式下編輯的是**政策單位下的高度**，不是 frame 3 的數字。
              // **認 `srcI`，不用畫面上的位置去數**：系統補的中繼點也在
              // 序列裡，數下去會改到別的點
              const mi = isNew ? (w.srcI ?? -1) : -1;
              const mine = mi >= 0 ? pts[mi] : null;
              const isEx = isNew && mine?.alt_source === "manual";
              const alt = isNew
                ? (w.kind === "takeoff"
                    ? (pol.takeoff_alt_m ?? Math.max(1.5, pol.height_m))
                    : mine?.h ?? pol.height_m)
                : cur.alt ?? Math.round((w.amsl - (prof?.home_amsl_m ?? 0)) * 10) / 10;
              const spdNow = cur.speed ?? out?.speed_ms ?? null;
              // 到點停留（062）。**只有一般航點能停**——起飛、降落的 param1
              // 意思不同；從零模式下系統補的中繼點也不行（它們會被重算掉）
              const canHold = w.kind === "wp" && !w.auto && (!isNew || mi >= 0);
              const holdNow = isNew ? (mine?.hold_s ?? 0) : (cur.hold ?? w.hold ?? 0);
              const set = (k: "alt" | "speed" | "hold", v: number) => {
                if (k === "hold") {
                  const hv = Math.max(0, Math.min(3600, Number.isFinite(v) ? v : 0));
                  if (isNew) {
                    setPts((p) => p.map((q, j) =>
                      j === mi ? { ...q, hold_s: hv > 0 ? hv : undefined } : q));
                  } else {
                    setOv((o) => ({ ...o, [w.seq]: { ...o[w.seq], hold: hv } }));
                  }
                  return;
                }
                if (isNew) {
                  // **改一個點的高度＝把它變成例外。** 之後改政策不會動它
                  if (k === "alt" && mi >= 0)
                    setPts((p) => p.map((q, j) =>
                      j === mi ? { ...q, h: v, alt_source: "manual" } : q));
                  // 起飛點的高度 → 政策的 takeoff_alt_m（**不是某個航點的 h**）
                  if (k === "alt" && w.kind === "takeoff")
                    setPol((q) => ({ ...q, takeoff_alt_m: v }));
                  // **速度改的是「這個點之後那一段」**（使用者 2026-09-23 拿掉
                  // 工具列那一欄之後，這裡就是唯一的入口）。起飛點沒有自己的
                  // 點物件，它那一段仍然是政策速度
                  if (k === "speed") {
                    if (mi >= 0) {
                      setPts((p) => p.map((q, j) => j === mi ? { ...q, speed_ms: v } : q));
                    } else {
                      setPol((q) => ({ ...q, speed_ms: v }));
                    }
                  }
                  return;
                }
                setOv((o) => ({ ...o, [w.seq]: { ...o[w.seq], [k]: v } }));
              };
              return (
                <>
                  <div className="rail-row"><span>航點</span>
                    <b className="num">seq {w.seq}</b></div>
                  {isNew && w.kind === "takeoff" && hasHome && (
                    <div className="rail-row"><span>位置</span>
                      <b className="num">{Number(home.lat).toFixed(6)}, {Number(home.lon).toFixed(6)}</b></div>
                  )}
                  {isNew && w.kind === "takeoff" && (
                    // 刪掉之後回到「放起飛點」：航點留著，重新放一個起飛點就接回去
                    <button className="btn-plain btn-sm" onClick={() => {
                      setHome({ lat: "", lon: "" }); setProf(null); setChk(null);
                      setDecisions([]); setPlaceKind("home"); setSelWp(-1);
                    }}>刪除起飛點</button>
                  )}
                  {mi >= 0 && (
                    <>
                      <div className="seg2">
                        {[["wp", "航點"], ["land", "降落點"]].map(([k, t]) => (
                          <button key={k}
                            aria-pressed={(mine?.kind ?? "wp") === k}
                            onClick={() => {
                              setPts((p) => p.map((q, j) =>
                                j === mi ? { ...q, kind: k } : q));
                              // 標成降落點就是為了降在那裡。改回航點時若已經
                              // 沒有降落點了，就回到降落在起飛點
                              if (k === "land") setPol((q) => ({ ...q, land_at_home: false }));
                            }}>{t}</button>
                        ))}
                      </div>
                      {mine?.kind === "land" && (
                        <>
                          <div className="rail-field">
                            <span className="k">降落在哪裡</span>
                            <div className="seg2">
                              <button aria-pressed={pol.land_at_home}
                                onClick={() => setPol((q) => ({ ...q, land_at_home: true }))}>
                                起飛點</button>
                              <button aria-pressed={!pol.land_at_home}
                                onClick={() => setPol((q) => ({ ...q, land_at_home: false }))}>
                                這個位置</button>
                            </div>
                          </div>
                          <div className="rail-field">
                            <span className="k">降落方式</span>
                            <div className="seg2">
                              <button aria-pressed={pol.land_mode === "vert"}
                                onClick={() => setPol((q) => ({ ...q, land_mode: "vert" }))}>
                                垂直</button>
                              <button aria-pressed={pol.land_mode === "glide"}
                                onClick={() => setPol((q) => ({ ...q, land_mode: "glide" }))}>
                                逐漸</button>
                            </div>
                          </div>
                        </>
                      )}
                      <button className="btn-plain btn-sm"
                        onClick={() => { setPts((p) =>
                          p.filter((_, j) => j !== mi)); setSelWp(-1); }}>
                        刪除這個點</button>
                      <div className="hint-line">3D 上拖曳航點只移動位置；
                        高度、速度、停留在下面填。</div>
                    </>
                  )}
                  {isEx && (
                    <div className="rail-row">
                      <span className="tag-warn">這個點是例外</span>
                      <button className="btn-plain btn-sm"
                        onClick={() => setPts((p) => p.map((q, j) =>
                          j === mi
                            ? { lat: q.lat, lon: q.lon, kind: q.kind } : q))}>
                        收回，跟著政策</button>
                    </div>
                  )}
                  {/* **起飛點的高度不是航點的高度。** 它是「飛機會先爬到
                      這裡才往第一個航點飛」，而且天生是離起飛點的
                      （使用者 2026-09-09）。標錯的話它會被讀成又一個航點高度 */}
                  <NumField
                    label={w.kind === "takeoff" ? "起飛高度（離起飛點）"
                      : `高度（${isNew ? MODE_TEXT[pol.mode] : "離起飛點"}）`}
                    tip={w.kind === "takeoff"
                      ? "飛機會**先爬到這個高度**才往第一個航點飛。"
                        + "用比其他點低的高度起飛，第一段會在地面爬升處貼地。"
                      : isNew && pol.mode === "agl"
                        ? "離**這個點正下方的地面**多高（用地面站的地形資料逐點換算）。"
                          + "改這一個點不會動到別的點。"
                        : "離**起飛點**多高。改這一個點不會動到別的點。"}
                    value={alt} min={-50} max={120} step={0.1} unit="m"
                    onCommit={(v) => set("alt", v)} />
                  {out && (
                    <NumField label="下一段速度" unit="m/s"
                      tip={"從這個點飛往下一個點的速度（寫成航線裡的 DO_CHANGE_SPEED）。"
                        + "**它只從被執行到的那一項之後才生效**，所以擺在這個點之前。"}
                      value={spdNow ?? 1} min={0.2} max={15} step={0.1}
                      onCommit={(v) => set("speed", v)} />
                  )}
                  {canHold && (
                    <NumField label="到點停留（秒）" unit="秒"
                      tip={"飛到這一點後**停在原地**這麼久，再飛往下一點（寫進航點的 param1）。"
                        + "定點量測用：飛過去只有一兩筆樣本，停 30 秒就是約 30 筆。"
                        + "停留期間的樣本在任務歷史裡會標成靜止量測。0＝不停。"}
                      value={holdNow} min={0} max={3600} step={1}
                      onCommit={(v) => set("hold", v)} />
                  )}
                  {out && (() => {
                    // **穿地與低空帶速是兩條不同的規則。** 只看 `low_fast`
                    // 會在離地是負的時候寫「這一段通過」——綠色、而且是錯的
                    // （2026-09-08 從零產生時當場撞到：離地 −1.4 m 配 1 m/s，
                    // 速度沒超標所以 low_fast 是 false）
                    const under = out.agl_m != null && out.agl_m < 0;
                    const bad = under || out.low_fast;
                    return (
                      <div className={`verdict ${bad ? "bad" : "ok"}`}>
                        {under
                          ? `離地 ${out.agl_m} m——這一段在地面以下，飛不了`
                          : out.low_fast
                            ? `離地 ${out.agl_m} m 卻要飛 ${out.speed_ms} m/s——低於 ${lowAlt} m 時地面會擾動這架飛機`
                            : `離地 ${out.agl_m ?? "—"} m ・ ${out.speed_ms ?? "—"} m/s，這一段通過`}
                      </div>
                    );
                  })()}
                </>
              );
            })()}
            {/* 未量測建物的假設高度。**它不是那些樓的高度**——放在這裡是
                因為它會改變判定結果，而使用者要看得到自己按了什麼 */}
            {(chk?.terrain_blind?.length ?? 0) > 0 && (
              <div className="rail-field">
                <div className="rail-row">
                  <span>未量測建物假設高度</span>
                  {assume == null ? (
                    <button className="btn-sm"
                      onClick={() => setAssume(defAssume)}>套用 {defAssume} m</button>
                  ) : (
                    <input className="numin" type="number" step={1} min={0} max={80}
                      value={assume}
                      onChange={(e) => setAssume(Number(e.target.value))} />
                  )}
                </div>
                {assume != null && (
                  <>
                    <input type="range" min={0} max={80} step={1} value={assume}
                      onChange={(e) => setAssume(Number(e.target.value))} />
                    <div className="hint-line">
                      {emph(`${chk!.terrain_blind!.length} 棟沒量過，都當成 ${assume} m 算——**這是規劃時的旋鈕，不是它們的高度**，實測要等光達`)}
                    </div>
                    <button className="btn-sm" onClick={() => setAssume(null)}>
                      改回不假設（擋下）
                    </button>
                  </>
                )}
                {assume == null && (
                  <div className="hint-line">
                    {emph(`${chk!.terrain_blind!.length} 棟沒量過——**現在是擋下**，不是通過`)}
                  </div>
                )}
              </div>
            )}

            {/* **另存新檔仍然是主要動作**（橘色那顆）：飛過的那一份是紀錄，
                改它等於改歷史。2026-09-09 使用者要加回「儲存到這一份」，
                所以把後果做成明的——按下去會先問一次，訊息寫清楚它會蓋掉
                航點並讓那一份的人工審查失效（簽核綁在 waypoints_hash 上）。 */}
            <div className="rail-save">
              <div className="hint-line">
                {isNew
                  ? `已放 ${pts.length} 個點——${busy ? "試算中…" : "還沒存"}`
                  : Object.keys(ov).length
                    ? `已改 ${Object.keys(ov).length} 個航點——${busy ? "試算中…" : "只在畫面上，還沒存"}`
                    : "改數字就會試算；原本這份不會被動到"}
              </div>
              {!isNew && Object.keys(ov).length > 0 && (
                <>
                  <button className="btn-plain btn-sm" disabled={busy}
                    onClick={() => { setOv({}); }}>捨棄改動</button>
                  <button className="btn-plain btn-sm" disabled={busy}
                    onClick={() => setOverwrite(true)}>儲存到這一份</button>
                </>
              )}
              {/* **沒有結尾就不給存**（使用者 2026-09-23）。警告就在按鈕上面，
                  而且把兩條出路放在手邊：標一個降落點，或回起飛點降落 */}
              {noEnding && (
                <div className="cmd-ready lock">
                  {emph("這條航線**沒有結尾**：沒有標降落點，也沒有選「回起飛點降落」。")}
                  <div className="rail-row" style={{ marginTop: 6 }}>
                    <button className="btn-plain btn-sm"
                      onClick={() => setPol((q) => ({ ...q, land_at_home: true }))}>
                      回起飛點降落</button>
                    <span className="hint-line">或在右欄把某個點改成「降落點」</span>
                  </div>
                </div>
              )}
              <button className="btn-accent btn-sm"
                disabled={busy || noEnding
                  || (isNew ? pts.length < 1 : !Object.keys(ov).length)}
                onClick={() => setNaming(isNew
                  ? `新航線 ${new Date().toISOString().slice(5, 16).replace("T", " ")}`
                  : `${name}（調整）`)}>另存新檔</button>
              {saved && saved !== id && (
                <div className="hint-line">
                  已另存 · <a href={`/plans/${saved}/plan`}>打開新的那一份</a>
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
      {/* **「系統替你決定了 N 件事」那張表拿掉了**（使用者 2026-09-23：「這解釋多餘了」）。
          它列的是起飛項、改速度項的位置、進場點、降落方式——每次重算都原樣再說一次，
          佔掉地圖下方一整塊。`decisions` 仍由後端回、仍存在狀態裡，要回復只要把表接回來 */}

      {blds.length > 0 && (
        <details className="plan-decisions" open>
          <summary>航線 30 m 內的建物 {blds.length} 棟</summary>
          <table className="plan-legs">
            <thead><tr>
              <th>建物</th><th>離航線</th><th>長</th><th>寬</th><th>高</th>
              <th>高度來源</th><th>佔地</th>
            </tr></thead>
            <tbody>
              {blds.map((b) => (
                <tr key={b.id}>
                  <td>{b.name ?? b.id}<span className="muted"> · {b.kind}</span></td>
                  <td>{b.dist_m} m</td>
                  <td>{b.length_m} m</td>
                  <td>{b.width_m} m</td>
                  {/* **長寬跟高不是同一種東西。** 輪廓量得到，高度多半沒有
                      ——所以高度那一欄要嘛是數字加來源，要嘛就寫「沒量過」 */}
                  <td className={b.known ? "" : "bad"}>
                    {b.height_m != null ? `${b.height_m} m`
                      : assume != null ? `假設 ${assume} m` : "沒量過"}
                  </td>
                  <td className="muted">
                    {b.height_source === "osm:height" ? "OSM 實填"
                      : b.height_source === "osm:levels" ? "樓層數 × 3.5 m 推算"
                      : assume != null ? "右欄的假設高度旋鈕" : "—"}
                  </td>
                  <td className="muted">{b.area_m2} m²</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="hint-line">
            {emph("長寬是**輪廓的最小面積外接矩形**（OSM 足跡，公尺級，量出來的）。高度那一欄不是——實測要等光達。範圍跟著航線走，改線就重算。")}
          </div>
        </details>
      )}

      {overwrite && (
        <div className="mask" onClick={() => setOverwrite(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>儲存到「{name}」？</h3>
            <div className="hint-line">
              {emph("**這會蓋掉原本的航點**，而且讓這一份的人工審查失效——簽核是綁在航點上的，航點一換它就不算數，上傳會被擋下，要重新審查一次。\n飛過的那一份是紀錄；如果你想留著它，用「另存新檔」。")}
            </div>
            <div className="modal-row">
              <button className="btn-plain btn-sm"
                onClick={() => setOverwrite(false)}>取消</button>
              <button className="btn-accent btn-sm" disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const r = await fetch(`${API}/api/plans/${id}/preview`, {
                      method: "POST", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        overrides: Object.entries(ov).map(([seq, v]) =>
                          ({ seq: Number(seq), ...v })),
                        wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
                        rtl_alt_m: spdRef.current.rtl, assume_m: assume,
                        fence: fenceBody(), save: true }),
                    });
                    const d = await r.json();
                    if (r.ok) {
                      setOv({}); setChk(d.check); setProf(d.profile);
                      setSign(await getJson<Sign>(`${API}/api/plans/${id}/sign`)
                        .catch(() => null as unknown as Sign));
                    } else {
                      setErr(errText(d.detail, "存檔失敗"));
                    }
                  } finally { setBusy(false); setOverwrite(false); }
                }}>蓋掉並儲存</button>
            </div>
          </div>
        </div>
      )}

      {naming !== null && (
        <div className="mask" onClick={() => setNaming(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>另存新檔</h3>
            <div className="hint-line">原本那份不會被動到。</div>
            <input value={naming} autoFocus
              onChange={(e) => setNaming(e.target.value)} />
            <div className="modal-row">
              <button className="btn-plain btn-sm"
                onClick={() => setNaming(null)}>取消</button>
              <button className="btn-accent btn-sm" disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const url = isNew ? `${API}/api/plans/draft`
                      : `${API}/api/plans/${id}/preview`;
                    const body = isNew
                      ? { home: [Number(home.lat), Number(home.lon)], points: pts,
                          policy: pol,
                          wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
                          rtl_alt_m: spdRef.current.rtl, fence: fenceBody(),
                          save_as: naming }
                      : { overrides: Object.entries(ov).map(([seq, v]) =>
                            ({ seq: Number(seq), ...v })),
                          wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
                          rtl_alt_m: spdRef.current.rtl, fence: fenceBody(),
                          save_as: naming };
                    const r = await fetch(url, { method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify(body) });
                    const d = await r.json();
                    if (r.ok && d.saved_id) {
                      setSaved(d.saved_id); setOv({}); setNaming(null);
                    }
                  } finally { setBusy(false); }
                }}>存檔</button>
            </div>
          </div>
        </div>
      )}

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
                <td className={l.agl_m != null && l.agl_m < lowAlt ? "bad" : undefined}>
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
