"use client";
/** 資訊頁 · 事件歷史（跨架次）。
 *
 * **為什麼不能只有「這一趟的事件」**：實測資料庫裡兩萬則事件，掛得上架次的
 * 不到一百則——大部分事情發生在還沒解鎖的時候（預檢擋下、遙控器離線、
 * sysid 撞號、代理上線）。那些正是事後最想回頭查的東西，而它們不屬於
 * 任何一趟飛行。所以事件有自己的分頁，篩選與往回翻都在這裡。
 *
 * 三條規矩：
 *  1. **型別下拉照資料庫實際有的列**（`/api/event-types`），不寫死清單——
 *     韌體與後端會一直長出新型別，硬編一份等於每加一種就多一個查不到的死角。
 *  2. **往回翻用游標不用 offset**：事件持續在寫，offset 分頁會在新事件進來時
 *     把同一則推到下一頁（或整則跳過）。
 *  3. **取得失敗不得長得像「沒有事件」**（lib/fetchJson.ts）。
 */
import { Fragment, useCallback, useEffect, useState } from "react";

import EventModal from "@/components/EventModal";
import {
  type DroneRow, type EventRow, dayShort, hms, SEV_COLOR,
} from "@/components/InfoShared";
import InfoTip from "@/components/InfoTip";
import { emph } from "@/lib/emph";
import { evText } from "@/lib/evtext";
import { errText, getJson } from "@/lib/fetchJson";
import { asGroups, EvDensity, foldEvents, foldTitle } from "@/lib/foldEvents";
import { eventDetail } from "@/lib/jsonb";
import { normSev } from "@/lib/severity";
import { API } from "@/lib/signal";

const PAGE = 120;

interface TypeRow { type: string; source: string; n: number; last_seen: string }

