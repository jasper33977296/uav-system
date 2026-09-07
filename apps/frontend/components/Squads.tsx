"use client";
/** 小隊＝常設編組（doc/squads-design.md，使用者核准 2026-09-08）。
 *
 * **小隊是一份名單，不是任務設定。** 隊形、高度分層、航線一律在派任務時決定
 * ——否則同一個決定會有兩個家，而它們遲早不一致，操作員得先知道「哪個贏」
 * 才敢按。同理，小隊**不影響任何飛安判定**：入列、預檢、守門仍逐機進行。
 *
 * 三個呈現上的規矩：
 *  1. **成員狀態即時 join**：API 只回 drone_id，在線／訊號從 store 拿。
 *     後端回一份狀態快照的話，那份快照一定會過期。
 *  2. **空小隊留著並明說**：成員機被刪光時不自動刪隊——自動消失會讓人以為
 *     自己按錯了。
 *  3. **取得失敗不得長得像「還沒有小隊」**：前者是我方壞了，後者是一個宣告。
 */
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import ConfirmModal from "@/components/ConfirmModal";
import InfoTip from "@/components/InfoTip";
import { errText, getJson } from "@/lib/fetchJson";
import { API } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

export interface SquadMember { drone_id: string; position: number; drone_name: string }
export interface Squad {
  id: string; name: string; note: string | null; created_at: string;
  members: SquadMember[]; last_flight: string | null; flights: number;
}
interface DroneLite { id: string; name: string }

const when = (iso: string) =>
  new Date(iso).toLocaleString("zh-TW", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false });

export default function Squads({ drones }: { drones: DroneLite[] }) {
  const router = useRouter();
  const fleet = useUavStore((s) => s.fleet);
  const [squads, setSquads] = useState<Squad[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<Squad | "new" | null>(null);
  const [toDelete, setToDelete] = useState<Squad | null>(null);

  const load = useCallback(() => {
    getJson<Squad[]>(`${API}/api/squads`)
      .then((r) => { setSquads(r); setErr(null); })
      // 取得失敗說出來——空清單是「還沒有小隊」，那是一個宣告，不是錯誤
      .catch((e) => setErr(errText((e as Error).message, "無法取得小隊")));
  }, []);
  useEffect(load, [load]);

  /** 這台機現在的狀態——與機隊列同一套說法（三種，不多不少）。 */
  const stateOf = (id: string) => {
    const t = fleet[id];
    if (t?.connected && t.armed) return { text: "飛行中", tone: "flying", on: true };
    if (t?.connected) return { text: "在線", tone: "on", on: true };
    return { text: "未連線", tone: "off", on: false };
  };

  const remove = async (s: Squad) => {
    setToDelete(null);
    const r = await fetch(`${API}/api/squads/${s.id}`, { method: "DELETE" });
    if (!r.ok) setErr(errText((await r.json()).detail, "刪除失敗"));
    load();
  };

  /** 派任務：把成員送進編隊模式，跳即時頁的任務控制。
   * **這裡不決定任何隊形或分層**——那些在那一頁。 */
  const dispatch = (s: Squad) => {
    useUavStore.getState().setFormation(true, s.members.map((m) => m.drone_id));
    router.push("/");
  };

  return (
    <>
      <div className="drone-head squad-head">
        <span className="name">小隊{squads?.length ? `（${squads.length}）` : ""}</span>
        <button className="btn-plain btn-sm" onClick={() => setEditing("new")}>
          ＋ 新增小隊
        </button>
        <span className="spacer" />
        <InfoTip tip="小隊＝固定編組。先在這裡組好，派群飛任務時直接選一隊，不必每次重新勾機。一台機可以同時屬於多個小隊。小隊只是名單——隊形、高度分層與航線在派任務時才決定，而入列、預檢、守門仍然逐機判定。" />
      </div>

      {err && <div className="form-err">{err}</div>}
      {!err && squads === null && <div className="empty">載入中…</div>}
      {!err && squads?.length === 0 && (
        <div className="card"><div className="empty">
          還沒有小隊。組一隊之後，派群飛任務時就不必每次重新勾機。
        </div></div>
      )}

      {squads?.map((s) => (
        <div className="card squad" key={s.id}>
          <div className="squad-row">
            <span className="squad-name">{s.name}</span>
            <span className="meta">{s.members.length} 台</span>
            {s.note && <span className="chip">{s.note}</span>}
            <span className="spacer" />
            <button className="btn-plain btn-sm" title="改名稱與成員"
              onClick={() => setEditing(s)}>編輯</button>{" "}
            <button className="btn-plain btn-sm" disabled={!s.members.length}
              title="到即時頁的任務控制指派這一隊（隊形與高度分層在那裡決定）"
              onClick={() => dispatch(s)}>派任務</button>{" "}
            <button className="btn-danger btn-sm"
              onClick={() => setToDelete(s)}>刪除</button>
          </div>
          {s.members.length > 0 ? (
            <div className="squad-members">
              {s.members.map((m) => {
                const st = stateOf(m.drone_id);
                return (
                  <span className="squad-member" key={m.drone_id}>
                    <span className={`dot${st.on ? "" : " drone-dot-off"}`}
                      style={st.on ? { background: "var(--status-ok)" } : undefined} />
                    {m.drone_name}
                    <span className={`drone-state drone-state-${st.tone}`}>{st.text}</span>
                  </span>
                );
              })}
            </div>
          ) : (
            // **不自動刪空隊**：自動消失會讓人以為自己按錯了
            <div className="hint-line">這隊已經沒有成員（機被刪掉時會自動退出名單）。</div>
          )}
          <div className="hint-line">
            {s.last_flight
              ? `上次群飛 ${when(s.last_flight)}　共 ${s.flights} 趟`
              : "還沒有一起飛過"}
          </div>
        </div>
      ))}

      {editing && (
        <SquadEditor
          squad={editing === "new" ? null : editing}
          drones={drones} stateOf={stateOf}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }}
          onError={setErr}
        />
      )}
      {toDelete && (
        <ConfirmModal title={`刪除小隊「${toDelete.name}」？`}
          confirmLabel="刪除小隊"
          onConfirm={() => remove(toDelete)} onClose={() => setToDelete(null)}>
          <div>只會刪掉這個編組，<b>不會動到任何一台機與它們的紀錄</b>。</div>
          <div>已經飛過的群飛紀錄也留著——那些留的是當時的隊名。</div>
        </ConfirmModal>
      )}
    </>
  );
}

