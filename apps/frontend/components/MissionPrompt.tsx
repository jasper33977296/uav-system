"use client";
/** 任務的生命週期（doc/mission-vs-plan-design.md §4.5）。
 *
 * **起飛時問一次，落地時問要不要結束。**
 *
 * §4.1② 那條「指派是人做的，系統不猜」沒有變——變的是問的時機：與其飛完之後
 * 回頭一趟一趟指，不如在起飛那一刻問，之後同一個任務底下的每一趟由後端自動
 * 接手（`create_session` 會把進行中的那個任務寫進去）。
 *
 * 三條規矩：
 *  1. **只在真的發生轉換時問**（`session_id` 由無變有 / 由有變無）。開頁時
 *     已經在飛的那一趟不算——那不是一次起飛，而且每次重整都跳一個 modal
 *     會讓人開始無腦按掉。
 *  2. **落地不自動結束任務**：一個任務本來就可以有多趟，所以問、不自動關。
 *  3. **問不到就不擋**：任務是紀錄，不是飛行的前置條件。取消掉照樣飛，
 *     事後在資訊頁補歸。
 */
import { useEffect, useRef, useState } from "react";

import { errText, getJson } from "@/lib/fetchJson";
import { API } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

interface Mission { id: string; name: string; external?: boolean }
interface Squad { id: string; name: string; members: { drone_id: string }[] }

/** 任務名稱的預設值。**與外部起飛的自動命名同一個形狀**（`missions._pick_name`）：
 *  `<路徑名> MM-DD HH:MM`；不知道路徑就用「任務」。撞名時後端會說，改一下就好。 */
