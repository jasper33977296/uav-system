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
import { useEffect, useRef, useState } from "react";

import TerrainStage, { type StageHit, type StageTip, type StageWp }
  from "@/components/TerrainStage";
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
  plan: number | null; agl: number | null; seq: number | null }
interface Profile { points: Pt[]; home_amsl_m: number | null; frames: number[] }
interface Leg {
  from: number; to: number; length_m: number; agl_m: number | null;
  speed_ms: number | null; speed_src: string; turn_deg?: number | null;
  /** **判定由後端給**（`plan_check.leg_profile`）。前端不要照門檻自己再判
   *  一次——改了後端的常數，畫面不會跟著變，而且看起來完全正常 */
  low_fast?: boolean;
}
interface Limits { low_alt_m: number; low_speed_ms: number; min_takeoff_alt_m: number;
  /** 未量測建物假設高度的**預設值**（後端給，前端不要自己抄一份） */
  assumed_default_m?: number }
interface Check {
  ok: boolean; problems: string[]; warnings: string[]; legs?: Leg[];
  limits?: Limits; terrain?: { home_amsl_m?: number };
  /** 這份判定是用哪個假設高度算的；null ＝沒有假設，未量測的樓直接擋下 */
  assumed_m?: number | null;
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
function Profile({ p }: { p: Profile }) {
  const pts = p.points.filter((x) => x.ground != null);
  if (pts.length < 2) {
    return <div className="empty">{emph(
      "沒有地形資料，畫不出剖面——**這不代表航線沒問題**，是這一段沒被檢查過")}</div>;
  }
  const W = 900, H = 260, PAD_L = 46, PAD_R = 12, PAD_T = 14, PAD_B = 26;
  const dMax = Math.max(...pts.map((x) => x.d), 1);
  const surf = (x: Pt) => x.top ?? x.ground!;
  const vals = pts.flatMap((x) => [x.ground!, surf(x), x.plan ?? x.ground!]);
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
  /** **從零產生**（使用者 2026-09-08：兩個入口都要）。`/plans/new/plan`。 */
  const isNew = id === "new";
  const [home, setHome] = useState({ lat: "24.773449", lon: "121.045864" });
  const [tkAlt, setTkAlt] = useState(1.5);
  const [drawSpd, setDrawSpd] = useState(1.0);
  const [pts, setPts] = useState<
    { lat: number; lon: number; alt: number; kind: string }[]>([]);
  const [started, setStarted] = useState(false);
  const [placeKind, setPlaceKind] = useState("wp");
  const [landHome, setLandHome] = useState(true);
  const [landMode, setLandMode] = useState("vert");
  const [naming, setNaming] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [prof, setProf] = useState<Profile | null>(null);
  // 未量測建物的假設高度。**它是規劃時的旋鈕，不是那些樓的高度**——
  // 真正的答案要等光達實測，所以每一份用到它的判定都帶著出處出去。
  // null ＝不假設：那時未量測的樓是擋下，不是通過
  const [assume, setAssume] = useState<number | null>(null);
  const assumeRef = useRef<number | null>(null);
  assumeRef.current = assume;
  const [chk, setChk] = useState<Check | null>(null);
  const [spd, setSpd] = useState<{ wp: number | null; rad: number | null; src: string }>(
    { wp: null, rad: null, src: "還沒讀過這台機" });
  const [err, setErr] = useState<string | null>(null);
  const [selWp, setSelWp] = useState(0);
  /** **改動只存在畫面上**（使用者裁定 2026-09-08：先只算不存）。
   *  每次變動送去後端試算——規則只有一份，前端不自己再算一次。 */
  const [ov, setOv] = useState<Record<number,
    { alt?: number; speed?: number; lat?: number; lon?: number }>>({});
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const spdRef = useRef<{ wp: number | null; rad: number | null }>({ wp: null, rad: null });

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
            `${COMMAND_API}/api/command/${sid}/params?names=WP_SPD,WP_RADIUS_M`);
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
        // 開頁時**先不假設**：第一眼看到的是「這幾棟沒量過」，
        // 而不是一份用假設值算出來的「通過」
        const [ms, pr, ck] = await Promise.all([
          getJson<{ name: string }[]>(`${API}/api/plans`),
          getJson<Profile>(`${API}/api/plans/${id}/profile`),
          getJson<Check>(`${API}/api/plans/${id}/check${q}`),
        ]);
        if (stop) return;
        setName((ms as any).find?.((m: any) => m.id === id)?.name ?? id);
        setProf(pr); setChk(ck); setSpd({ wp, rad, src });
        spdRef.current = { wp, rad };
      } catch (e) {
        if (!stop) setErr(errText((e as Error).message, "讀不到這份航線"));
      }
    })();
    return () => { stop = true; };
  }, [id, isNew]);

  // 從零模式：點一變就重算（同樣去抖、同樣不寫資料庫）
  useEffect(() => {
    if (!isNew || !started) return;
    const t = setTimeout(async () => {
      setBusy(true);
      try {
        const r = await fetch(`${API}/api/plans/draft`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            home: [Number(home.lat), Number(home.lon)], points: pts,
            takeoff_alt: tkAlt, speed: drawSpd, land_at_home: landHome,
            land_mode: landMode,
            wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
            assume_m: assumeRef.current }),
        });
        const d = await r.json();
        if (r.ok) { setChk(d.check); setProf(d.profile); }
      } finally { setBusy(false); }
    }, 220);
    return () => clearTimeout(t);
  }, [isNew, started, pts, tkAlt, drawSpd, home.lat, home.lon, landHome, landMode,
      assume]);

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
        if (list.length) {
          const r = await fetch(`${API}/api/plans/${id}/preview`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ overrides: list, wp_spd: sp,
                                   wp_radius: spdRef.current.rad, assume_m: assume }),
          });
          const d = await r.json();
          if (r.ok) { setChk(d.check); setProf(d.profile); }
        } else {
          const [pr, ck] = await Promise.all([
            getJson<Profile>(`${API}/api/plans/${id}/profile${
              assume == null ? "" : `?assume_m=${assume}`}`),
            getJson<Check>(`${API}/api/plans/${id}/check${q}`),
          ]);
          setProf(pr); setChk(ck);
        }
      } catch { /* 讀不到就維持上一份，畫面不要空掉 */ } finally { setBusy(false); }
    }, 260);
    return () => clearTimeout(t);
  }, [assume, id, isNew, ov]);

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
                                 assume_m: assumeRef.current }),
        });
        const d = await r.json();
        if (r.ok) { setChk(d.check); setProf(d.profile); }
      } finally { setBusy(false); }
    }, 260);
    return () => clearTimeout(t);
  }, [ov, id, isNew]);

  const defAssume = chk?.limits?.assumed_default_m ?? 9;
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

  const stageWps: StageWp[] = (prof?.points ?? [])
    .filter((p) => p.seq != null && p.plan != null)
    .map((p) => ({ seq: p.seq as number, lat: p.lat ?? 0, lon: p.lon ?? 0,
      amsl: p.plan as number, ground: p.ground,
      bad: badSeq.has(p.seq as number), fixed: p.seq === 0 }))
    .map((w) => {
      const o = ov[w.seq];
      return o?.lat != null ? { ...w, lat: o.lat, lon: o.lon as number } : w;
    });
  const worst = legs.reduce<number | null>(
    (m, l) => (l.agl_m == null ? m : m == null || l.agl_m < m ? l.agl_m : m), null);

  return (
    <div className="page">
      <div className="plan-head">
        <Link href="/plans" className="btn-plain btn-sm">← 路徑管理</Link>
        <h1 className="mtitle">{name || "…"}</h1>
      </div>

      {err && <div className="form-err">{err}</div>}

      <div className="plan-facts">
        <span className="chip">{prof ? frameLabel(prof.frames) : "…"}</span>
        {prof?.home_amsl_m != null && (
          <span className="chip">起飛點 {prof.home_amsl_m} m（海拔）</span>
        )}
        {worst != null && (
          <span className={`chip${worst < 0 ? " bad" : ""}`}>最低離地 {worst} m</span>
        )}
        <span className="chip" style={spd.wp == null ? { opacity: 0.6 } : undefined}
          title={spd.wp == null
            ? "航線裡的 DO_CHANGE_SPEED 只從它被執行到的那一項之後才生效；在那之前用的是機上的 WP_SPD。讀不到它，速度相關的判定就不做——讀不到不等於沒問題"
            : "第一段永遠用這個值：航線裡的 DO_CHANGE_SPEED 管不到起飛之後那一段"}>
          {spd.wp == null ? "機上速度未讀到" : `機上 WP_SPD ${spd.wp} m/s`} · {spd.src}
        </span>
      </div>

      {/* 3D 地形（issues/048 F1）。**地形是真的**：maplibre 吃我們自己從
          `.hgt` 產的圖磚。原型那張手繪線框到此為止 */}
      {isNew && (
        <div className="newform">
          <label className="f"><span>起飛點緯度</span>
            <input value={home.lat} disabled={started}
              onChange={(e) => setHome((h) => ({ ...h, lat: e.target.value }))} /></label>
          <label className="f"><span>起飛點經度</span>
            <input value={home.lon} disabled={started}
              onChange={(e) => setHome((h) => ({ ...h, lon: e.target.value }))} /></label>
          <label className="f"><span>起飛高度</span>
            <input type="number" step="0.5" value={tkAlt} disabled={started}
              onChange={(e) => setTkAlt(Number(e.target.value))} /></label>
          <label className="f"><span>速度 m/s</span>
            <input type="number" step="0.1" value={drawSpd} disabled={started}
              onChange={(e) => setDrawSpd(Number(e.target.value))} /></label>
          {started && (
            <>
              <div className="f"><span>放點類型</span>
                <div className="seg2">
                  {[["wp", "航點"], ["land", "降落點"]].map(([k, t]) => (
                    <button key={k} aria-pressed={placeKind === k}
                      onClick={() => setPlaceKind(k)}>{t}</button>
                  ))}
                </div>
              </div>
              <div className="f"><span>降落在哪裡</span>
                <label className="opt"><input type="radio" checked={landHome}
                  onChange={() => setLandHome(true)} />起飛點</label>
                <label className="opt"><input type="radio" checked={!landHome}
                  onChange={() => setLandHome(false)} />標成降落點的位置</label>
              </div>
              <div className="f"><span>降落方式</span>
                <label className="opt"><input type="radio" checked={landMode === "vert"}
                  onChange={() => setLandMode("vert")} />飛到定點再垂直降落</label>
                <label className="opt"><input type="radio" checked={landMode === "glide"}
                  onChange={() => setLandMode("glide")} />逐漸降落</label>
              </div>
            </>
          )}
          {!started
            ? <button className="btn-accent btn-sm" onClick={() => setStarted(true)}>
                從這裡開始放點</button>
            : <span className="hint-line">
                點地形放下一個航點（{pts.length} 個）・拖曳轉視角
                {pts.length > 0 && <>　<button className="btn-plain btn-sm"
                  onClick={() => setPts((p) => p.slice(0, -1))}>移除上一個</button></>}
              </span>}
        </div>
      )}
      {(stageWps.length > 1 || (isNew && started)) && (
        <div className="plan-work">
          <TerrainStage wps={stageWps} sel={selWp} onSelect={setSelWp}
            assumeM={assume}
            tipFor={tipFor}
            placing={isNew && started}
            center={isNew ? [Number(home.lon), Number(home.lat)] : undefined}
            onPlace={(l) => setPts((p) => {
              setSelWp(p.length + 1);
              return [...p, { lat: l.lat, lon: l.lng, alt: tkAlt, kind: placeKind }];
            })}
            onMove={(i, l) => {
              if (isNew) {
                const k = i - 1;
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
          <aside className="plan-rail">
            <h2>選取的航點</h2>
            {(() => {
              const w = stageWps[selWp];
              if (!w) return <div className="hint-line">在 3D 上點一個航點</div>;
              const out = legs.find((l) => l.from === w.seq);   // 從它出發的那一段
              const cur = ov[w.seq] ?? {};
              const alt = cur.alt ?? Math.round((w.amsl - (prof?.home_amsl_m ?? 0)) * 10) / 10;
              const spdNow = cur.speed ?? out?.speed_ms ?? null;
              const set = (k: "alt" | "speed", v: number) => {
                if (isNew) {
                  if (k === "alt" && selWp > 0)
                    setPts((p) => p.map((q, j) => j === selWp - 1 ? { ...q, alt: v } : q));
                  if (k === "alt" && selWp === 0) setTkAlt(v);
                  if (k === "speed") setDrawSpd(v);
                  return;
                }
                setOv((o) => ({ ...o, [w.seq]: { ...o[w.seq], [k]: v } }));
              };
              return (
                <>
                  <div className="rail-row"><span>航點</span>
                    <b className="num">seq {w.seq}</b></div>
                  {isNew && selWp > 0 && (
                    <>
                      <div className="seg2">
                        {[["wp", "航點"], ["land", "降落點"]].map(([k, t]) => (
                          <button key={k}
                            aria-pressed={(pts[selWp - 1]?.kind ?? "wp") === k}
                            onClick={() => setPts((p) => p.map((q, j) =>
                              j === selWp - 1 ? { ...q, kind: k } : q))}>{t}</button>
                        ))}
                      </div>
                      <button className="btn-plain btn-sm"
                        onClick={() => { setPts((p) =>
                          p.filter((_, j) => j !== selWp - 1)); setSelWp(0); }}>
                        刪除這個點</button>
                      <div className="hint-line">3D 上拖曳航點只移動位置；
                        高度用下面的滑桿或數字。</div>
                    </>
                  )}
                  <label className="rail-field">
                    <div className="rail-row"><span>高度（離起飛點）</span>
                      <input className="numin" type="number" step={0.1} value={alt}
                        onChange={(e) => set("alt", Number(e.target.value))} /></div>
                    <input type="range" min={0} max={30} step={0.1} value={alt}
                      onChange={(e) => set("alt", Number(e.target.value))} />
                  </label>
                  {out && (
                    <label className="rail-field">
                      <div className="rail-row"><span>下一段速度</span>
                        <input className="numin" type="number" step={0.1}
                          value={spdNow ?? 1}
                          onChange={(e) => set("speed", Number(e.target.value))} /></div>
                      <input type="range" min={0.2} max={8} step={0.1}
                        value={spdNow ?? 1}
                        onChange={(e) => set("speed", Number(e.target.value))} />
                    </label>
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

            {/* **改動不會動到原本那份**（使用者裁定）：按了才另存 */}
            <div className="rail-save">
              <div className="hint-line">
                {isNew
                  ? `已放 ${pts.length} 個點——${busy ? "試算中…" : "還沒存"}`
                  : Object.keys(ov).length
                    ? `已改 ${Object.keys(ov).length} 個航點——${busy ? "試算中…" : "只在畫面上，還沒存"}`
                    : "拖滑桿試算；原本這份不會被動到"}
              </div>
              <button className="btn-accent btn-sm"
                disabled={busy || (isNew ? pts.length < 1 : !Object.keys(ov).length)}
                onClick={() => setNaming(isNew
                  ? `新航線 ${new Date().toISOString().slice(5, 16).replace("T", " ")}`
                  : `${name}（調整）`)}>另存新檔</button>
              {saved && (
                <div className="hint-line">
                  已另存 · <a href={`/plans/${saved}/plan`}>打開新的那一份</a>
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
      {prof && <Profile p={prof} />}
      <div className="hint-line">
        {emph("地面線來自 SRTM（水平約 30 m）——**被格子抹平的表面**：樹冠與屋頂混在裡面，但沒有一棟樓是它畫得出來的。建物是另一份（OSM 輪廓），三種畫法對應三種出處：實心灰塊標「樓層數推算」是**樓層數 × 3.5 m 猜的**，不是量的；虛線橘塊標「假設 N m」用的是右欄那個旋鈕，**改它判定就會變**；沒有頂的橘色柱子代表現在不假設，那棟樓的高度沒有人量過。三種都不是實測——**實測要等光達**。輪廓只取外環，**中庭當成實心**（多禁不會少禁）。")}
      </div>

      {(chk?.problems?.length || chk?.warnings?.length) ? (
        <div className="plan-findings">
          {/* 後端文案用 `**` 當強調記號，而畫面不解析 Markdown（ui-spec §0.3c）*/}
          {chk.problems.map((p, i) => <div key={i} className="form-err">✕ {emph(p)}</div>)}
          {chk.warnings.map((w, i) => <div key={i} className="hint-line">⚠ {emph(w)}</div>)}
        </div>
      ) : chk ? <div className="hint-line">這份航線沒有發現。</div> : null}

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
                          takeoff_alt: tkAlt, speed: drawSpd,
                          land_at_home: landHome, land_mode: landMode,
                          wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
                          save_as: naming }
                      : { overrides: Object.entries(ov).map(([seq, v]) =>
                            ({ seq: Number(seq), ...v })),
                          wp_spd: spdRef.current.wp, wp_radius: spdRef.current.rad,
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