/** 新增／編輯：**改名與改成員同一個 modal**（使用者要求 2026-09-08）。 */
function SquadEditor({ squad, drones, stateOf, onClose, onSaved, onError }: {
  squad: Squad | null;
  drones: DroneLite[];
  stateOf: (id: string) => { text: string; tone: string; on: boolean };
  onClose: () => void; onSaved: () => void; onError: (m: string) => void;
}) {
  const [name, setName] = useState(squad?.name ?? "");
  const [note, setNote] = useState(squad?.note ?? "");
  const [members, setMembers] = useState<string[]>(
    squad ? squad.members.map((m) => m.drone_id) : []);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const toggle = (id: string) =>
    setMembers((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);

  const save = async () => {
    setBusy(true); setErr(null);
    const body = JSON.stringify({ name: name.trim(), note: note.trim(), members });
    const r = squad
      ? await fetch(`${API}/api/squads/${squad.id}`,
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body })
      : await fetch(`${API}/api/squads`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body });
    setBusy(false);
    if (!r.ok) {
      // 撞名（409）與「機不存在」（422）後端都給得出人話，原文顯示
      setErr(errText((await r.json()).detail, "儲存失敗"));
      return;
    }
    onSaved();
  };

  const invalid = !name.trim() || members.length === 0;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal squad-modal" role="dialog" aria-modal="true"
        aria-label={squad ? "編輯小隊" : "新增小隊"} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="name">{squad ? `編輯「${squad.name}」` : "新增小隊"}</span>
          <span className="spacer" />
          <InfoTip tip="名稱可以隨時改，改名不影響已經飛過的紀錄（那些留的是當時的隊名）。勾選要編在一起的機；一台機可以同時屬於多個小隊。" />
          <button className="modal-close" aria-label="關閉（Esc）" title="關閉（Esc）"
            onClick={onClose}>✕</button>
        </div>
        <div className="squad-form">
          <label className="squad-field">
            <span>小隊名稱</span>
            <input value={name} autoFocus placeholder="例如：低速測線隊"
              onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="squad-field">
            <span>用途（選填）</span>
            <input value={note} placeholder="一句話，例如：低速測線用"
              onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="squad-field">
            <span>成員（{members.length} 台）</span>
            <div className="squad-pick">
              {drones.map((d) => {
                const on = members.includes(d.id);
                const st = stateOf(d.id);
                return (
                  <button key={d.id} className="squad-pickrow"
                    aria-pressed={on} onClick={() => toggle(d.id)}>
                    <span className="squad-box">{on ? "✓" : ""}</span>
                    <span className={`dot${st.on ? "" : " drone-dot-off"}`}
                      style={st.on ? { background: "var(--status-ok)" } : undefined} />
                    <span className="squad-pickname">{d.name}</span>
                    <span className="spacer" />
                    <span className={`drone-state drone-state-${st.tone}`}>{st.text}</span>
                  </button>
                );
              })}
              {!drones.length && <div className="empty">還沒有註冊過任何無人機。</div>}
            </div>
          </div>
          {err && <div className="form-err">{err}</div>}
          <div className="hint-line">
            小隊只是名單——隊形、高度分層與航線在派任務時才決定。
          </div>
        </div>
        <div className="modal-actions">
          <button className="btn-plain" onClick={onClose}>取消</button>
          <button className="btn-plain" disabled={invalid || busy} onClick={save}
            title={invalid ? "要有名字，而且至少一台機" : undefined}>
            {busy ? "儲存中…" : squad ? "儲存" : "建立"}
          </button>
        </div>
      </div>
    </div>
  );
}
