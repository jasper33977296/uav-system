"use client";
import { useEffect } from "react";

import { API, WS_URL } from "./signal";
import { useUavStore } from "./store";

/** 連上 backend 的 telemetry WebSocket，自動重連。 */
export function useTelemetry() {
  const { setLive, setWsConnected, pushEvent, seedEvents, setRegistry } = useUavStore();

  // 事件流開頁先補歷史：WS 只送「開頁之後」的事件，中途開頁會漏掉先前的
  // 轉換（實際發生過：飛行中開頁，degraded/lost 都發過了，畫面卻是「尚無事件」）。
  useEffect(() => {
    fetch(`${API}/api/events?limit=20`)
      .then((r) => r.json())
      .then((rows) =>
        seedEvents(
          rows.map((e: any) => {
            // REST 路徑的 detail 是 JSONB 字串，WS 路徑是物件；統一成物件。
            // **逐列 try/catch，不能讓一列毀掉整批**：原本 JSON.parse 在 map
            // 裡拋出→被下方 catch 吞掉→seedEvents 從未執行→**整段歷史消失、
            // 畫面顯示「尚無事件」**。空事件流是本 UI 最強的安心宣告
            // （＝沒有異常發生），靜默失效會讓故障中的飛機看起來健康。
            // 壞掉的那列也不丟：原文照原樣留在 text，並標記 parse_failed
            // 讓 modal 的鍵值表看得到——**看得懂的錯誤好過看不見的沉默**
            if (typeof e.detail !== "string") return e;
            try {
              return { ...e, detail: JSON.parse(e.detail) };
            } catch {
              return { ...e, detail: { text: e.detail, parse_failed: true } };
            }
          })
        )
      )
      .catch(() => {});
  }, [seedEvents]);

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
        else if (msg.type === "event") pushEvent(msg.event, msg.fold === true);
        // 機上資料 §2.8：訊息登錄表 1–2Hz（014 Phase B，暫定契約形）
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
