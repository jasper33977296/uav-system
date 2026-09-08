"use client";
import { useCallback, useEffect, useState } from "react";

import CoverageCard, { type Coverage } from "@/components/InfoCoverage";
import InfoTip from "@/components/InfoTip";
import LogIndexSheet from "@/components/LogIndexSheet";
import { errText, getJson } from "@/lib/fetchJson";
import { API } from "@/lib/signal";
import { AgentState, RecordUpload, useUavStore } from "@/lib/store";

/* 資訊頁 · 錄製與回傳（issues/014；2026-09-07 從 app/captures/page.tsx 拆成
 * 元件，成為資訊頁的第三個分頁——內容與規則原樣不動）。
 *
 * 這一段回答「**這趟飛的資料，我拿得到嗎**」，順序就是回答的順序：
 * ① 這一趟兩層各蓋到哪 → ② 機上還有沒有東西沒回來 → ③ 把檔案拿走。
 *
 * 四條誠實規則貫穿全段：
 *   1. **「沒有東西要傳」與「傳不動」不同形**——兩者都是「沒在傳」，
 *      一個是完成、一個是故障，處置完全相反。所以永遠說得出為什麼。
 *   2. **「不知道」不畫成「沒有」**——代理失聯時整張卡降調、圓點空心。
 *   3. **兩層不合併成一張清單**——合併就把「兩者相差的那一段」抹掉了，
 *      而那是機上那份唯一不可取代的價值。
 *   4. **不可挽回的損失留在畫面上**——未回傳即被滾動刪除是永久的。
 */

