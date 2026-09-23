"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import ConfirmModal from "@/components/ConfirmModal";
import InfoTip from "@/components/InfoTip";
import LogIndexSheet from "@/components/LogIndexSheet";
import Squads, { type Squad } from "@/components/Squads";
import { Battery, SignalBars } from "@/components/SimpleHud";
import { errText, getJson } from "@/lib/fetchJson";
import { parseJsonb } from "@/lib/jsonb";
import { API, classifySinr } from "@/lib/signal";
import { ageText } from "@/lib/staleness";
import { AgentState, useUavStore } from "@/lib/store";

interface Drone {
  id: string; name: string; is_simulated: boolean; is_primary: boolean;
  connection_url: string | null; status: string | null;
  mav_sysid: number | null;
  board_uid?: string | null; flight_sw_version?: string | null;
  airframe_serial?: string | null; model?: string | null;
  /** 槳徑（mm）。**不參與任何判定**——見下方「機體」那一段 */
  prop_diameter_mm?: number | null;
  video_url: string | null;
  /** 相機來源（issue 022）：**地面站要去拉的** RTSP。與 video_url 是兩件事 */
  camera_url: string | null;
  autopilot?: string | null;    // "px4"/"ardupilot"/"unknown"；null＝從未見 MAVLink 心跳
  agent?: AgentState | null;    // 意圖通道現況（/api/drones 帶，之後由 WS 更新）
}

/** 意圖協定的狀態 → 人話。**認不得的原樣顯示**——代理版本比地面站新時，
 * 硬翻成「未知」會讓「協定長出了新狀態」看起來像「壞了」。 */
const STATE_TEXT: Record<string, string> = {
  DISCONNECTED: "飛控無心跳", NOT_READY: "地面未就緒", READY: "地面待命",
  ARMED_GROUND: "已解鎖・在地上", TAKING_OFF: "爬升中",
  FLYING_MISSION: "執行路徑中", HOLDING: "空中暫停", RETURNING: "返航中",
  LANDING: "降落中", PILOT_CONTROL: "飛手接管", EMERGENCY: "飛控 failsafe",
};

// 機型標示（issue 015 機隊盤點）：欄位缺席＝舊後端，不顯示
function apChip(ap: string | null | undefined): string | null {
  if (ap === undefined) return null;
  if (ap === null) return "未見 MAVLink 心跳";
  return { px4: "PX4", ardupilot: "ArduPilot" }[ap] ?? "機型未知";
}
interface Session {
  id: string; drone_id: string; drone_name: string;
  plan_name: string | null;
  started_at: string; ended_at: string | null;
  summary: {
    avg_sinr?: number | null; min_sinr?: number | null; avg_rtt_ms?: number | null;
    max_alt_rel?: number | null; samples_total?: number;
  } | null;
  video_mode?: string | null;   // §5.4：off＝未錄影弱字標記（有錄不標）
  end_reason?: string | null;   // 「上鎖」與「我們看不到它了」是兩件事
  events_total?: number; events_warning?: number; events_critical?: number;
}

/** 一趟是怎麼結束的。**照枚舉列，不猜字串**：認不得就顯示原代號。 */
const END_LABELS: Record<string, string> = {
  disarmed: "上鎖（正常結束）",
  telemetry_lost: "遙測中斷（不代表飛行結束）",
  telemetry_lost_backfilled: "遙測中斷，事後由機上補回",
};

const fmt = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

const shortWhen = (iso: string) =>
  new Date(iso).toLocaleString("zh-TW", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false });

function duration(a: string, b: string | null): string {
  if (!b) return "進行中";
  const s = Math.round((new Date(b).getTime() - new Date(a).getTime()) / 1000);
  return `${Math.floor(s / 60)}m${(s % 60).toString().padStart(2, "0")}s`;
}