export default function InfoEvents({ drones }: { drones: DroneRow[] }) {
  const [rows, setRows] = useState<EventRow[] | null>(null);
  const [types, setTypes] = useState<TypeRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);          // 還有更早的可以載
  const [openEv, setOpenEv] =
    useState<(EventRow & { timeFirst?: string; times?: number[] }) | null>(null);
  // 折疊（lib/foldEvents.tsx）。**這一頁最需要它**：資料庫裡兩萬則事件，
  // 其中一萬四千則是四句 PreArm 嘮叨的重複——不折的話往回翻永遠翻不到
  // 真正發生過的那幾件事
  const [foldOn, setFoldOn] = useState(true);

  // 篩選。**空字串＝不篩**，不用 null——select 的值本來就是字串，
  // 兩種「沒選」會在比較時分岔
  const [drone, setDrone] = useState("");
  const [sev, setSev] = useState("");
  const [src, setSrc] = useState("");
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const [qLive, setQLive] = useState("");           // 輸入中的值（debounce 前）

  useEffect(() => {
    const t = setTimeout(() => setQ(qLive), 350);
    return () => clearTimeout(t);
  }, [qLive]);

  const query = useCallback((beforeId?: number) => {
    const p = new URLSearchParams({ limit: String(PAGE) });
    if (drone) p.set("drone_id", drone);
    if (sev) p.set("severity", sev);
    if (src) p.set("source", src);
    if (type) p.set("type", type);
    if (q.trim()) p.set("q", q.trim());
    if (beforeId != null) p.set("before_id", String(beforeId));
    return `${API}/api/events?${p}`;
  }, [drone, sev, src, type, q]);

  // 篩選一動就從頭載
  useEffect(() => {
    let stop = false;
    setLoading(true); setErr(null);
    getJson<EventRow[]>(query())
      .then((r) => {
        if (stop) return;
        setRows(r); setMore(r.length === PAGE); setLoading(false);
      })
      .catch((e) => {
        if (stop) return;
        setErr(errText((e as Error).message, "無法取得事件"));
        setRows(null); setLoading(false);
      });
    return () => { stop = true; };
  }, [query]);

  useEffect(() => {
    getJson<TypeRow[]>(`${API}/api/event-types?days=3650`)
      .then(setTypes)
      .catch(() => setTypes([]));   // 下拉少一個是不便，不是錯誤——不擋整頁
  }, []);

  const loadMore = async () => {
    const last = rows?.[rows.length - 1];
    if (!last) return;
    setLoading(true);
    try {
      const r = await getJson<EventRow[]>(query(last.id));
      setRows([...(rows ?? []), ...r]);
      setMore(r.length === PAGE);
    } catch (e) {
      setErr(errText((e as Error).message, "無法載入更早的事件"));
    }
    setLoading(false);
  };

  // 逐列解析 detail：**一筆壞掉不得讓整份清單消失**（lib/jsonb.ts）
  const parsed = (rows ?? []).map((e) => ({ ...e, detail: eventDetail(e.detail) }));
  const groups = foldOn ? foldEvents(parsed) : asGroups(parsed);

  const droneName = (id: string | null) =>
    drones.find((d) => d.id === id)?.name ?? null;
  const filtered = !!(drone || sev || src || type || q.trim());

  return (
    <>
      {/* 篩選是一列控制，不是一張需要標題的卡（ui-spec §6c.7） */}
      <div className="card">
        <div className="info-filters">
          <span className="hint-line">嚴重度</span>
          <div className="sess-pills">
            {([["", "全部"], ["critical", "危急"], ["warning", "警告"],
               ["info", "資訊"]] as const).map(([v, l]) => (
              <button key={v} className={`pill${sev === v ? " on" : ""}`}
                onClick={() => setSev(v)}>{l}</button>
            ))}
          </div>
          <span className="hint-line">來源</span>
          <div className="sess-pills">
            {([["", "全部"], ["vehicle", "機上"], ["agent", "代理"], ["system", "系統"]] as const)
              .map(([v, l]) => (
              <button key={v} className={`pill${src === v ? " on" : ""}`}
                onClick={() => setSrc(v)}>{l}</button>
            ))}
          </div>
          <label>無人機
            <select value={drone} onChange={(e) => setDrone(e.target.value)}>
              <option value="">全部</option>
              {drones.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </label>
          <label>型別
            <select value={type} onChange={(e) => setType(e.target.value)}>
              {/* 照資料庫實際有的列（見檔頭規矩 1）；括號是這種事件出現過幾次 */}
              <option value="">全部（{types.length} 種）</option>
              {types.map((t) => (
                <option key={`${t.type}:${t.source}`} value={t.type}>
                  {t.type}（{t.n}）
                </option>
              ))}
            </select>
          </label>
          <label className="info-search">搜尋
            <input value={qLive} placeholder="型別或內容⋯"
              onChange={(e) => setQLive(e.target.value)} />
          </label>
          {filtered && (
            <button className="btn-plain btn-sm" onClick={() => {
              setDrone(""); setSev(""); setSrc(""); setType(""); setQ(""); setQLive("");
            }}>清除篩選</button>
          )}
        </div>
      </div>

      <div className="card">
        <h3>事件
          <span className="h3-note">
            {rows ? `已載入 ${rows.length} 則${more ? "（還有更早的）" : ""}` : ""}
          </span>
          <InfoTip tip="同一句話重複出現時折成一列：×N 是總次數，右邊的細條是每一次發生的時刻，滑過去看起訖與間隔。點列看逐則與原始 detail。「折疊」關掉即回到一則一列。" />
          <span className="ev-filter">
            <button className={foldOn ? "on" : ""}
              title={foldOn ? "同一句話折成一列（點擊看未折疊的原樣）"
                : "一則一列（點擊折疊重複）"}
              onClick={() => setFoldOn(!foldOn)}>折疊</button>
          </span>
        </h3>
        {err && <div className="form-err">{err}</div>}
        {!err && rows === null && <div className="empty">載入中…</div>}
        {!err && rows?.length === 0 && (
          <div className="empty">
            {filtered ? "這組篩選沒有符合的事件——放寬條件再試。" : "資料庫裡還沒有任何事件。"}
          </div>
        )}
        {!!rows?.length && (
          <div className="info-evlist">
            {groups.map((g, gi) => {
              const e = g.latest;
              // **日期做群組標頭**：逐列重複「2026/9/8」的話，真正的內容
              // 會被擠到右邊（ui-spec §6c.7）
              const dayKey = dayShort(e.time).slice(0, 5);
              const newDay = gi === 0
                || dayShort(groups[gi - 1].latest.time).slice(0, 5) !== dayKey;
              const d = e.detail;
              const dn = droneName(e.drone_id);
              // **`warn` 也是警告**（lib/severity.ts）：舊資料裡有 292 則
              const sv = normSev(e.severity);
              return (
                <Fragment key={g.key}>
                {newDay && <div className="info-day">{dayKey}</div>}
                <button className="info-evrow"
                  title={g.count > 1 ? foldTitle(g) : "點擊看完整內容"}
                  onClick={() => setOpenEv({
                    ...e,
                    // 折疊在列表這一層做，modal 也要拿得到同一個次數
                    detail: g.count > 1 ? { ...d, count: g.count } : d,
                    ...(g.count > 1 ? { timeFirst: g.first, times: g.times } : {}),
                  })}>
                  <span className="dot" style={{ background: SEV_COLOR[sv] }} />
                  <time>{hms(e.time)}</time>
                  {/* 已經篩成一種來源／一台機時，那一格就不再逐列重複
                      （空的 auto 欄會自己收掉，不影響其餘欄的對齊） */}
                  <span className="info-evsrc">
                    {/* agent＝機上代理（057）。**不能併進「系統」**：那是地面站
                        推導的，而代理的事是地面站不在時機上自己看到的 */}
                    {src ? "" : (e.source === "vehicle" ? "機上"
                      : e.source === "agent" ? "代理" : "系統")}
                  </span>
                  {/* 機名沒有就留白——**不要寫「未知機」**：多數系統事件本來
                      就不屬於任何一台機，替它掛一個「未知」是無中生有 */}
                  <span className="info-evdrone">{drone ? "" : (dn ?? "")}</span>
                  <span className="info-evtext">
                    {emph(evText({ type: e.type, detail: d,
                      severity: e.severity as "info" | "warning" | "critical" }))}
                  </span>
                  {/* 折疊徽章那一格：**空的時候也要在**（見 .info-evrow 註解） */}
                  <span className="info-evagg">
                    {g.count > 1 && <span className="ev-count">×{g.count}</span>}
                    {g.count > 1 && <EvDensity times={g.times} color={SEV_COLOR[sv]} />}
                  </span>
                  {e.session_id && <span className="info-evflag" title="這則事件屬於某一趟飛行">飛行中</span>}
                </button>
                </Fragment>
              );
            })}
          </div>
        )}
        {more && !err && (
          <div className="info-more">
            <button className="btn-plain btn-sm" disabled={loading} onClick={loadMore}>
              {loading ? "載入中…" : "載入更早的"}
            </button>
          </div>
        )}
        {rows && !more && rows.length > 0 && (
          <div className="hint-line info-more">已經是最早的一則了。</div>
        )}
      </div>

      {openEv && (
        <EventModal onClose={() => setOpenEv(null)}
          ev={{ id: openEv.id, time: openEv.time, type: openEv.type,
            severity: openEv.severity, detail: openEv.detail,
            source: openEv.source, timeFirst: openEv.timeFirst,
            times: openEv.times, drone: droneName(openEv.drone_id) }} />
      )}
    </>
  );
}
