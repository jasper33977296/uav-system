"use client";
import { useEffect } from "react";

import { getJson } from "./fetchJson";
import { eventDetail } from "./jsonb";
import { API, WS_URL } from "./signal";
import { useUavStore } from "./store";

/** 連上 backend 的 telemetry WebSocket，自動重連。 */
export function useTelemetry() {
  const { setLive, setWsConnected, pushEvent, seedEvents, setRegistry,
    setAgent, setEventsFailed, pushReplay } = useUavStore();

  // 事件流開頁先補歷史：WS 只送「開頁之後」的事件，中途開頁會漏掉先前的
  // 轉換（實際發生過：飛行中開頁，degraded/lost 都發過了，畫面卻是「尚無事件」）。
  useEffect(() => {
    // getJson：HTTP 4xx/5xx 要拋——否則 500 的錯誤 body 會被當成空清單，
    // 畫面顯示「尚無事件」＝在後端掛掉時宣告飛機一切正常（見 lib/fetchJson.ts）
    getJson<any[]>(`${API}/api/events?limit=20`)
      .then((rows) => {
        // 逐列解析、單列失敗不毀整批（成因與呈現規則見 lib/jsonb.ts）
        seedEvents(rows.map((e: any) => ({ ...e, detail: eventDetail(e.detail) })));
        setEventsFailed(false);
      })
      .catch(() => setEventsFailed(true));
  }, [seedEvents, setEventsFailed]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;

    const connect = () => {
      ws = new WebSocket(WS_URL);
      ws.onopen = () => setWsConnected(true);
      ws.onmessage = (e) => {
        // 單則壞訊息（如後端漏出的裸 NaN——非法 JSON）不炸掉整個 handler
        let msg;
        try { msg = JSON.parse(e.data); } catch { return; }
        if (msg.type === "telemetry") setLive(msg);
        // fold:true＝同句 STATUSTEXT 重複的就地更新（不新增列，7127218 契約）
        else if (msg.type === "event") {
          pushEvent(msg.event, msg.fold === true);
          // **航線換了就重畫**：上傳／改航線完成時後端會發這則事件。
          // 由事件觸發而不是由按鈕觸發，是因為上傳的呼叫端不只有這個畫面
          // （驗收 rig、MCP、curl 都會上傳）——綁在按鈕上等於只有自己按的
          // 那次會更新，別人上傳時畫面就與飛機的事實對不上
          if (msg.event?.type === "mission_shown")
            useUavStore.getState().requestPlanRefresh();
        }
        // 機上資料 §2.8：訊息登錄表 1–2Hz（014 Phase B，暫定契約形）
        // 意圖協定 §4.2 的狀態鏡像（代理 → 地面站，1Hz 保活＋變化即送）
        else if (msg.type === "agent_state") setAgent(msg);
        // 失聯期間按下的操作，恢復後的重算判決（039 複裁 G）。**乾跑，
        // 飛機沒有動**——要不要真的做由人再按一次
        else if (msg.type === "agent_intent_replay")
          pushReplay({ ...msg, at: Date.now() });
        else if (msg.type === "msg_registry" && msg.drone_id)
          setRegistry(msg.drone_id,
            { sensors: msg.sensors ?? [], messages: msg.messages ?? [] });
      };
      ws.onclose = () => {
        setWsConnected(false);
        if (!closed) retry = setTimeout(connect, 2000);
      };
      ws.onerror = () => ws?.close();
    };
    connect();

    return () => {
      closed = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [setLive, setWsConnected, pushEvent]);
}