export default function Drones() {
  const router = useRouter();
  const [drones, setDrones] = useState<Drone[]>([]);
  // 三態各自有話（§0.2e）：原本「讀取中，或 backend 未連線」把兩種狀態塞進
  // 同一句——讀取卡住時看起來像 backend 掛了、backend 掛了時看起來像還在讀，
  // **兩個都沒真的宣告**。這頁尤其不能混：沒有機與連不上，處置完全不同。
  // 這個形狀不需要任何事情出錯就在說謊，所以錯誤注入測不到，只能讀文案
  const [dronesLoaded, setDronesLoaded] = useState(false);
  const [dronesErr, setDronesErr] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const live = useUavStore((s) => s.live);
  const fleet = useUavStore((s) => s.fleet);
  const agents = useUavStore((s) => s.agents);

  // 卡片摺疊（compare-drones-restyle §2）：收合＝一機一行的機隊全貌，
  // 展開＝架次工作區。工作區判準 → per 機 localStorage 記憶；
  // 單機自動展開（僅在無記憶時——全貌不成問題，多一次點擊是純摩擦）
  const [open, setOpen] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (!drones.length) return;
    setOpen((cur) => {
      const next = { ...cur };
      for (const d of drones) {
        if (next[d.id] === undefined) {
          const saved = localStorage.getItem(`drone-open:${d.id}`);
          next[d.id] = saved != null ? saved === "1" : drones.length === 1;
        }
      }
      return next;
    });
  }, [drones]);
  const toggleOpen = (id: string) =>
    setOpen((cur) => {
      const v = !cur[id];
      localStorage.setItem(`drone-open:${id}`, v ? "1" : "0");
      return { ...cur, [id]: v };
    });

  const reload = useCallback(() => {
    // 取得失敗經 catch 說出來（見 lib/fetchJson.ts）：空清單＝「沒有無人機／
    // 沒有航線」是一個宣告，我方取不到時不該替後端宣告
    getJson<Drone[]>(`${API}/api/drones`)
      .then((d) => { setDrones(d); setDronesLoaded(true); setDronesErr(false); })
      .catch(() => { setDronesErr(true); setErr("無法取得無人機清單"); });
    getJson<any[]>(`${API}/api/sessions?limit=500&with_events=true`)
      .then((rows) =>
        setSessions(
          // 逐列解析：一筆 summary 壞掉不得讓整份架次清單消失
          // （見 lib/jsonb.ts）。壞掉那筆的數值欄位顯示「—」，列還在
          rows.map((r: any) => {
            const v = parseJsonb(r.summary);
            return { ...r, summary: v.ok ? v.value : null };
          })
        )
      )
      .catch(() => setErr("無法取得航線清單"));
  }, []);
  useEffect(reload, [reload]);

  // 撞號偵測（issues/038）：sysid 是機上可改的參數，兩台機設成同號時
  // 遙測會互相覆蓋。這件事後端不會擋（擋了反而讓機連不上），只能標出來
  const dupSysid = new Set(
    drones
      .map((d) => d.mav_sysid)
      .filter((v, i, a) => v != null && a.indexOf(v) !== i) as number[]
  );

  const [toDelete, setToDelete] = useState<Drone | null>(null);
  // 欄位編輯改用 modal（取代 window.prompt）：prompt 放不下說明，也帶不了
  // 「留空＝清除」這種語意，而影像位址恰恰需要說清楚
  const [editing, setEditing] = useState<{
    drone: Drone; field: string; label: string; value: string | null; tip: string;
  } | null>(null);
  // 每台機已回傳幾份機上錄製。**一次抓、不輪詢**：/api/onboard-captures 會掃
  // 目錄，它的 docstring 自己說是「人按出來的，不是熱路徑」
  const [onboardCount, setOnboardCount] = useState<Record<string, number>>({});
  useEffect(() => {
    getJson<{ files: { drone_id: string; status: string }[] }>(
      `${API}/api/onboard-captures`)
      .then((d) => {
        const n: Record<string, number> = {};
        for (const f of d.files ?? []) {
          if (f.status === "complete") n[f.drone_id] = (n[f.drone_id] ?? 0) + 1;
        }
        setOnboardCount(n);
      })
      .catch(() => setOnboardCount({}));   // 拿不到就不顯示份數，不猜 0
  }, []);
  // 機列上的小隊 chip：**一台機可以在多隊**，所以是清單不是單一值
  const [squads, setSquads] = useState<Squad[]>([]);
  useEffect(() => {
    getJson<Squad[]>(`${API}/api/squads`).then(setSquads).catch(() => setSquads([]));
  }, []);

  /** 這台機現在是什麼狀態——**一句話，而且說得出根據**。
   *
   * 三種：飛行中（在線且已解鎖）／在線（連著、還沒解鎖）／未連線。
   *
   * **「曾經飛過」與「從來沒有」不另外分成兩種狀態**（使用者定案 2026-09-08）：
   * 兩者現在都是「未連線」——差別在副行的「上次飛行」有沒有值，那是一個事實，
   * 不必再變成一個要學的狀態名。 */
  function statusOf(d: Drone, mine: Session[]) {
    const t = fleet[d.id];
    const ag = agents[d.id] ?? d.agent;
    const pending = ag?.fresh ? ag.record_upload?.pending ?? 0 : 0;
    const lastFlight = mine.length ? mine[0].started_at : null;
    const online = !!t?.connected;
    if (online && t?.armed) {
      return { text: `飛行中${t.flight_mode ? ` ${t.flight_mode}` : ""}`,
        tone: "flying", online, pending, lastFlight };
    }
    if (online) {
      return { text: t?.telem_age_s != null && t.telem_age_s > 10
        ? `在線 · 遙測 ${ageText(t.telem_age_s)}` : "在線",
        tone: "on", online, pending, lastFlight };
    }
    return { text: "未連線", tone: "off", online, pending, lastFlight };
  }

  /** 排序鍵：需要注意的排前面。 */
  function rank(d: Drone) {
    const mine = sessions.filter((s) => s.drone_id === d.id);
    const st = statusOf(d, mine);
    if (st.tone === "flying") return 0;
    if (st.tone === "on") return 1;
    if (st.pending > 0) return 2;
    return 3;
  }

  // 人維護欄位的共用編輯（改名／機架序號／型號走同一條 PATCH）
  async function patch(d: Drone, body: Record<string, string>, what: string) {
    setErr(null);
    const res = await fetch(`${API}/api/drones/${d.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) setErr(errText((await res.json()).detail, `${what}失敗`));
    reload();
  }


  async function remove(d: Drone) {
    setToDelete(null);
    setErr(null);
    const res = await fetch(`${API}/api/drones/${d.id}`, { method: "DELETE" });
    if (!res.ok) {
      setErr(errText((await res.json()).detail, `刪除失敗（${res.status}）`));
      return;
    }
    reload();
  }

  return (
    <div className="page-pad">
      {/* **版本橫幅移除**（使用者指示 2026-08-26）：dirty 建置在開發期是常態，
          橫幅天天都在，於是它從「這件事要注意」變成畫面上的固定裝飾——
          常駐的警告等於沒有警告。
          §0.2f 要防的問題（「系統說不出自己是誰」）沒有被丟掉，只是換了地方：
          版本仍在導覽列品名的 tooltip、console，以及 `GET /api/version`。
          **要程式問得到才是那條規則的重點**，橫幅只是其中一種呈現。 */}
      {/* 註冊表單已移除（issues/038）：代理一上線就自報身分，機器問得到的東西
          不勞人打字。原本這裡有一段「機是怎麼出現在這裡的」說明，也拿掉了——
          它解釋的是**系統怎麼運作**，不是使用者此刻要做的決定；常駐在清單頂端
          等於每次來都再讀一次已經知道的事。機制寫在 doc/drone-registration.md。
          錯誤訊息留下來：它不是說明，是這一頁剛剛發生的事。 */}
      {err && <div className="form-err">{err}</div>}

      <Squads drones={drones.map((d) => ({ id: d.id, name: d.name }))} />

      <div className="drone-head" style={{ marginTop: 14 }}>
        {/* 「機隊」→「無人機」（使用者 2026-09-23 改名）：這一頁管的是一台一台的機體 */}
        <span className="name">無人機{drones.length ? `（${drones.length}）` : ""}</span>
        <span className="spacer" />
        <InfoTip tip="需要注意的排前面，不照註冊順序：飛行中 → 在線 → 有待回傳 → 未連線。副行的「上次飛行」說得出這台機最後一趟是什麼時候，沒有值就是還沒飛過。點一列展開那台機的架次與設定。" />
      </div>

      {/* 排序：**需要注意的排前面**，不照註冊順序（同回傳現況那張卡的規矩）。
          飛行中 → 在線 → 有待回傳 → 離線 → 未連線 */}
      {[...drones].sort((a, b) => rank(a) - rank(b)
        || a.name.localeCompare(b.name)).map((d) => {
        const mine = sessions.filter((s) => s.drone_id === d.id);
        const isLive = live?.drone_id === d.id;
        const st = statusOf(d, mine);
        return (
          <div className="card" key={d.id}>
            {/* 一行全貌：色點＋名＋**狀態一句**＋訊號＋電量＋待回傳＋N 趟。
                色點實心＝在線、空心＝不在線（形狀先於顏色）。
                **「離線」與「未連線」不同形**（§0.2e-2）：前者曾經飛過、
                說得出上次是什麼時候；後者沒有「最後已知」可言。
                離線機沒有的資訊不畫、不放「—」。 */}
            <div className="drone-head drone-row" onClick={() => toggleOpen(d.id)}>
              <span className={`dot drone-dot${st.online ? "" : " drone-dot-off"}`}
                style={st.online ? { background: "var(--status-ok)" } : undefined} />
              <span className="name">{d.name}</span>
              <span className={`drone-state${st.tone ? ` drone-state-${st.tone}` : ""}`}>
                {st.text}
              </span>
              {st.online && <SignalBars sinr={fleet[d.id]?.link?.sinr} />}
              {st.online && <Battery pct={fleet[d.id]?.battery_pct} plain />}
              {squads.filter((q) => q.members.some((m) => m.drone_id === d.id))
                .map((q) => (
                  <span className="chip" key={q.id} title="所屬小隊">{q.name}</span>
                ))}
              {st.pending > 0 && (
                <span className="chip" title="機上錄好、還沒回傳成功的份數">
                  ⚠ {st.pending} 待回傳
                </span>
              )}
              {/* 代理狀態（意圖協定 §4.2 鏡像）。**權威在機上**，這裡只轉述。
                  三種情況必須長得不一樣，否則畫面會替代理宣告它沒說過的事：
                  有代理且新鮮＝報狀態／有代理但不新鮮＝報「最後看到」／
                  沒有代理＝什麼都不說（不是「離線」，是這台機沒有代理）。 */}
              {(() => {
                const ag = agents[d.id] ?? d.agent;
                if (!ag?.state) return null;
                const txt = STATE_TEXT[ag.state] ?? ag.state;
                return ag.fresh ? (
                  <span className="chip" title={`機上代理回報（代理 ${ag.agent_version ?? "版本未知"}）`}>
                    {txt}
                  </span>
                ) : (
                  <span className="chip" style={{ opacity: 0.55 }}
                    title="意圖通道斷了。這是最後看到的狀態，不是現在的狀態">
                    最後：{txt}
                  </span>
                );
              })()}
              {/* 撞號放在**收合列**（issues/038）：兩筆記錄綁同一個 sysid 時遙測
                  會互相覆蓋，那是「這頁顯示的數字有假」的層級，不能只在展開後
                  才說——會展開的人通常已經在查了，需要提醒的是還沒起疑的人 */}
              {dupSysid.has(d.mav_sysid ?? -1) && (
                <span className="chip" style={{ background: "#d03b3b", color: "#fff" }}
                  title="有兩筆以上的記錄綁到同一個 sysid——遙測會混料，先刪掉或改掉其中一筆">
                  ⚠ sysid {d.mav_sysid} 撞號
                </span>
              )}
              <span className="spacer" />
              <span className="meta">{mine.length} 趟</span>
              <span className="meta">{open[d.id] ? "▾" : "▸"}</span>
            </div>
            {/* 副行＝身分與歷史（弱字）：**幾乎不變的欄位不搶主行**，
                但要看得到，否則「這台是哪一台」得展開才知道 */}
            <div className="drone-sub">
              {d.mav_sysid != null && <span>sysid {d.mav_sysid}</span>}
              {/* **只在認得的時候說機型**：`autopilot` 是 null 代表這筆記錄還沒
                  見過 MAVLink 心跳——而主行的「未連線」已經講完那件事，副行再
                  寫一次「未見 MAVLink 心跳」只是把同一個事實說兩遍 */}
              {d.autopilot && apChip(d.autopilot) && <span>{apChip(d.autopilot)}</span>}
              {d.flight_sw_version && <span>{d.flight_sw_version}</span>}
              {st.lastFlight && <span>上次飛行 {shortWhen(st.lastFlight)}</span>}
            </div>

            {/* 展開＝工作區：徽章＋最近時間＋操作列＋架次表格
                （刪除/匯出安全流程照舊） */}
            {open[d.id] && (
              <DroneWork d={d} sessions={mine} isLive={isLive}
                agent={agents[d.id] ?? d.agent}
                onboardFiles={onboardCount[d.id] ?? null}
                dupSysid={dupSysid.has(d.mav_sysid ?? -1)}
                onEdit={(field, label, value, tip) =>
                  setEditing({ drone: d, field, label, value, tip })}
                onPrimary={async () => {
                  const res = await fetch(`${API}/api/drones/${d.id}/primary`,
                    { method: "POST" });
                  if (!res.ok) setErr(errText((await res.json()).detail, "切換失敗"));
                  reload();
                }}
                onDelete={() => setToDelete(d)}
                onRemoveSession={(sid, when) => {
                  if (!window.confirm(`移除航線 ${when}？\n\n將刪除其全部遙測與訊號資料。`
                      + "若尚未匯出，資料將永久遺失。")) return;
                  fetch(`${API}/api/sessions/${sid}`, { method: "DELETE" }).then(reload);
                }} />
            )}
          </div>
        );
      })}
      {drones.length === 0 && (
        <div className="card"><div className="empty">
          {dronesErr ? "無法連線到系統"
            : dronesLoaded ? "尚無無人機" : "讀取中…"}
        </div></div>
      )}

      {editing && (
        <FieldEditor {...editing}
          onClose={() => setEditing(null)}
          onSave={async (v) => {
            // 數字欄位要送數字：空字串在 `int | None` 上是 422，
            // 而「留空＝清除」是這個編輯器對每一欄的承諾
            const body = editing.field === "prop_diameter_mm"
              ? { [editing.field]: v.trim() ? Number(v) : null }
              : { [editing.field]: v };
            const res = await fetch(`${API}/api/drones/${editing.drone.id}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            if (!res.ok) setErr(errText((await res.json()).detail, `${editing.label}更新失敗`));
            setEditing(null); reload();
          }} />
      )}

      {toDelete && (
        <ConfirmModal
          title={`刪除「${toDelete.name}」？`}
          onClose={() => setToDelete(null)}
          onConfirm={() => remove(toDelete)}
        >
          <p>
            將刪除其 <b>{sessions.filter((s) => s.drone_id === toDelete.id).length} 條航線</b>
            與全部遙測、訊號量測、事件資料，<b>此操作無法復原</b>。
          </p>
          <p className="hint-line">
            關聯的任務路徑不會被刪除，僅解除與此機的關聯（路徑不綁機）。若要長期保留航線資料，請先逐航線「匯出」。
          </p>
        </ConfirmModal>
      )}
    </div>
  );
}

