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

interface Mission { id: string; name: string }

export default function MissionPrompt() {
  const live = useUavStore((s) => s.live);
  const sessionId = live?.session_id ?? null;
  const prev = useRef<string | null | undefined>(undefined);
  const [ask, setAsk] = useState<"start" | "end" | null>(null);
  const [active, setActive] = useState<Mission | null>(null);
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadActive = () => getJson<Mission | null>(`${API}/api/missions/active`)
    .then(setActive).catch(() => setActive(null));
  useEffect(() => { loadActive(); }, []);

  useEffect(() => {
    const before = prev.current;
    prev.current = sessionId;
    // 開頁的第一次觀察不算轉換——那不是一次起飛
    if (before === undefined) return;
    if (!before && sessionId) {
      // 起飛：沒有進行中的任務才問。有的話後端已經把這一趟接進去了
      getJson<Mission | null>(`${API}/api/missions/active`).then((m) => {
        setActive(m);
        if (!m) { setName(""); setErr(null); setAsk("start"); }
      }).catch(() => { /* 問不到就不問，不擋飛行 */ });
    } else if (before && !sessionId) {
      // 落地：**問，不自動關**
      getJson<Mission | null>(`${API}/api/missions/active`).then((m) => {
        setActive(m);
        if (m) { setErr(null); setAsk("end"); }
      }).catch(() => {});
    }
  }, [sessionId]);

  if (!ask) return null;

  const create = async () => {
    const n = name.trim();
    if (!n) return;
    setBusy(true); setErr(null);
    const r = await fetch(`${API}/api/missions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: n }),
    });
    const b = await r.json().catch(() => null);
    setBusy(false);
    if (!r.ok) { setErr(errText(b?.detail, "建立失敗")); return; }
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
              取一個名字，接下來的每一趟都會自動歸到它底下，直到你把它結束。
            </div>
            <input className="msearch" autoFocus value={name} style={{ width: "100%" }}
              placeholder="例如：低速測線實驗"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) create(); }} />
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
              onClick={create}>{busy ? "建立中…" : "建立並開始"}</button>
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