interface OnboardFile {
  drone_id: string; drone_name: string | null;
  name: string; onboard_name: string;
  bytes: number; expected_bytes: number;
  status: "complete" | "partial" | "lost";
  sha256: string | null;
  covers: { from: number; to: number; frames: number } | null;
  received: string | null; lost_at: string | null;
  url: string | null;
}
interface OnboardList {
  dir: string; files: OnboardFile[]; total_bytes: number;
  keep_days: number; free_mb: number;
}
interface GroundFile { name: string; bytes: number; received: string | null; url: string }
interface GroundList { dir: string; files: GroundFile[]; total_bytes: number; keep_days: number }
interface Drone { id: string; name: string; agent?: AgentState | null }
interface Session { id: string; drone_name: string; started_at: string; ended_at: string | null }
const mb = (b: number) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);
const clock = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("zh-TW", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
const hhmm = (t: number) =>
  new Date(t * 1000).toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit", hour12: false });
const dur = (s: number) =>
  s >= 60 ? `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒` : `${Math.round(s)} 秒`;

/** 版本比大小（`x.y.z`，缺位補 0）。**認不得就當成新的**——把一個看不懂的
 * 版本字串判成「太舊」，會讓升級過頭的機被畫成不能用。 */
function older(v: string | null | undefined, than: string): boolean {
  if (!v) return false;
  const a = (v.match(/\d+/g) ?? []).map(Number);
  const b = than.split(".").map(Number);
  if (!a.length) return false;
  for (let i = 0; i < b.length; i++) {
    const x = a[i] ?? 0;
    if (x !== b[i]) return x < b[i];
  }
  return false;
}

/** 「這台機的回傳狀況」——**沒在傳的時候一定說得出為什麼**。
 *
 * 回 `rank` 供排序：需要注意的排前面，不照機隊順序。 */
type Verdict = {
  rank: number; tone: "ok" | "warn" | "serious" | "unknown";
  state: string; why: string | null; meta: string | null;
  progress: number | null; stale: boolean;
};

function verdict(ag: AgentState | null | undefined): Verdict {
  if (!ag) {
    return { rank: 3, tone: "unknown", state: "不知道", stale: true, progress: null,
      why: "沒有代理就沒有機上錄製。這台機只有地面站那一份——斷線的那幾段沒有備份。",
      meta: null };
  }
  const ver = ag.agent_version ? `代理 ${ag.agent_version}` : "代理版本不明";
  if (!ag.fresh) {
    // **不知道，不是「沒有」。** 代理失聯時它最後說的那句話已經過期了，
    // 而過期的資料絕不能看起來像現在的資料（即時頁同一條規則）
    return { rank: 3, tone: "unknown", state: "不知道", stale: true, progress: null,
      why: "代理沒有在推狀態。機上有幾份等著回傳，現在看不到。",
      meta: `${ver} · 上次 ${clock(ag.since ?? null)}` };
  }
  const u: RecordUpload | null | undefined = ag.record_upload;
  if (!u) {
    // **兩種情況，話不一樣。** 代理太舊＝它真的不會傳；夠新但沒回報＝
    // 可能只是自動回傳被關掉。**把後者說成前者是在下一個我們沒有依據的結論**
    return older(ag.agent_version, "0.6.0")
      ? { rank: 2, tone: "serious", state: "這台機不會自己回傳", stale: false,
        progress: null,
        why: "機上有幾份、還在不在，我們看不到。落地後要人 scp。",
        meta: `${ver}（自動回傳從 v0.6.0 起）` }
      : { rank: 2, tone: "serious", state: "看不到這台機的回傳狀況", stale: false,
        progress: null,
        why: "自動回傳可能被關掉了，也可能是代理還沒開始回報。",
        meta: `${ver}（回傳現況從 v0.7.0 起回報）` };
  }
  const backlog = u.pending > 0 ? `待回傳 ${u.pending} 份` : null;
  if (u.current) {
    return { rank: 0, tone: "ok", stale: false, progress: u.progress,
      state: `正在回傳 ${u.current}`, why: null,
      meta: [ver, backlog].filter(Boolean).join(" · ") };
  }
  if (u.blocked) {
    return { rank: u.pending > 0 ? 1 : 4, tone: u.pending > 0 ? "warn" : "ok",
      stale: false, progress: null,
      state: u.pending > 0 ? `沒有在傳 —— ${u.blocked}` : `沒有東西要傳（${u.blocked}）`,
      why: u.pending > 0
        ? `機上 ${u.pending} 份等著。守門一開就會自己傳。` : null,
      meta: ver };
  }
  if (u.pending > 0) {
    return { rank: 1, tone: "warn", stale: false, progress: null,
      state: `待回傳 ${u.pending} 份`, why: "守門是開的，這一輪就會送。", meta: ver };
  }
  return { rank: 4, tone: "ok", stale: false, progress: null,
    state: "已同步", why: null, meta: ver };
}

export default function InfoCaptures() {
  const agents = useUavStore((s) => s.agents);
  const [drones, setDrones] = useState<Drone[] | null>(null);
  const [onboard, setOnboard] = useState<OnboardList | null>(null);
  const [ground, setGround] = useState<GroundList | null>(null);
  const [cov, setCov] = useState<Coverage | null>(null);
  const [covSession, setCovSession] = useState<Session | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<"onboard" | "ground">("onboard");
  // **在網頁上打開一份 tlog**（使用者定案 2026-09-07：「log 只能下載來看」）。
  // 單一 sheet、新點替換——同事件詳情 modal 的慣例
  const [look, setLook] = useState<{ url: string; title: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [d, o, g] = await Promise.all([
        getJson<Drone[]>(`${API}/api/drones`),
        getJson<OnboardList>(`${API}/api/onboard-captures`),
        getJson<GroundList>(`${API}/api/captures`),
      ]);
      setDrones(d); setOnboard(o); setGround(g); setErr(null);
    } catch (e) {
      // **取得失敗不得靜默**：空清單會被讀成「沒有錄到」，而那是假話
      setErr(errText((e as Error).message, "無法取得錄製清單"));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 10_000);   // 回傳進度會動，但不必比這更快
    return () => clearInterval(t);
  }, [load]);

  // 最新一趟的兩層覆蓋。**失敗就整段不畫**——畫一半的覆蓋帶比不畫更糟
  useEffect(() => {
    (async () => {
      try {
        const ss = await getJson<Session[]>(`${API}/api/sessions?limit=1`);
        if (!ss.length) return;
        setCovSession(ss[0]);
        setCov(await getJson<Coverage>(
          `${API}/api/onboard-captures/coverage?session_id=${ss[0].id}`));
      } catch { /* 覆蓋帶是加分項，拿不到就不畫，不影響其餘 */ }
    })();
  }, []);

  const rows = (drones ?? []).map((d) => {
    const ag = agents[d.id] ?? d.agent;
    return { d, ag, v: verdict(ag) };
  }).sort((a, b) => a.v.rank - b.v.rank || a.d.name.localeCompare(b.d.name));

  const totalPending = rows.reduce(
    (n, r) => n + ((agents[r.d.id] ?? r.d.agent)?.record_upload?.pending ?? 0), 0);

  return (
    <>
      <div className="drone-head">
        <span className="name">回傳現況</span>
        <span className="spacer" />
        <div className="cap-stats">
          <div className="metric"><div className="value">{totalPending}</div>
            <div className="label">待回傳</div></div>
          <div className="metric"><div className="value">
            {onboard ? onboard.files.filter((f) => f.status === "complete").length : "—"}
          </div><div className="label">機上檔案</div></div>
          <div className="metric"><div className="value">
            {onboard ? `${(onboard.free_mb / 1000).toFixed(0)} GB` : "—"}
          </div><div className="label">地面站剩餘</div></div>
        </div>
      </div>

      {err && <div className="form-err">{err}</div>}

      {/* ① 兩層覆蓋帶（最近一趟；逐趟看在「架次」分頁） */}
      {cov && covSession && (
        <CoverageCard cov={cov} title={`最近一趟 · ${cov.drone_name}`} />
      )}

      {/* ② 每台機的回傳狀態 */}
      <div className="card">
        {/* **一台一列**（ui-spec §6c.7）：三張卡各抄一遍「沒有代理就沒有機上
            錄製…」——那句話對每一台沒有代理的機都一樣，它住 ⓘ。
            有代理的機才有各自的原因，那些照樣寫在列上。 */}
        <h3>回傳狀態<InfoTip tip={"需要注意的排前面，不照機隊順序。"
          + "「不知道」＝這台機沒有代理，我方無從得知它有沒有在機上錄——"
          + "不是「沒有錄」。沒有機上錄製的機只有地面站那一份，"
          + "斷線的那幾段沒有備份。"
          + "「沒有東西要傳」與「傳不動」都是沒在傳，但一個是完成、一個是故障。"} /></h3>
        {drones === null && !err && <div className="empty">載入中…</div>}
        {drones?.length === 0 && <div className="empty">還沒有註冊過任何無人機。</div>}
        <div className="cap-fleet-rows">
          {rows.map(({ d, ag, v }) => (
            <div key={d.id} className={`cap-arow${v.stale ? " cap-stale" : ""}`}>
              <span className={`dot cap-dot-${v.tone}`} />
              <span className="cap-dname">{d.name}</span>
              {v.progress != null && (
                <span className="cap-prog"><i
                  style={{ width: `${Math.round(v.progress * 100)}%` }} /></span>
              )}
              <span className="spacer" />
              {/* 沒有代理時的那句話是共通的（住 ⓘ）；有代理才有各自的原因 */}
              {v.why && ag && <span className="hint-line">{v.why}</span>}
              <span className="cap-dstate">{v.state}</span>
              {v.meta && <span className="hint-line">{v.meta}</span>}
              <LostBanner droneId={d.id} files={onboard?.files} />
            </div>
          ))}
        </div>
      </div>

      {/* ③ 檔案 */}
      <div className="card">
        <div className="cap-tabs" role="tablist">
          <button role="tab" aria-selected={tab === "onboard"}
            onClick={() => setTab("onboard")}>
            機上錄製{onboard ? ` · ${onboard.files.length}` : ""}
          </button>
          <button role="tab" aria-selected={tab === "ground"}
            onClick={() => setTab("ground")}>
            地面站錄製{ground ? ` · ${ground.files.length}` : ""}
          </button>
        </div>
        {tab === "onboard"
          ? <OnboardTable list={onboard} err={err} onLook={setLook} />
          : <GroundTable list={ground} err={err} onLook={setLook} />}
      </div>

      {look && (
        <LogIndexSheet url={look.url} title={look.title}
          onClose={() => setLook(null)} />
      )}
    </>
  );
}