/* ── 展開後的工作區（2026-09-08，對齊 doc/drones-redesign-proto.html）──────
 *
 * 四段，順序＝展開一台機時的問法：
 *   ① 身分     這是哪一台（除了名稱都是機器覆核的事實）
 *   ② 機上錄製 那份飛控自己錄的，回來了沒、看不看得到
 *   ③ 架次     它飛過哪些、每趟訊號如何
 *   ④ 這台機   設定與危險操作
 *
 * 原本是一排 `window.prompt` 按鈕＋兩行 hint-line：機架序號與型號**一台都沒
 * 填過**，留著只是多兩列空欄位（使用者定案 2026-09-08 移除）；影像位址是功能
 * 入口，移到第④段。
 */
function DroneWork({ d, sessions, isLive, agent, onboardFiles, dupSysid,
                     onEdit, onPrimary, onDelete, onRemoveSession }: {
  d: Drone; sessions: Session[]; isLive: boolean;
  agent: AgentState | null | undefined; onboardFiles: number | null;
  dupSysid: boolean;
  onEdit: (field: string, label: string, value: string | null, tip: string) => void;
  onPrimary: () => void; onDelete: () => void;
  onRemoveSession: (sessionId: string, when: string) => void;
}) {
  const [sheet, setSheet] = useState<{ url: string; title: string } | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const up = agent?.fresh ? agent.record_upload : undefined;

  /** 看最近一份紀錄：**按下去才抓清單**（那支端點會掃目錄，不是熱路徑）。 */
  const openLatest = async () => {
    setBusy(true); setNote(null);
    try {
      const r = await fetch(`${API}/api/onboard-captures`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      const f = (data.files ?? []).find((x: { drone_id: string; status: string }) =>
        x.drone_id === d.id && x.status === "complete");
      if (!f) setNote("這台機還沒有回傳過完整的機上錄製——飛一趟落地後再看。");
      else setSheet({ url: `${API}${f.url}/index`, title: `${f.name} · ${d.name}` });
    } catch (e) {
      // **取不到與「沒有」不同形**：這句話說的是我方失敗
      setNote(`取不到錄製清單：${(e as Error).message}`);
    }
    setBusy(false);
  };

  const kv = (k: string, v: string | null, empty: string, edit?: () => void) => (
    <div className="dw-kv" key={k}>
      <span className="dw-k">{k}</span>
      <span className={`dw-v${v ? "" : " dw-empty"}`}>{v || empty}</span>
      {edit
        ? <button className="btn-plain btn-xs" onClick={edit}>編輯</button>
        : <span />}
    </div>
  );

  return (
    <div className="dw">
      {/* 撞號：**這頁顯示的數字有假**，所以擺最上面，不等展開細節才說 */}
      {dupSysid && (
        <div className="form-err">
          ⚠ sysid {d.mav_sysid} 撞號——有兩筆以上的記錄綁到同一個號碼，遙測會互相
          覆蓋。先刪掉或改掉其中一筆。
        </div>
      )}

      <section className="dw-sect">
        <h3>身分
          <InfoTip tip="除了名稱以外都是機器每次連線覆核的事實：板子 UID 是飛控板的身分（換機架時它跟著板子走）、韌體與自駕儀來自 MAVLink 心跳、代理版本來自意圖通道。名稱是人取的，改名不影響已經記下的架次。" />
        </h3>
        <div className="dw-box">
          {kv("名稱", d.name, "—", () => onEdit("name", "名稱", d.name,
            "畫面上到處都用這個名字。改名不影響已經記下的架次。"))}
          {kv("sysid", d.mav_sysid != null ? String(d.mav_sysid) : null, "尚未收到心跳")}
          {kv("板子 UID", d.board_uid ? d.board_uid.slice(-12) : null,
            "無板子 UID（身分較弱）")}
          {kv("韌體", d.flight_sw_version ?? null, "尚未回報")}
          {kv("自駕儀", d.autopilot ? apChip(d.autopilot) : null, "未見 MAVLink 心跳")}
          {kv("機上代理", agent?.agent_version ?? null,
            agent ? "代理版本未知" : "無機上代理")}
        </div>
      </section>

      {/* **機體：人填的事實。** 與「身分」那一段刻意分開——上面那些是機器
          每次連線覆核的，這裡是只有人知道的。
          槳徑放這裡而不是放在門檻旁邊，是因為 **它不參與任何判定**
          （issues/048 第 4 項）：教科書的地效區是 1–2 倍槳徑，而 09-07
          出事是在 1.5 m，兩者對不上。填它的用途是讓那個矛盾看得見。 */}
      <section className="dw-sect">
        <h3>機體
          <InfoTip tip="人填的欄位，機器不會覆核。槳徑不參與任何判定：教科書說多旋翼的地效區大約是 1–2 倍槳徑，但 2026-09-07 這台在離地 1.5 m 就被地面擾動到失控——比教科書值高得多。所以低空門檻 3 m 是從那一次往外留的保守值，不是從槳徑算的。填槳徑是為了讓這個矛盾在畫面上看得見，日後真的要量（在不同高度各懸停 20 秒、看氣壓高度的抖動從哪裡開始收斂）時有個對照。" />
        </h3>
        <div className="dw-box">
          {kv("型號", d.model ?? null, "未填", () => onEdit("model", "型號",
            d.model ?? null, "人填的，機器不會覆核。"))}
          {kv("機架序號", d.airframe_serial ?? null, "未填",
            () => onEdit("airframe_serial", "機架序號", d.airframe_serial ?? null,
              "機架的序號。**板子 UID 認的是飛控板**，換機架時那個不會變，這個會。"))}
          {kv("槳徑", d.prop_diameter_mm
            ? `${d.prop_diameter_mm} mm（約 ${(d.prop_diameter_mm / 25.4).toFixed(0)} 吋）`
            : null, "未填",
            () => onEdit("prop_diameter_mm", "槳徑（mm）",
              d.prop_diameter_mm != null ? String(d.prop_diameter_mm) : null,
              "**不參與任何判定。** 教科書的地效區是 1–2 倍槳徑，而這台 2026-09-07 在離地 1.5 m 就被擾動到失控——比教科書值高得多。低空門檻 3 m 是從那一次往外留的，不是算出來的。留空＝清除。"))}
          {d.prop_diameter_mm ? (
            <div className="dw-kv">
              <span className="dw-k">地效區（教科書）</span>
              <span className="dw-v dw-empty">
                {(d.prop_diameter_mm / 1000).toFixed(2)}–
                {(d.prop_diameter_mm * 2 / 1000).toFixed(2)} m
                　·　實際出事在 1.5 m，門檻取 3 m
              </span>
              <span />
            </div>
          ) : null}
        </div>
      </section>

      <section className="dw-sect">
        <h3>機上錄製
          <InfoTip tip="機上錄的是飛控送出的東西，地面站錄的是送到地面站的東西——兩者相差的正是斷線那一段，所以不合併成一份。回傳只在地面進行，一解鎖就停。" />
        </h3>
        <div className="dw-box">
          {kv("已回傳", onboardFiles != null ? `${onboardFiles} 份` : null,
            "取不到清單")}
          {/* **「沒有東西要傳」與「不知道」不同形**：代理沒在推狀態時，機上還有
              幾份等著回傳我們根本看不到——說成 0 是替代理宣告它沒說過的事 */}
          {kv("待回傳", up ? (up.current ? `回傳中 ${up.current}`
            : up.pending > 0 ? `${up.pending} 份` : null) : null,
            up ? "沒有東西要傳"
              : agent ? "看不到回傳狀況（代理沒在推）" : "不知道（這台機沒有代理）")}
          <div className="dw-acts">
            <button className="btn-plain btn-sm" disabled={busy} onClick={openLatest}>
              {busy ? "查詢中…" : "看最近一份紀錄"}
            </button>
            {note && <span className="hint-line">{note}</span>}
          </div>
        </div>
      </section>

      <section className="dw-sect">
        <h3>架次<span className="h3-note">{sessions.length} 趟</span>
          <InfoTip tip="一次飛行（解鎖到上鎖）＝一列。平均 SINR 前的色點是那一趟落在哪一級。「結束方式」分得出「上鎖」與「我們看不到它了」——後者不代表飛行結束，只代表資料在那裡斷了。點一列開回放。" />
        </h3>
        <SessionTable rows={sessions} onRemove={onRemoveSession} />
      </section>

      <section className="dw-sect">
        <h3>這台機</h3>
        <div className="dw-acts">
          {/* **相機來源與播放位址是兩件事**（issue 022）：前者是地面站要去拉的
              RTSP，後者是瀏覽器要播的 WHEP。設了相機來源就會自動填播放位址，
              省得兩邊各填一次又填不一致 */}
          <button className="btn-plain btn-sm"
            title="地面站要去拉的相機 RTSP（機上那支 MediaMTX）"
            onClick={() => onEdit("camera_url", "相機來源", d.camera_url,
              "**地面站會去拉這個位址**（機上跑一支 MediaMTX，USB 相機由它轉成 RTSP）。"
              + "例：rtsp://10.141.2.32:8554/cam。\n"
              + "沒人看、也沒在錄的時候不會去拉——影像與 5G 量測共用上行，"
              + "一直傳會讓量到的不再是原本那條鏈路的品質。\n留空＝清除。")}>
            相機來源{d.camera_url ? " ✓" : ""}
          </button>
          <button className="btn-plain btn-sm"
            title="地圖點機體時開的串流位址（設了相機來源會自動填）"
            onClick={() => onEdit("video_url", "影像位址", d.video_url,
              "地圖點機體時開的串流。瀏覽器不支援 RTSP——**設了「相機來源」這一欄會自動填好**"
              + "（地面站的 WHEP 位址）。MJPEG／MP4 亦可。留空＝清除。")}>
            影像位址{d.video_url ? " ✓" : ""}
          </button>
          {d.is_primary
            ? <span className="chip">主機</span>
            : <button className="btn-plain btn-sm"
                title="MAVLink 收到的遙測記在這台名下（飛行中無法切換）"
                onClick={onPrimary}>設為主機</button>}
          {d.is_simulated && <span className="chip">模擬</span>}
          <span className="spacer" />
          <button className="btn-danger btn-sm" disabled={isLive}
            title={isLive ? "連線中的無人機無法刪除"
              : `刪除記錄與其 ${sessions.length} 趟的全部資料`}
            onClick={onDelete}>刪除這台機</button>
        </div>
      </section>

      {sheet && (
        <LogIndexSheet url={sheet.url} title={sheet.title}
          onClose={() => setSheet(null)} />
      )}
    </div>
  );
}

/** 架次表：多了分級色點、事件數與結束方式（原本只有數字）。 */
function SessionTable({ rows, onRemove }: {
  rows: Session[]; onRemove: (id: string, when: string) => void;
}) {
  const router = useRouter();
  if (!rows.length) {
    return <div className="empty">
      尚無架次——一次飛行（解鎖到上鎖）就會出現一列。
    </div>;
  }
  return (
    <div className="dw-tablewrap">
      <table className="table">
        <thead><tr>
          <th>開始</th><th>路徑</th><th>時長</th>
          <th className="num">樣本</th><th className="num">平均 SINR</th>
          <th className="num">最低</th><th className="num">RTT</th>
          <th className="num">最高高度</th><th>事件</th><th>結束方式</th><th />
        </tr></thead>
        <tbody>
          {rows.map((s) => {
            const avg = s.summary?.avg_sinr;
            const cls = avg != null ? classifySinr(avg) : null;
            const when = new Date(s.started_at).toLocaleString("zh-TW", { hour12: false });
            return (
              <tr key={s.id} className="row-link" title="點擊回放這條航線"
                onClick={() => router.push(`/replay/${s.id}`)}>
                <td>{when}
                  {s.video_mode === "off" &&
                    <span className="meta" style={{ marginLeft: 6 }}>未錄影</span>}
                </td>
                <td>{s.plan_name ?? "—"}</td>
                <td>{duration(s.started_at, s.ended_at)}</td>
                <td className="num">{s.summary?.samples_total ?? "—"}</td>
                <td className="num">
                  {cls && <span className="dw-sdot" style={{ background: cls.color }} />}
                  {fmt(avg)} dB
                </td>
                <td className="num">{fmt(s.summary?.min_sinr)} dB</td>
                <td className="num">{fmt(s.summary?.avg_rtt_ms, 0)} ms</td>
                <td className="num">{fmt(s.summary?.max_alt_rel, 0)} m</td>
                <td className="dw-ev">
                  {s.events_total ?? 0} 則
                  {!!s.events_critical && <b> · {s.events_critical} 危急</b>}
                </td>
                <td>{s.end_reason
                  ? END_LABELS[s.end_reason] ?? s.end_reason
                  : (s.ended_at ? "—" : "進行中")}</td>
                <td onClick={(e) => e.stopPropagation()} className="num">
                  <a className="btn-plain btn-xs" download
                    href={`${API}/api/sessions/${s.id}/export`}
                    title="下載此航線的完整原始資料（JSON）">匯出</a>{" "}
                  <button className="btn-danger btn-xs"
                    title="從資料庫移除此航線（請先匯出）"
                    onClick={() => onRemove(s.id, when)}>移除</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** 欄位編輯 modal（取代 `window.prompt`）：**prompt 放不下說明**，也帶不了
 * 「留空＝清除」這種語意，而影像位址恰恰需要說清楚。 */
function FieldEditor({ label, value, tip, onClose, onSave }: {
  drone: Drone; field: string; label: string; value: string | null; tip: string;
  onClose: () => void; onSave: (v: string) => void;
}) {
  const [v, setV] = useState(value ?? "");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal squad-modal" role="dialog" aria-modal="true"
        aria-label={`編輯${label}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="name">編輯{label}</span>
          <span className="spacer" />
          <InfoTip tip={tip} />
          <button className="modal-close" aria-label="關閉（Esc）" title="關閉（Esc）"
            onClick={onClose}>✕</button>
        </div>
        <div className="squad-form">
          <label className="squad-field">
            <span>{label}</span>
            <input value={v} autoFocus placeholder="留空＝清除"
              onChange={(e) => setV(e.target.value)} />
          </label>
          <div className="hint-line">留空並儲存＝清除這個欄位。</div>
        </div>
        <div className="modal-actions">
          <button className="btn-plain" onClick={onClose}>取消</button>
          <button className="btn-plain" disabled={busy}
            onClick={() => { setBusy(true); onSave(v.trim()); }}>
            {busy ? "儲存中…" : "儲存"}
          </button>
        </div>
      </div>
    </div>
  );
}
