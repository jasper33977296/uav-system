"use client";
/** 資訊頁 · 架次紀錄（左清單／右詳情）。
 *
 * **一趟飛行是這個系統記憶的骨架**：事件、指令、錄製檔案全都掛在它下面。
 * 所以這一頁不是四張並排的表，而是「先選一趟，再看那一趟的全部」——
 * 把同一趟的四種紀錄拆到四個地方，讀的人得自己在腦子裡對時間軸，
 * 而那正是我方應該替他做的事。
 *
 * 右側四段的順序＝回想一趟飛行的順序：
 *   ① 這趟是什麼（機、任務、時長、訊號、怎麼結束的）
 *   ② 我下了什麼指令、系統擋了我幾次（command_log）
 *   ③ 過程中發生了什麼（events，時間正序）
 *   ④ 錄到的東西還在不在（兩層覆蓋＋檔案）
 *
 * 誠實規則照舊：載入中／取得失敗／真的沒有，三種話分開講；
 * 拿不到的段落**整段不畫並說明**，不畫一半。
 */
import { Fragment, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import CoverageCard, { type Coverage } from "@/components/InfoCoverage";
import {
  type CommandRow, type DroneRow, type EventRow, type SessionRow,
  dateTime, dayShort, duration, hms, num, SEV_COLOR,
} from "@/components/InfoShared";
import EventModal from "@/components/EventModal";
import InfoTip from "@/components/InfoTip";
import { emph } from "@/lib/emph";
import { evText } from "@/lib/evtext";
import { errText, getJson } from "@/lib/fetchJson";
import { asGroups, EvDensity, foldEvents, foldTitle } from "@/lib/foldEvents";
import { eventDetail, parseJsonb } from "@/lib/jsonb";
import { normSev } from "@/lib/severity";
import { API } from "@/lib/signal";

/** 指令代號 → 畫面上的說法。**照枚舉列，不猜字串**（同 CommandPanel 的
 * INTENT_LABELS）：漏一個就顯示原代號，比顯示一個猜錯的中文好。 */
const ACTION_LABELS: Record<string, string> = {
  arm: "解鎖", disarm: "上鎖", takeoff: "起飛",
  mission_upload: "上傳任務", mission_fly: "起飛→任務", mission_start: "啟動任務",
  mission_clear: "清除任務", mission_change: "更換任務",
  "mode:rtl": "返航", "mode:hold": "中斷任務（懸停）", "mode:land": "降落",
  "mode:mission": "繼續任務", "mode:guided": "切 GUIDED",
};
function actionLabel(a: string): string {
  if (ACTION_LABELS[a]) return ACTION_LABELS[a];
  // `takeoff:15.0m`＝帶高度的起飛。字尾是資料不是型別，拆開來講
  const m = /^takeoff:(.+)$/.exec(a);
  if (m) return `起飛（到 ${m[1]}）`;
  return a;
}

/** 一趟飛行是怎麼結束的。**「上鎖」與「我們看不到它了」是兩件事**
 * （後端 _close_orphan_sessions 的同一條紀律）：前者是飛行結束，後者只代表
 * 資料在那裡斷了——事後看架次時混在一起，會以為那趟就是那麼長。
 * **照枚舉列，不猜字串**：認不得就顯示原代號。 */
const END_LABELS: Record<string, string> = {
  disarmed: "上鎖（正常結束）",
  telemetry_lost: "遙測中斷（不代表飛行結束）",
  telemetry_lost_backfilled: "遙測中斷，事後由機上補回",
};

/** 影像那一欄的說法。**認不得的原樣顯示代碼**，同 END_LABELS 的理由。 */
const VIDEO_LABELS: Record<string, string> = {
  on: "有錄影", off: "未錄影", no_source: "沒有影像源",
  // 這一趟從未離地（飛控說全程 on_ground），影像已自動刪除
  // ——**架次紀錄留著**，刪的只有影像（flight-video-design §8c）
  discarded: "未離地・影像已刪",
};

/** 指令的下場。**「被擋下」與「送出後失敗」不是同一件事**——前者是守門
 * 攔住了（飛機沒動），後者是指令出去了而沒成（飛機可能動了一半）。
 * 兩者在畫面上必須分得開。 */
const RESULT_TONE: Record<string, { tone: string; label: string }> = {
  accepted: { tone: "ok", label: "已執行" },
  refused: { tone: "warn", label: "守門擋下" },
  rejected: { tone: "warn", label: "機端拒絕" },
  rejected_precheck: { tone: "warn", label: "預檢擋下" },
  timeout: { tone: "danger", label: "逾時無回應" },
  failed: { tone: "danger", label: "執行失敗" },
  error: { tone: "danger", label: "錯誤" },
};

export default function InfoFlights({ drones }: { drones: DroneRow[] }) {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [drone, setDrone] = useState("");
  const [withTest, setWithTest] = useState(false);
  const [selId, setSelId] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    const p = new URLSearchParams({ limit: "200", with_events: "true" });
    if (drone) p.set("drone_id", drone);
    if (withTest) p.set("include_test", "true");
    setSessions(null); setErr(null);
    getJson<SessionRow[]>(`${API}/api/sessions?${p}`)
      .then((rows) => {
        if (stop) return;
        // 逐列解析 summary：一筆壞掉不得讓整份清單消失（lib/jsonb.ts）
        setSessions(rows.map((r) => {
          const v = parseJsonb(r.summary);
          return { ...r, summary: (v.ok ? v.value : null) as SessionRow["summary"] };
        }));
      })
      .catch((e) => {
        if (stop) return;
        setErr(errText((e as Error).message, "無法取得架次清單"));
      });
    return () => { stop = true; };
  }, [drone, withTest]);

  // 清單換了就把選擇收回到第一筆——**留著一個不在清單裡的選擇**會讓右側
  // 顯示一趟左邊看不到的飛行
  useEffect(() => {
    if (!sessions) return;
    if (!sessions.some((s) => s.id === selId)) setSelId(sessions[0]?.id ?? null);
  }, [sessions, selId]);

  const sel = sessions?.find((s) => s.id === selId) ?? null;

  return (
    <div className="info-split">
      <div className="card info-list">
        <h3>架次
          <span className="h3-note">{sessions ? `${sessions.length} 趟` : ""}</span>
        </h3>
        <div className="info-filters info-filters-tight">
          <label>無人機
            <select value={drone} onChange={(e) => setDrone(e.target.value)}>
              <option value="">全部</option>
              {drones.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </label>
          <label className="info-check">
            <input type="checkbox" checked={withTest}
              onChange={(e) => setWithTest(e.target.checked)} />
            含測試架次
          </label>
        </div>
        {err && <div className="form-err">{err}</div>}
        {!err && sessions === null && <div className="empty">載入中…</div>}
        {!err && sessions?.length === 0 && (
          <div className="empty">
            {drone ? "這台機還沒有飛行紀錄。" : "還沒有任何飛行紀錄——一次解鎖到上鎖＝一趟。"}
          </div>
        )}
        {/* **日期做群組標頭**：逐列重複年月日的話，真正在變的（時間、時長、
            出過什麼事）會被擠到右邊（ui-spec §6c.7） */}
        <div className="info-rows">
          {(sessions ?? []).map((s, i, arr) => (
            <Fragment key={s.id}>
              {dayShort(s.started_at).slice(0, 5)
                !== dayShort(arr[i - 1]?.started_at ?? "").slice(0, 5) && (
                <div className="info-day">{dayShort(s.started_at).slice(0, 5)}</div>
              )}
            <button
              className={`info-srow${s.id === selId ? " on" : ""}`}
              onClick={() => setSelId(s.id)}>
              <span className="info-sday">{hms(s.started_at).slice(0, 5)}</span>
              <span className="info-sdur">{duration(s.started_at, s.ended_at)}</span>
              {/* 機名只在「機：全部」時出現——已經篩成一台了還逐列重複是雜訊 */}
              {!drone && <span className="info-sname">{s.drone_name}</span>}
              <span className="spacer" />
              {/* 「這趟出過事嗎」要在清單上看得出來，不必逐趟點進去 */}
              {!!s.events_critical && (
                <span className="info-badge bad" title="危急事件">{s.events_critical}</span>
              )}
              {!!s.events_warning && (
                <span className="info-badge warn" title="警告事件">{s.events_warning}</span>
              )}
            </button>
            </Fragment>
          ))}
        </div>
      </div>

      <div className="info-detail">
        {!sel && !err && sessions?.length !== 0 && (
          <div className="card"><div className="empty">左邊選一趟。</div></div>
        )}
        {sel && <FlightDetail s={sel} onReplay={() => router.push(`/replay/${sel.id}`)} />}
      </div>
    </div>
  );
}

