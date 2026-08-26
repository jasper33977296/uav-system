"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import ConfirmModal from "@/components/ConfirmModal";
import { Battery, SignalBars } from "@/components/SimpleHud";
import { buildNeedsNotice, buildNoticeText } from "@/lib/buildInfo";
import { errText, getJson } from "@/lib/fetchJson";
import { parseJsonb } from "@/lib/jsonb";
import { API } from "@/lib/signal";
import { AgentState, useUavStore } from "@/lib/store";

interface Drone {
  id: string; name: string; is_simulated: boolean; is_primary: boolean;
  connection_url: string | null; status: string | null;
  mav_sysid: number | null;
  board_uid?: string | null; flight_sw_version?: string | null;
  airframe_serial?: string | null; model?: string | null;
  video_url: string | null;
  autopilot?: string | null;    // "px4"/"ardupilot"/"unknown"；null＝從未見 MAVLink 心跳
  agent?: AgentState | null;    // 意圖通道現況（/api/drones 帶，之後由 WS 更新）
}

/** 意圖協定的狀態 → 人話。**認不得的原樣顯示**——代理版本比地面站新時，
 * 硬翻成「未知」會讓「協定長出了新狀態」看起來像「壞了」。 */
const STATE_TEXT: Record<string, string> = {
  DISCONNECTED: "飛控無心跳", NOT_READY: "地面未就緒", READY: "地面待命",
  ARMED_GROUND: "已解鎖・在地上", TAKING_OFF: "爬升中",
  FLYING_MISSION: "執行任務中", HOLDING: "空中暫停", RETURNING: "返航中",
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
  mission_name: string | null;
  started_at: string; ended_at: string | null;
  summary: {
    avg_sinr?: number | null; min_sinr?: number | null; avg_rtt_ms?: number | null;
    max_alt_rel?: number | null; samples_total?: number;
  } | null;
  video_mode?: string | null;   // §5.4：off＝未錄影弱字標記（有錄不標）
}

