"use client";
/** 資訊頁（2026-09-07 使用者指示：把「錄製」擴充成資訊頁）。
 *
 * > 使用者原話：**「資訊頁要能在網頁端就看到所有歷史資訊」**
 *
 * 在這之前，歷史散在三個地方：無人機頁有每台機的架次表、錄製頁有檔案、
 * 比較頁有場域訊號圖；而事件與指令**在網頁上根本看不到歷史**——要查得
 * 進資料庫下 SQL。於是「上禮拜那趟到底發生什麼事」這種問題，答案在系統裡
 * 卻不在畫面上。
 *
 * ## 三個分頁，順序＝回想一件事的順序
 *
 * | 分頁 | 回答的問題 | 骨架 |
 * |---|---|---|
 * | **架次** | 那一趟發生了什麼？ | 一趟飛行（事件／指令／錄製都掛在它下面）|
 * | **事件** | 那件事是什麼時候開始的？ | 時間（跨架次往回翻）|
 * | **錄製與回傳** | 那些資料我拿得到嗎？ | 檔案 |
 *
 * **為什麼事件要有自己的分頁**：實測資料庫裡兩萬則事件，掛得上架次的不到
 * 一百則——大部分事情發生在還沒解鎖的時候（預檢擋下、遙控器離線、sysid
 * 撞號）。只做「這一趟的事件」等於把最常查的那些藏起來。
 *
 * **分頁選擇進網址**（`?tab=`）：重整不跳回第一頁，也貼得出連結。
 */
import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import InfoCaptures from "@/components/InfoCaptures";
import InfoEvents from "@/components/InfoEvents";
import InfoFlights from "@/components/InfoFlights";
import InfoTip from "@/components/InfoTip";
import { type DroneRow } from "@/components/InfoShared";
import { getJson } from "@/lib/fetchJson";
import { API } from "@/lib/signal";

const TABS = [
  { key: "flights", label: "架次", hint: "一趟飛行的全部：指令、事件、錄製涵蓋" },
  { key: "events", label: "事件", hint: "跨架次往回翻，可篩機／嚴重度／型別" },
  { key: "captures", label: "錄製與回傳", hint: "機上與地面站兩層的檔案" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

function InfoPage() {
  const router = useRouter();
  const params = useSearchParams();
  const raw = params.get("tab");
  const tab: TabKey = TABS.some((t) => t.key === raw) ? (raw as TabKey) : "flights";
  const setTab = useCallback((k: TabKey) => {
    router.replace(k === "flights" ? "/info" : `/info?tab=${k}`, { scroll: false });
  }, [router]);

  // 機名清單三個分頁都要（篩選下拉、事件列的機名）。**取不到就空陣列**：
  // 少一個下拉是不便，不該讓整頁不能用——各分頁自己的資料另外報錯
  const [drones, setDrones] = useState<DroneRow[]>([]);
  useEffect(() => {
    getJson<DroneRow[]>(`${API}/api/drones`).then(setDrones).catch(() => setDrones([]));
  }, []);

  return (
    <div className="page-pad">
      <div className="drone-head">
        <span className="name">資訊</span>
        <InfoTip tip="這套系統記得的每一趟飛行、每一則事件、每一個檔案。三個分頁的順序＝回想一件事的順序：先找那一趟（架次）、再找那件事什麼時候開始的（事件）、最後看檔案還在不在（錄製與回傳）。" />
      </div>

      <div className="info-tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key}
            title={t.hint} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "flights" && <InfoFlights drones={drones} />}
      {tab === "events" && <InfoEvents drones={drones} />}
      {tab === "captures" && <InfoCaptures />}
    </div>
  );
}

export default function Page() {
  // useSearchParams 需要 Suspense 邊界（Next.js App Router 靜態預渲染要求）
  return (
    <Suspense fallback={<div className="page-pad"><div className="empty">載入中…</div></div>}>
      <InfoPage />
    </Suspense>
  );
}