function FlightDetail({ s, onReplay }: { s: SessionRow; onReplay: () => void }) {
  return (
    <>
      <div className="card">
        {/* **這趟是什麼**（chip）與**量到什麼**（KPI）分兩層：舊版八格平鋪，
            「影像 沒有影像源」與「平均 SINR」一樣大（ui-spec §6c.7） */}
        <h3>
          {s.drone_name}
          <span className="h3-note">
            {dateTime(s.started_at)} · {duration(s.started_at, s.ended_at)}
            {s.ended_at ? "" : "（尚未結束）"}
          </span>
        </h3>
        <div className="chips info-chips">
          {/* **飛行中換過路徑要說出來**（doc/data-schema §3.4）：`mission_name`
              是解鎖那一刻那份，機上後來飛的可能是別份——不標的話這個 chip
              就是一句說錯的話。使用者定案：換路徑仍然是同一趟 */}
          <span className="chip">
            {s.mission_name ?? "無任務"}
            {!!s.plan_changes && `（飛行中換過 ${s.plan_changes} 次）`}
          </span>
          {!!s.plan_changes && (
            <InfoTip tip={"這一趟飛到一半換過路徑。上面寫的是**解鎖那一刻**那份，"
              .replace(/\*\*/g, "")
              + "換成哪一份、幾點換的看下面「指令」那一段的「上傳任務」。"
              + "一趟可以飛不只一份路徑——架次的邊界是解鎖到上鎖，飛機沒落地，"
              + "中間那個切點在物理上什麼都沒發生。"} />
          )}
          <span className="chip">{VIDEO_LABELS[s.video_mode ?? ""] ?? s.video_mode ?? "影像未知"}</span>
        </div>
        <div className="metrics info-metrics">
          <M label="鏈路樣本" value={s.summary?.samples_total != null
            ? String(s.summary.samples_total) : "—"} />
          <M label="平均 SINR" value={num(s.summary?.avg_sinr)} unit="dB" />
          <M label="最低 SINR" value={num(s.summary?.min_sinr)} unit="dB" />
          <M label="平均 RTT" value={num(s.summary?.avg_rtt_ms, 0)} unit="ms" />
          <M label="最高高度" value={num(s.summary?.max_alt_rel, 0)} unit="m" />
          {/* 怎麼結束的：正常上鎖與被切斷是兩件事，畫面要分得開 */}
          <M label="結束方式" value={s.end_reason
            ? END_LABELS[s.end_reason] ?? s.end_reason
            : (s.ended_at ? "—" : "尚未結束")} />
        </div>
        {s.note && <div className="hint-line">備註：{s.note}</div>}
        <TelemetryQuality sessionId={s.id} />
        <div className="cmd-row info-actions">
          <button className="btn-plain btn-sm" onClick={onReplay}>▶ 開回放</button>
          <a className="btn-plain btn-sm"
            href={`${API}/api/sessions/${s.id}/export`}>⤓ 匯出完整 JSON</a>
          {/* 這句話讀第一次有用、讀第五十次只是把數字往下擠（ui-spec §6c.7） */}
          <InfoTip tip={"匯出檔含這一趟的遙測、鏈路、事件與指令。"
            + "資料庫的保留期到了會清掉，匯出的那一份不會——要留很久的就匯出。"} />
        </div>
      </div>

      <CommandsCard sessionId={s.id} />
      <SessionEventsCard sessionId={s.id} droneName={s.drone_name} />
      <CoverageBlock sessionId={s.id} />
    </>
  );
}