function autoName(plan: string | null): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${plan || "任務"} ${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function MissionPrompt() {
  const live = useUavStore((s) => s.live);
  const fleet = useUavStore((s) => s.fleet);
  const sessionId = live?.session_id ?? null;
  const droneId = live?.drone_id ?? null;
  const prev = useRef<string | null | undefined>(undefined);
  const [ask, setAsk] = useState<"start" | "end" | null>(null);
  const [active, setActive] = useState<Mission | null>(null);
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 誰要跑這個任務（§4.6）。**可以綁小隊，也可以綁單台**——綁小隊是活的連結
  const [squads, setSquads] = useState<Squad[]>([]);
  /** 畫面上正在顯示的那條路徑的名字，拿來當任務名稱的預設值 */
  const planName = useRef<string | null>(null);
  useEffect(() => {
    getJson<{ name?: string }>(`${API}/api/plans/active`)
      .then((p) => { planName.current = p?.name ?? null; })
      .catch(() => { planName.current = null; });   // 問不到就退回「任務 MM-DD HH:MM」
  }, [sessionId]);
  const [squadId, setSquadId] = useState<string>("");
  const [crew, setCrew] = useState<string[]>([]);

  /** 這台機參與中的那個任務。**恰好一個**是資料庫的不變式保證的
   *  （一台機一次只能執行一個任務），所以取 [0] 是安全的。 */
  const activeOf = (did: string | null) =>
    getJson<Mission[]>(`${API}/api/missions/active${did ? `?drone_id=${did}` : ""}`)
      .then((r) => r[0] ?? null);

  const loadActive = () => activeOf(droneId).then(setActive).catch(() => setActive(null));
  useEffect(() => { loadActive(); }, [droneId]);
  useEffect(() => {
    getJson<Squad[]>(`${API}/api/squads`).then(setSquads).catch(() => setSquads([]));
  }, []);

  useEffect(() => {
    const before = prev.current;
    prev.current = sessionId;
    // 開頁的第一次觀察不算轉換——那不是一次起飛
    if (before === undefined) return;
    if (!before && sessionId) {
      // 起飛：**這台機**沒有參與中的任務才問。有的話後端已經接手了
      activeOf(droneId).then((m) => {
        setActive(m);
        if (!m) {
          // **名稱預先填好**（issues/061，使用者 2026-09-23）：外部起飛不給名稱時
          // 用「<路徑名> MM-DD HH:MM」，畫面這條路以前一律空白要人自己想——
          // 同一件事兩個入口產生的任務長得不一樣。直接按確定就與外部一致，
          // 要取名字也還可以改
          setName(autoName(planName.current)); setErr(null); setSquadId("");
          // 預設帶**當下連線中的機**——那是「這次誰要飛」最可能的答案
          setCrew(Object.entries(fleet)
            .filter(([, t]) => t.connected).map(([id]) => id));
          setAsk("start");
        }
      }).catch(() => { /* 問不到就不問，不擋飛行 */ });
    } else if (before && !sessionId) {
      // 落地：**問，不自動關**。問的是**這台機所屬的那個任務**
      activeOf(droneId).then((m) => {
        setActive(m);
        // 外部控制端建立的任務在最後一台上鎖 3 秒後自己結束，不必問
        if (m && !m.external) { setErr(null); setAsk("end"); }
      }).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  if (!ask) return null;

  const create = async () => {
    const n = name.trim();
    if (!n) return;
    setBusy(true); setErr(null);
    const r = await fetch(`${API}/api/missions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: n, squad_id: squadId || null, drones: crew }),
    });
    const b = await r.json().catch(() => null);
    setBusy(false);
    // 撞名、或「一台機一次只能執行一個任務」，後端都給得出人話（說得出是哪一台、
    // 撞到哪兩個任務），原文顯示
    if (!r.ok) { setErr(errText(b?.detail?.msg ?? b?.detail, "建立失敗")); return; }
    // **這一趟要補歸**：後端建立架次時還沒有這個任務
    if (sessionId) {
      await fetch(`${API}/api/sessions/${sessionId}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mission_id: b.id }),
      }).catch(() => {});
    }
    await loadActive();
    setAsk(null);
  };

  const end = async () => {
    if (!active) { setAsk(null); return; }
    setBusy(true); setErr(null);
    const r = await fetch(`${API}/api/missions/${active.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ended: true }),
    });
    setBusy(false);
    if (!r.ok) {
      const b = await r.json().catch(() => null);
      setErr(errText(b?.detail, "結束失敗")); return;
    }
    setActive(null);
    setAsk(null);
  };

  return (
    <div className="modal-backdrop" onClick={() => setAsk(null)}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="name">
            {ask === "start" ? "這一趟屬於哪個任務？" : `任務「${active?.name}」結束了嗎？`}
          </span>
          <span className="spacer" />
          <button className="modal-close" aria-label="關閉" title="關閉"
            onClick={() => setAsk(null)}>✕</button>
        </div>
        <div className="modal-text">
          {ask === "start" ? (<>
            <div className="hint-line">
              取一個名字，這一趟和這幾台機接下來的每一趟都會歸到它底下，直到你把它結束。
            </div>
            <input className="msearch" autoFocus value={name} style={{ width: "100%" }}
              placeholder="例如：低速測線實驗"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) create(); }} />
            {/* 誰要跑：綁一整隊（活的連結，小隊改成員任務跟著變）或勾單台。
                兩種可以並用——有效名單是聯集 */}
            <div className="cmp-scope">
              <span className="hint-line">小隊</span>
              <select value={squadId} onChange={(e) => setSquadId(e.target.value)}>
                <option value="">不綁小隊</option>
                {squads.map((q) => (
                  <option key={q.id} value={q.id}>{q.name}（{q.members.length} 台）</option>
                ))}
              </select>
            </div>
            <div className="sess-pills">
              {Object.entries(fleet).map(([id, t]) => (
                <button key={id}
                  className={`pill${crew.includes(id) ? " on" : ""}`}
                  onClick={() => setCrew((c) =>
                    c.includes(id) ? c.filter((x) => x !== id) : [...c, id])}>
                  {t.drone_name || id.slice(0, 6)}
                </button>
              ))}
            </div>
            <div className="hint-line">
              一台機一次只能執行一個任務——已經在別的任務裡的會被擋下來，
              訊息會說是哪一台。
            </div>
          </>) : (
            <div className="hint-line">
              {/* 落地只是這一趟結束，不是任務結束——一個任務可以有多趟 */}
              落地了。如果還要再飛，選「還要再飛」——下一趟會繼續歸到這個任務。
            </div>
          )}
          {err && <div className="form-err">{err}</div>}
        </div>
        <div className="modal-actions">
          {ask === "start" ? (<>
            <button className="btn-plain" onClick={() => setAsk(null)}>先不指定</button>
            <button className="btn-plain" disabled={!name.trim() || busy}
              onClick={create}>{busy ? "建立中…" : "建立任務"}</button>
          </>) : (<>
            <button className="btn-plain" onClick={() => setAsk(null)}>還要再飛</button>
            <button className="btn-plain" disabled={busy} onClick={end}>
              {busy ? "結束中…" : "結束任務"}
            </button>
          </>)}
        </div>
      </div>
    </div>
  );
}