/** 這台機有沒有「傳成功之前就被刪掉」的檔案。**那是永久的損失，要留在畫面上**。 */
function LostBanner({ droneId, files }: { droneId: string; files?: OnboardFile[] }) {
  const lost = (files ?? []).filter((f) => f.drone_id === droneId && f.status === "lost");
  if (!lost.length) return null;
  return (
    <div className="cap-lost">
      <b>{lost.length} 份沒傳回來就被機上滾動刪掉了。</b>那幾趟的機上紀錄已經沒有了。
    </div>
  );
}

const STATUS_CHIP: Record<OnboardFile["status"], { text: string; tone: string }> = {
  complete: { text: "已回傳", tone: "ok" },
  partial: { text: "回傳中", tone: "warn" },
  lost: { text: "已遺失", tone: "danger" },
};

type Look = (v: { url: string; title: string } | null) => void;

function OnboardTable({ list, err, onLook }: {
  list: OnboardList | null; err: string | null; onLook: Look;
}) {
  if (err) return <div className="form-err">{err}</div>;
  if (!list) return <div className="empty">載入中…</div>;
  if (!list.files.length) {
    return <div className="empty">
      還沒有任何機上錄製回傳。代理只在<b>地面</b>傳，而且只傳關好的檔案——
      飛過一趟再回來看。
    </div>;
  }
  return (
    <div className="cap-tablewrap">
      <table className="table">
        <thead><tr>
          <th>檔案</th><th>無人機</th><th className="num">大小</th>
          <th>狀態</th><th>涵蓋</th><th>收到</th><th />
        </tr></thead>
        <tbody>
          {list.files.map((f) => {
            const c = STATUS_CHIP[f.status];
            return (
              <tr key={`${f.drone_id}/${f.name}`} className={f.status === "lost" ? "cap-rlost" : ""}>
                <td>
                  <span className="cap-fname">{f.name}</span>
                  {f.name !== f.onboard_name && (
                    <small className="cap-sub">機上原名 {f.onboard_name} · 撞名另存</small>
                  )}
                </td>
                <td>{f.drone_name ?? "—"}</td>
                <td className="num">
                  {f.status === "lost" ? "—"
                    : f.status === "partial" ? `${mb(f.bytes)} / ${mb(f.expected_bytes)}`
                      : mb(f.bytes)}
                </td>
                <td><span className={`chip cap-chip-${c.tone}`}>
                  <span className={`dot cap-dot-${c.tone}`} />{c.text}</span></td>
                <td>{f.covers
                  ? `${hhmm(f.covers.from)} – ${hhmm(f.covers.to)}`
                  : <span className="cap-dim">不知道</span>}</td>
                <td>{f.status === "lost" ? clock(f.lost_at) : clock(f.received)}</td>
                <td className="num cap-acts">{f.url ? <>
                  {/* **看得到才叫拿得到。** 下載留著（QGC／mavlogdump 照樣讀），
                      但「這份檔裡有什麼」不該先付一次下載的代價 */}
                  <button className="btn-plain btn-sm"
                    onClick={() => onLook({ url: `${API}${f.url}/index`,
                      title: `${f.name} · ${f.drone_name ?? "—"}` })}>檢視</button>
                  <a className="btn-plain btn-sm" href={`${API}${f.url}`}>下載</a>
                </> : <span className="cap-dim">—</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function GroundTable({ list, err, onLook }: {
  list: GroundList | null; err: string | null; onLook: Look;
}) {
  if (err) return <div className="form-err">{err}</div>;
  if (!list) return <div className="empty">載入中…</div>;
  if (!list.files.length) return <div className="empty">地面站沒有在錄（檢查 CAPTURE_* 設定）。</div>;
  return (
    <div className="cap-tablewrap">
      <table className="table">
        <thead><tr><th>檔案</th><th className="num">大小</th><th>最後寫入</th><th /></tr></thead>
        <tbody>
          {list.files.map((f) => (
            <tr key={f.name}>
              <td><span className="cap-fname">{f.name}</span></td>
              <td className="num">{mb(f.bytes)}</td>
              <td>{clock(f.received)}</td>
              <td className="num cap-acts">
                <button className="btn-plain btn-sm"
                  onClick={() => onLook({ url: `${API}${f.url}/index`,
                    title: `${f.name} · 地面站錄製` })}>檢視</button>
                <a className="btn-plain btn-sm" href={`${API}${f.url}`}>下載</a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