/** 這一趟的遙測是誰寫進來的，兩份說法有沒有打架。
 *
 * **為什麼要在架次頁講**：`telemetry` 有兩個來源——即時串流（直接來自飛控的
 * 封包）與機上補傳（代理在斷線期間緩衝、恢復後補送）。2026-09-07 之前補傳的
 * 去重從來沒生效過（比對百分秒，而兩條路的百分秒天生不同，見 d6dea0b），
 * 於是**飛機正在空中的那些秒，被插進「機在地上 LOITER」的樣本**，兩種互相
 * 矛盾的資料在匯出檔裡長得一樣可信。
 *
 * 修法只擋住未來，**已經寫進去的列還在**。所以判讀或匯出這一趟之前，這裡要
 * 說得出：有沒有、幾筆、差多遠。**不自動修正、也不隱藏**——那是資料，不是
 * 顯示問題；要刪要留是人的決定（`scripts/clean-backfill-conflicts.py`）。
 *
 * 沒有補傳列時整段不畫：**大多數架次沒有這回事，不必每一趟都掛一句話**。 */
function TelemetryQuality({ sessionId }: { sessionId: string }) {
  const [q, setQ] = useState<{
    live: number; backfilled: number; conflicts: number;
    max_gap_m: number | null; mode_mismatch: number; rule: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    setQ(null); setErr(null);
    getJson<NonNullable<typeof q>>(`${API}/api/sessions/${sessionId}/telemetry-quality`)
      .then((r) => { if (!stop) setQ(r); })
      // **取不到不得靜默**：靜默＝這一趟看起來沒有補傳問題（§0.2e）
      .catch((e) => { if (!stop) setErr(errText((e as Error).message, "無法取得遙測來源統計")); });
    return () => { stop = true; };
  }, [sessionId]);

  if (err) return <div className="form-err">{err}</div>;
  if (!q || q.backfilled === 0) return null;
  const gap = q.max_gap_m != null ? `，位置最遠差 ${q.max_gap_m} m` : "";
  return (
    <div className={q.conflicts > 0 ? "tq-row tq-bad" : "tq-row"}>
      <span className="tq-main">
        遙測 {q.live} 筆即時 · {q.backfilled} 筆機上補傳
        {q.conflicts > 0 && (
          <b>　{q.conflicts} 筆補傳落在即時資料已覆蓋的秒數上{gap}</b>
        )}
      </span>
      <InfoTip tip={q.conflicts > 0
        ? `這些列是 2026-09-07 修好的去重漏洞留下的（${q.rule}）：同一批時間戳上有兩份互相矛盾的資料，其中 ${q.mode_mismatch} 筆連飛行模式都不一樣。即時那份直接來自飛控，補傳那份是代理的 1Hz 快照——判讀與匯出這一趟時，補傳那份要排除。已經寫進去的列不會自動刪，那是資料不是顯示問題。`
        : `這一趟有 ${q.backfilled} 筆是機上補傳（斷線期間代理緩衝、恢復後補送），沒有任何一筆與即時資料撞在同一秒。補傳列在匯出檔裡帶 backfilled=true。`} />
    </div>
  );
}

