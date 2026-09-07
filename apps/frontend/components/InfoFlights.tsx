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
import { useEffect, useState } from "react";
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
import { eventDetail, parseJsonb } from "@/lib/jsonb";
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
        <div className="info-rows">
          {(sessions ?? []).map((s) => (
            <button key={s.id}
              className={`info-srow${s.id === selId ? " on" : ""}`}
              onClick={() => setSelId(s.id)}>
              <span className="info-sday">{dayShort(s.started_at)}</span>
              <span className="info-sname">{s.drone_name}</span>
              <span className="info-sdur">{duration(s.started_at, s.ended_at)}</span>
              {/* 「這趟出過事嗎」要在清單上看得出來，不必逐趟點進去 */}
              {!!s.events_critical && (
                <span className="info-badge bad" title="危急事件">{s.events_critical}</span>
              )}
              {!!s.events_warning && (
                <span className="info-badge warn" title="警告事件">{s.events_warning}</span>
              )}
            </button>
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
        <h3>
          {s.drone_name}
          <span className="h3-note">
            {dateTime(s.started_at)} · {duration(s.started_at, s.ended_at)}
            {s.ended_at ? "" : "（尚未結束）"}
          </span>
        </h3>
        <div className="metrics info-metrics">
          <M label="任務" value={s.mission_name ?? "無"} />
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
          <M label="影像" value={s.video_mode === "off" ? "未錄影"
            : s.video_mode === "no_source" ? "沒有影像源"
              : s.video_mode === "on" ? "有錄"
                : s.video_mode ?? "—"} />
        </div>
        {s.note && <div className="hint-line">備註：{s.note}</div>}
        <div className="cmd-row info-actions">
          <button className="btn-plain btn-sm" onClick={onReplay}>▶ 開回放</button>
          <a className="btn-plain btn-sm"
            href={`${API}/api/sessions/${s.id}/export`}>⤓ 匯出完整 JSON</a>
          <span className="hint-line">
            匯出檔含遙測、鏈路、事件與指令——<b>保留期到了 DB 會清掉，匯出的不會</b>
          </span>
        </div>
      </div>

      <CommandsCard sessionId={s.id} />
      <SessionEventsCard sessionId={s.id} droneName={s.drone_name} />
      <CoverageBlock sessionId={s.id} />
    </>
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
      <h3>指令<InfoTip tip="含被擋下來的。「守門擋下」是我方攔住了（飛機沒動），「機端拒絕」是指令出去了而飛控不接受，「逾時無回應」是送出去了不知道結果——三者的處置完全不同。" /></h3>
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
            return (
              <div className="info-cmd" key={i}>
                <time>{hms(c.time)}</time>
                <span className="info-cmdact">{actionLabel(c.action)}</span>
                <span className={`chip cap-chip-${r.tone}`}>
                  <span className={`dot cap-dot-${r.tone}`} />{r.label}
                </span>
                {c.detail && (
                  <span className="info-cmddetail" title={c.detail}>{c.detail}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** ③ 這一趟發生了什麼。時間**正序**——讀一趟飛行是從頭讀到尾。 */
function SessionEventsCard({ sessionId, droneName }: {
  sessionId: string; droneName: string;
}) {
  const [rows, setRows] = useState<EventRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [src, setSrc] = useState<"all" | "vehicle" | "system">("all");
  const [open, setOpen] = useState<EventRow | null>(null);
  useEffect(() => {
    let stop = false;
    setRows(null); setErr(null);
    getJson<EventRow[]>(`${API}/api/events?session_id=${sessionId}&limit=1000`)
      .then((r) => { if (!stop) setRows(r); })
      .catch((e) => { if (!stop) setErr(errText((e as Error).message, "無法取得事件")); });
    return () => { stop = true; };
  }, [sessionId]);

  const shown = (rows ?? []).filter((e) =>
    src === "all" || (src === "vehicle" ? e.source === "vehicle" : e.source !== "vehicle"));

  return (
    <div className="card">
      <h3>事件
        <span className="h3-note">{rows ? `${rows.length} 則` : ""}</span>
        <span className="ev-filter">
          {([["all", "全部"], ["vehicle", "機上訊息"], ["system", "系統"]] as const)
            .map(([k, label]) => (
              <button key={k} className={src === k ? "on" : ""}
                onClick={() => setSrc(k)}>{label}</button>
            ))}
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
      {!!shown.length && (
        <div className="events info-sevents">
          {shown.map((e) => {
            const d = eventDetail(e.detail);
            return (
              <div className="event ev-tap" key={e.id} title="點擊看詳情"
                onClick={() => setOpen({ ...e, detail: d })}>
                <span className="dot" style={{
                  background: SEV_COLOR[e.severity] ?? SEV_COLOR.info }} />
                <time>{hms(e.time)}</time>
                <span className="detail">
                  {emph(evText({ type: e.type, detail: d,
                    severity: e.severity as "info" | "warning" | "critical" }))}
                </span>
              </div>
            );
          })}
        </div>
      )}
      {open && (
        <EventModal onClose={() => setOpen(null)}
          ev={{ id: open.id, time: open.time, type: open.type, severity: open.severity,
            detail: open.detail, source: open.source, drone: droneName }} />
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