const fmt = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

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
    getJson<any[]>(`${API}/api/sessions`)
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
      {/* §0.2f：版本未知或 dirty 必須**非 hover 可見**——tooltip 是藏起來的，
          用它宣告「我不知道我是誰」等於沒宣告。**也不能放進收合區**：
          我第一版放在「＋ 註冊無人機」的 <details> 裡，元素在、內容量到是空的
          （收合＝不可見），比 tooltip 更糟。正常態不顯示，也不進飛行畫面 */}
      {buildNeedsNotice && <div className="build-notice">{buildNoticeText}</div>}
      {/* 註冊表單已移除（issues/038）：代理一上線就自報身分，機器問得到的東西
          不勞人打字。原本這裡有一段「機是怎麼出現在這裡的」說明，也拿掉了——
          它解釋的是**系統怎麼運作**，不是使用者此刻要做的決定；常駐在清單頂端
          等於每次來都再讀一次已經知道的事。機制寫在 doc/drone-registration.md。
          錯誤訊息留下來：它不是說明，是這一頁剛剛發生的事。 */}
      {err && <div className="form-err">{err}</div>}

      {drones.map((d) => {
        const mine = sessions.filter((s) => s.drone_id === d.id);
        const isLive = live?.drone_id === d.id;
        return (
          <div className="card" key={d.id}>
            {/* 一行全貌（simple-first）：色點(亮=連線/灰=離線)＋名＋訊號格＋
                電池＋N 次。離線機沒有的資訊不畫、不放「—」；徽章全收展開 */}
            <div className="drone-head drone-row" onClick={() => toggleOpen(d.id)}>
              <span className="dot drone-dot" style={{
                background: fleet[d.id]?.connected ? "var(--status-ok)" : "var(--hairline)",
              }} />
              <span className="name">{d.name}</span>
              {fleet[d.id]?.connected && (
                <SignalBars sinr={fleet[d.id]?.link?.sinr} />
              )}
              {fleet[d.id]?.connected && (
                <Battery pct={fleet[d.id]?.battery_pct} plain />
              )}
              {isLive && live?.armed && (
                <span className="chip">
                  <span className="dot" style={{ background: "#d03b3b" }} />飛行中
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
                    title="意圖通道斷了。這是**最後看到**的狀態，不是現在的狀態">
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
              <span className="meta">{mine.length} 次</span>
              <span className="meta">{open[d.id] ? "▾" : "▸"}</span>
            </div>

            {/* 展開＝工作區：徽章＋最近時間＋操作列＋架次表格
                （刪除/匯出安全流程照舊） */}
            {open[d.id] && (<>
            <div className="drone-actions">
              <span className="spacer" />
              <button className="btn-plain btn-sm"
                onClick={() => {
                  const name = window.prompt("新名稱", d.name);
                  if (!name || name === d.name) return;
                  patch(d, { name }, "改名");
                }}>改名</button>{" "}
              {/* 機架序號／型號＝**代理問不到**的東西（issues/038）：板子 UID 是
                  飛控板的身分，換機架時板子會帶著記錄走，此時只有人知道它現在
                  裝在哪台機上。留空是誠實的「未填」，不預設值 */}
              <button className="btn-plain btn-sm"
                title="機身上的序號，用來對應實體機架（板子換機時要更新）"
                onClick={() => {
                  const v = window.prompt("機架序號（留空清除）", d.airframe_serial ?? "");
                  if (v === null) return;
                  patch(d, { airframe_serial: v.trim() }, "更新機架序號");
                }}>機架序號</button>{" "}
              <button className="btn-plain btn-sm"
                title="機架型號，如 X500 V2、S500。代理讀得到的是飛控機型（四旋翼／固定翼），不是這個"
                onClick={() => {
                  const v = window.prompt("型號（留空清除）", d.model ?? "");
                  if (v === null) return;
                  patch(d, { model: v.trim() }, "更新型號");
                }}>型號</button>{" "}
              <button className="btn-plain btn-sm"
                title={"即時影像串流位址（地圖點擊機體開啟）。瀏覽器不支援 RTSP：" +
                  "機上跑 MediaMTX 轉 WHEP，填 http://<機IP>:8889/<路徑>/whep；" +
                  "MJPEG/MP4 位址亦可。留空＝清除。"}
                onClick={async () => {
                  const v = window.prompt(
                    "影像串流位址（WHEP / MJPEG / MP4，留空清除）",
                    d.video_url ?? "http://");
                  if (v == null) return;
                  const res = await fetch(`${API}/api/drones/${d.id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ video_url: v === "http://" ? "" : v }),
                  });
                  if (!res.ok) setErr(errText((await res.json()).detail, "設定失敗"));
                  reload();
                }}>影像{d.video_url ? " ✓" : ""}</button>{" "}
              {!d.is_primary && (
                <button className="btn-plain btn-sm"
                  title="MAVLink 收到的遙測記在這台名下（飛行中無法切換）"
                  onClick={async () => {
                    const res = await fetch(`${API}/api/drones/${d.id}/primary`,
                      { method: "POST" });
                    if (!res.ok) setErr(errText((await res.json()).detail, "切換失敗"));
                    reload();
                  }}>設為主機</button>
              )}{" "}
              <button
                className="btn-danger"
                disabled={isLive}
                title={isLive ? "連線中的無人機無法刪除" : "刪除無人機與其全部資料"}
                onClick={() => setToDelete(d)}
              >
                刪除
              </button>
            </div>

            {/* 資訊膠囊一行（不用・串接）＋最近飛行弱文字獨立行（ui-spec §3） */}
            <div className="badges" style={{ marginTop: 8, marginBottom: 0 }}>
              {apChip(d.autopilot) && <span className="chip">{apChip(d.autopilot)}</span>}
              {d.is_primary && <span className="chip">主機</span>}
              {d.is_simulated && <span className="chip">模擬</span>}
            </div>
            {/* **兩層身分**（issues/038）：上一行是機器每次連線都會覆核的事實，
                下一行是人打的字。視覺上分開，因為可信度不同。 */}
            <div className="hint-line" style={{ marginTop: 6 }}>
              {d.mav_sysid != null ? `sysid ${d.mav_sysid}` : "尚未收到心跳"}
              {d.flight_sw_version ? ` · 韌體 ${d.flight_sw_version}` : ""}
              {d.board_uid
                ? ` · 板子 ${d.board_uid.slice(-8)}`
                : " · 無板子 UID（身分較弱）"}
              {(() => {
                const ag = agents[d.id] ?? d.agent;
                if (!ag) return " · 無機上代理";
                return ` · 代理 ${ag.agent_version ?? "版本未知"}` +
                  (ag.connected ? "" : "（意圖通道斷線）");
              })()}
            </div>
            <div className="hint-line" style={{ marginTop: 2 }}>
              {(d.airframe_serial || d.model)
                ? `機架 ${d.airframe_serial || "未填"} · ${d.model || "型號未填"}`
                : "機架序號與型號未填"}
            </div>
            {mine[0] && (
              <div className="hint-line" style={{ marginTop: 6 }}>
                最近飛行 {new Date(mine[0].started_at).toLocaleString("zh-TW",
                  { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
                    hour12: false })}
              </div>
            )}

            <table className="table" style={{ marginTop: 10 }}>
              <thead>
                <tr>
                  <th>開始時間</th>
                  <th>任務</th>
                  <th>時長</th>
                  <th className="num">樣本數</th>
                  <th className="num">平均 SINR</th>
                  <th className="num">最低 SINR</th>
                  <th className="num">平均 RTT</th>
                  <th className="num">最高高度</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {mine.length === 0 && (
                  <tr><td colSpan={9} className="empty">尚無航線——一次飛行（解鎖到上鎖）＝一條航線紀錄</td></tr>
                )}
                {mine.map((s) => (
                  <tr
                    key={s.id}
                    className="row-link"
                    title="點擊回放這條航線"
                    onClick={() => router.push(`/replay/${s.id}`)}
                  >
                    <td>
                      {new Date(s.started_at).toLocaleString("zh-TW", { hour12: false })}
                      {/* §5.4 opt-out 留痕：未錄影弱字；有錄的不標（正常不宣告） */}
                      {s.video_mode === "off" &&
                        <span className="meta" style={{ marginLeft: 6 }}>未錄影</span>}
                    </td>
                    <td>{s.mission_name ?? "—"}</td>
                    <td>{duration(s.started_at, s.ended_at)}</td>
                    <td className="num">{s.summary?.samples_total ?? "—"}</td>
                    <td className="num">{fmt(s.summary?.avg_sinr)} dB</td>
                    <td className="num">{fmt(s.summary?.min_sinr)} dB</td>
                    <td className="num">{fmt(s.summary?.avg_rtt_ms, 0)} ms</td>
                    <td className="num">{fmt(s.summary?.max_alt_rel, 0)} m</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {/* 匯出＝下載完整 JSON；配合 30 天 retention，要長期保留先匯出 */}
                      <a className="btn-plain btn-sm" href={`${API}/api/sessions/${s.id}/export`}
                         download title="下載此航線的完整原始資料（JSON）">匯出</a>{" "}
                      <button className="btn-danger btn-sm"
                        title="從資料庫移除此航線（請先匯出）"
                        onClick={() => {
                          if (window.confirm(
                            `移除航線 ${new Date(s.started_at).toLocaleString("zh-TW", { hour12: false })}？\n\n` +
                            "將刪除其全部遙測與訊號資料。若尚未匯出，資料將永久遺失。"))
                            fetch(`${API}/api/sessions/${s.id}`, { method: "DELETE" }).then(reload);
                        }}>移除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </>)}
          </div>
        );
      })}
      {drones.length === 0 && (
        <div className="card"><div className="empty">
          {dronesErr ? "無法連線到 backend"
            : dronesLoaded ? "尚無無人機" : "讀取中…"}
        </div></div>
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
            關聯的任務路徑不會被刪除，僅解除與此機的關聯（路徑不綁機）。
            若要長期保留航線資料，請先逐航線「匯出」。
          </p>
        </ConfirmModal>
      )}
    </div>
  );
}