function M({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="metric">
      <div className="label">{label}</div>
      <div className="value">{value}{unit && <span className="unit">{unit}</span>}</div>
    </div>
  );
}

/** ② 這一趟下了什麼指令。**被擋下來的也要在**（2026-09-02 起 command_log
 * 留痕）——「我按了但它沒動」是事後最需要回答的問題之一。 */
function CommandsCard({ sessionId }: { sessionId: string }) {
  const [rows, setRows] = useState<CommandRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [raw, setRaw] = useState<
    { title: string; meta: string; body: string } | null>(null);
  useEffect(() => {
    let stop = false;
    setRows(null); setErr(null);
    getJson<CommandRow[]>(`${API}/api/sessions/${sessionId}/commands`)
      .then((r) => { if (!stop) setRows(r); })
      .catch((e) => { if (!stop) setErr(errText((e as Error).message, "無法取得指令紀錄")); });
    return () => { stop = true; };
  }, [sessionId]);

  return (
    <div className="card">
      <h3>指令<InfoTip tip="含被擋下來的。點一列看飛控原樣回了什麼（ACK、嘗試次數、耗時）。「守門擋下」是我方攔住了（飛機沒動），「機端拒絕」是指令出去了而飛控不接受，「逾時無回應」是送出去了不知道結果——三者的處置完全不同。" /></h3>
      {err && <div className="form-err">{err}</div>}
      {!err && rows === null && <div className="empty">載入中…</div>}
      {!err && rows?.length === 0 && (
        <div className="empty">
          這一趟沒有從本系統下過指令。
          {/* **不要說成「沒有人操作過」**：飛手拿實體遙控器飛的那些，
              這張表本來就看不到 */}
          <div className="hint-line">（用實體遙控器飛的動作不會記在這裡。）</div>
        </div>
      )}
      {!!rows?.length && (
        <div className="info-cmds">
          {rows.map((c, i) => {
            const r = RESULT_TONE[c.result] ?? { tone: "warn", label: c.result };
            // **飛控原樣回的那包不攤在畫面上**：一列塞不下就截斷，讀不完也
            // 讀不懂，卻佔掉整個區塊。列上只留「花多久」，其餘進 modal
            const d = parseJsonb(c.detail);
            const obj = (d.ok && d.value && typeof d.value === "object")
              ? d.value as Record<string, unknown> : null;
            const acks = obj && typeof obj.steps === "object" && obj.steps
              ? Object.values(obj.steps as Record<string, { ack_ms?: number }>)
                .map((v) => v?.ack_ms).filter((v): v is number => v != null)
              : [];
            return (
              <button className="info-cmd info-cmd-tap" key={i}
                title={c.detail ? "點擊看飛控原樣回了什麼" : undefined}
                disabled={!c.detail}
                onClick={() => c.detail && setRaw({
                  title: actionLabel(c.action),
                  meta: `${hms(c.time)}　${r.label}${c.client ? `　${c.client}` : ""}`,
                  body: obj ? JSON.stringify(obj, null, 2) : c.detail,
                })}>
                <time>{hms(c.time)}</time>
                <span className="info-cmdact">{actionLabel(c.action)}</span>
                {/* 上傳／更換任務要說得出是哪一份——只有 uuid 的話，
                    「飛行中換成什麼」在畫面上答不出來 */}
                {c.action.startsWith("mission_") && (obj?.mission_id || c.mission_name) && (
                  <span className="hint-line">
                    {c.mission_name ?? "已刪除的路徑"}
                  </span>
                )}
                <span className={`chip cap-chip-${r.tone}`}>
                  <span className={`dot cap-dot-${r.tone}`} />{r.label}
                </span>
                {!!acks.length && (
                  <span className="hint-line">{Math.round(Math.max(...acks))} ms</span>
                )}
                <span className="spacer" />
                {c.client && <span className="hint-line">{c.client}</span>}
              </button>
            );
          })}
        </div>
      )}
      {raw && <RawModal {...raw} onClose={() => setRaw(null)} />}
    </div>
  );
}

/** 會改變「飛機在做什麼」的事。**這不是過濾掉，是預設**——「全部」永遠按得到。 */
const KEY_TYPES = new Set([
  "mode_change", "mission_state", "failsafe", "rc_link", "blackout",
  "link_state", "geofence", "precheck", "admission",
]);
function isKeyEvent(e: EventRow): boolean {
  const sv = normSev(e.severity);
  return sv === "critical" || sv === "warning" || KEY_TYPES.has(e.type);
}

/** 原始資料的家。**它該在一個「想看才會打開」的位置**——攤在列上時，
 * 一列塞不下就截斷，那既不是可讀的資訊也不是可用的資料。 */
function RawModal({ title, meta, body, onClose }: {
  title: string; meta: string; body: string; onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="name">{title}</span>
          <span className="meta">{meta}</span>
          <span className="spacer" />
          <button className="modal-close" aria-label="關閉（Esc）" title="關閉（Esc）"
            onClick={onClose}>✕</button>
        </div>
        <div className="modal-text"><pre className="info-raw">{body}</pre></div>
      </div>
    </div>
  );
}

/** ③ 這一趟發生了什麼。時間**正序**——讀一趟飛行是從頭讀到尾。 */
function SessionEventsCard({ sessionId, droneName }: {
  sessionId: string; droneName: string;
}) {
  const [rows, setRows] = useState<EventRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // **預設只顯示會改變「飛機在做什麼」的那些**（ui-spec §6c.7）：一次起飛在
  // 舊版是四列（收下了 NAV_TAKEOFF／模式換／Mission: 1 Takeoff／任務進度），
  // 同一件事的第二、第三種說法把真正要看的擠掉。不是過濾掉——「全部」按得到。
  // 來源（機上／系統）的篩選在「事件」分頁，這裡不重複一套軸
  const [key, setKey] = useState<"key" | "all">("key");
  const [open, setOpen] =
    useState<(EventRow & { timeFirst?: string; times?: number[] }) | null>(null);
  const [foldOn, setFoldOn] = useState(true);
  useEffect(() => {
    let stop = false;
    setRows(null); setErr(null);
    getJson<EventRow[]>(`${API}/api/events?session_id=${sessionId}&limit=1000`)
      .then((r) => { if (!stop) setRows(r); })
      .catch((e) => { if (!stop) setErr(errText((e as Error).message, "無法取得事件")); });
    return () => { stop = true; };
  }, [sessionId]);

  const shown = (rows ?? [])
    .filter((e) => key === "all" || isKeyEvent(e))
    // 逐列解析 detail：一列壞掉不得吃掉整批（lib/jsonb.ts）
    .map((e) => ({ ...e, detail: eventDetail(e.detail) }));
  // 一趟之內同一句話重複（任務進度、ACK）照樣佔滿版面——同 lib/foldEvents.tsx。
  // **這裡的時間正序是刻意的**（讀一趟飛行從頭讀到尾），折疊後改以最近一次
  // 排序會把順序倒過來，所以折完再依首次時間正排
  const groups = (foldOn ? foldEvents(shown) : asGroups(shown))
    .sort((a, b) => new Date(a.first).getTime() - new Date(b.first).getTime());

  return (
    <div className="card">
      <h3>事件
        <span className="h3-note">
          {rows ? `${rows.length} 則・顯示 ${groups.length}` : ""}
        </span>
        <span className="ev-filter">
          {([["key", "重點"], ["all", "全部"]] as const).map(([k, label]) => (
            <button key={k} className={key === k ? "on" : ""}
              onClick={() => setKey(k)}>{label}</button>
          ))}
        </span>
        <InfoTip tip={"「重點」＝危急、警告，加上模式切換、任務狀態、失效保護、"
          + "遙控器鏈路這幾種會改變飛機在做什麼的事。其餘（飛控收到指令的 ACK、"
          + "逐條機上訊息、任務進度）是同一件事的第二、第三種說法，"
          + "按「全部」看得到，不預設佔版面。來源（機上／系統）的篩選在「事件」分頁。"} />
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
          這一趟沒有留下事件。
          {/* 空事件流的語意（ui-spec §0.2e）：這句話說的是「這一趟沒事件」，
              **不是「沒有異常發生」**——事件要有 session_id 才掛得上這一趟，
              解鎖之前的那些在「事件」分頁 */}
          <div className="hint-line">解鎖之前發生的事不掛在架次上——去「事件」分頁看。</div>
        </div>
      )}
      {!!groups.length && (
        <div className="events info-sevents">
          {groups.map((g) => {
            const e = g.latest, d = e.detail;
            const sv = normSev(e.severity);
            return (
              <div className="event ev-tap" key={g.key}
                title={g.count > 1 ? foldTitle(g) : "點擊看詳情"}
                onClick={() => setOpen({
                  ...e,
                  detail: g.count > 1 ? { ...d, count: g.count } : d,
                  ...(g.count > 1 ? { timeFirst: g.first, times: g.times } : {}),
                })}>
                <span className="dot" style={{ background: SEV_COLOR[sv] }} />
                {/* 折疊列顯示**首次**時間：讀一趟飛行是照發生順序讀的 */}
                <time>{hms(g.first)}</time>
                <span className="detail">
                  {emph(evText({ type: e.type, detail: d,
                    severity: e.severity as "info" | "warning" | "critical" }))}
                </span>
                {g.count > 1 && <span className="ev-count">×{g.count}</span>}
                {g.count > 1 && <EvDensity times={g.times} color={SEV_COLOR[sv]} />}
              </div>
            );
          })}
        </div>
      )}
      {open && (
        <EventModal onClose={() => setOpen(null)}
          ev={{ id: open.id, time: open.time, type: open.type, severity: open.severity,
            detail: open.detail, source: open.source, timeFirst: open.timeFirst,
            times: open.times, drone: droneName }} />
      )}
    </div>
  );
}

/** ④ 錄到的東西還在不在。覆蓋帶元件與「錄製與回傳」分頁共用同一份。 */
function CoverageBlock({ sessionId }: { sessionId: string }) {
  const [cov, setCov] = useState<Coverage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    setCov(null); setErr(null);
    getJson<Coverage>(`${API}/api/onboard-captures/coverage?session_id=${sessionId}`)
      .then((c) => { if (!stop) setCov(c); })
      .catch((e) => { if (!stop) setErr(errText((e as Error).message, "算不出這一趟的錄製涵蓋")); });
    return () => { stop = true; };
  }, [sessionId]);
  if (err) return <div className="card"><h3>錄製涵蓋</h3><div className="form-err">{err}</div></div>;
  if (!cov) return <div className="card"><h3>錄製涵蓋</h3><div className="empty">載入中…</div></div>;
  return <CoverageCard cov={cov} />;
}
