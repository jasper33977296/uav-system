"use client";
/** 單一影像串流播放器（可複用：單機 modal 與多機影像牆共用）。
 * 依 URL 型態選播放器：mjpeg → <img>、/whep → WebRTC、其他 → <video>。
 *
 * **即時頁現在走 MJPEG**（2026-10-01 使用者裁定）。原本是 WHEP，而 WebRTC 把
 * H.264 原封送進瀏覽器——上游破一個 NAL 就整個解不出來，畫面一片黑（當天
 * 5G 換網段後就是這樣）。MJPEG 在地面站先解碼再重編，破影格會變成「有花但
 * 看得到」。延遲多約 0.1–0.3 秒，換到的是壞掉時會降級而不是消失。
 * WHEP 的分支留著：有人把播放位址手動填成 whep 時仍然播得動。 */
import { useEffect, useRef, useState } from "react";

import { ageText } from "@/lib/staleness";

type Mode = "whep" | "mjpeg" | "video" | "error";

/** 超過這麼久沒有新影格＝凍住。地面站轉碼約 10–15 fps，正常的空檔遠小於 1 秒 */
const FROZEN_S = 3;

/** 地面站自己的 MJPEG（`/api/drones/{id}/camera/stream.mjpg`）→ 它的狀態端點。
 * 狀態端點**不會因為問就把轉碼打開**（api.py camera_status），可以放心輪詢 */
function statusUrlFor(url: string): string | null {
  const m = url.match(/^(.*\/api\/drones\/[^/]+\/camera)\/stream\.mjpg\b/);
  return m ? m[1] : null;
}

async function startWhep(url: string, pc: RTCPeerConnection): Promise<void> {
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  await pc.setLocalDescription(await pc.createOffer());
  // 非 trickle：等 ICE 蒐集完一次送出（MediaMTX/WHEP 標準作法）
  if (pc.iceGatheringState !== "complete") {
    await new Promise<void>((res) => {
      const check = () => {
        if (pc.iceGatheringState === "complete") {
          pc.removeEventListener("icegatheringstatechange", check);
          res();
        }
      };
      pc.addEventListener("icegatheringstatechange", check);
    });
  }
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: pc.localDescription!.sdp,
  });
  if (!res.ok) throw new Error(`WHEP ${res.status}`);
  await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
}

export default function VideoPlayer({ url, controls = true }: {
  url: string;
  controls?: boolean;   // PiP 小窗關閉原生控制條（窗身整面拖曳，§2.9）
}) {
  const [mode, setMode] = useState<Mode>(
    /\/whep\b/i.test(url) ? "whep" : /mjpe?g/i.test(url) ? "mjpeg" : "video");
  const [err, setErr] = useState<string | null>(null);
  // 第一個影格到達前是一片黑。**黑畫面與「壞掉了」長得一模一樣**，而這裡
  // 的等待是正常的：機上相機只在有人看的時候才開（issues/022 的拉流設計），
  // 所以每次開啟／展開小窗都要等「地面站去拉 → 機上開相機 → libcamera
  // 初始化 → 第一個關鍵影格」，實測約 8 秒。不說的話使用者只會看到壞掉。
  const [live, setLive] = useState(false);
  const [waited, setWaited] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  // **凍住的畫面要拿掉，不能留著**（使用者 2026-10-19：斷線時即時畫面要清掉，
  // 避免誤解還連著）。`<img>` 收 multipart 串流時，影格不來了就停在最後一張，
  // 不報錯、不觸發任何事件——和「正常但鏡頭前沒東西在動」完全分不出來。
  // null＝沒凍住；數字＝最後一張是幾秒前（null 年齡＝問不到，也當凍住）
  const [frozen, setFrozen] = useState<{ age: number | null } | null>(null);

  useEffect(() => {
    if (mode !== "whep") return;
    const pc = new RTCPeerConnection();
    pc.ontrack = (e) => {
      if (videoRef.current) videoRef.current.srcObject = e.streams[0];
    };
    startWhep(url, pc).catch((e) => { setMode("error"); setErr(String(e)); });
    return () => pc.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  // 等待秒數：**只用來換句話說，不用來宣告失敗**。等久了不等於連不上
  // （機上可能正在開相機），所以超時只是把「已經等了多久」講出來。
  useEffect(() => {
    if (live || mode === "error") return;
    const t = setInterval(() => setWaited((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [live, mode]);

  useEffect(() => { setLive(false); setWaited(0); setFrozen(null); }, [url]);

  // MJPEG：問地面站最後一張是幾秒前。**問不到也算凍住**——問不到的時候
  // 影像那條路多半也斷了，畫面上那張就是殘影
  useEffect(() => {
    const st = mode === "mjpeg" && live ? statusUrlFor(url) : null;
    if (!st) return;
    let stop = false;
    const poll = async () => {
      let age: number | null = null;
      try {
        const r = await fetch(st, { cache: "no-store", signal: AbortSignal.timeout(2500) });
        if (r.ok) age = (await r.json()).last_frame_age_s ?? null;
      } catch { /* 問不到 */ }
      if (!stop) setFrozen(age != null && age <= FROZEN_S ? null : { age });
    };
    poll();
    const t = setInterval(poll, 1500);
    return () => { stop = true; clearInterval(t); };
  }, [mode, live, url]);

  // <video>（WHEP 等）：播放時間不再前進＝凍住
  useEffect(() => {
    if (mode === "mjpeg" || mode === "error" || !live) return;
    let last = -1, since = Date.now();
    const t = setInterval(() => {
      const v = videoRef.current;
      if (!v) return;
      // 人自己按了暫停、或檔案播完了，不是中斷
      if (v.currentTime !== last || v.paused || v.ended) {
        last = v.currentTime; since = Date.now();
      }
      const age = (Date.now() - since) / 1000;
      setFrozen(age > FROZEN_S ? { age } : null);
    }, 1000);
    return () => clearInterval(t);
  }, [mode, live]);

  const frozenOverlay = frozen && (
    <div className="video-connecting">
      <p>影像中斷——{frozen.age != null
        ? `最後一張是 ${ageText(frozen.age)}` : "問不到地面站的影像狀態"}</p>
      <p className="hint-line">畫面已拿掉，避免把最後一張當成即時；恢復後會自己接上</p>
    </div>
  );

  if (mode === "error") {
    return (
      <div className="video-empty">
        <p>串流連線失敗：{err}</p>
        <p className="hint-line">{url}</p>
      </div>
    );
  }
  if (mode === "mjpeg") {
    return (
      <>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {/* **不給瀏覽器原生拖曳**（2026-10-13）：`<img>` 預設可拖，而那個原生
            拖曳會把指標捕捉取消掉（pointercancel），於是 PiP 小窗自己的拖曳
            當場失效——使用者回報「縮小畫面沒辦法拖拉」。換成 MJPEG 之前是
            `<video>`，沒有這個預設行為，所以是換法帶進來的回歸。 */}
        <img src={url} alt="即時畫面（MJPEG）" draggable={false}
          style={frozen ? { visibility: "hidden" } : undefined}
          onLoad={() => setLive(true)}
          onError={() => { setMode("error"); setErr("MJPEG 串流無法載入"); }} />
        {frozenOverlay}
        {!live && (
          <div className="video-connecting">
            <span className="spin" />
            <p>連線中…{waited >= 5 && `（已等 ${waited} 秒）`}</p>
            {waited >= 12 && (
              <p className="hint-line">機上相機只在有人看的時候才開，開機要幾秒</p>
            )}
          </div>
        )}
      </>
    );
  }
  return (
    <>
      <video
        ref={videoRef}
        src={mode === "video" ? url : undefined}
        autoPlay muted playsInline controls={controls}
        style={frozen ? { visibility: "hidden" } : undefined}
        onLoadedData={() => setLive(true)}
        onPlaying={() => setLive(true)}
        onError={() => { setMode("error"); setErr("video 元素無法播放此來源"); }}
      />
      {frozenOverlay}
      {!live && (
        <div className="video-connecting">
          <span className="spin" />
          <p>連線中…{waited >= 5 && `（已等 ${waited} 秒）`}</p>
          {waited >= 12 && (
            <p className="hint-line">機上相機只在有人看的時候才開，開機要幾秒</p>
          )}
        </div>
      )}
    </>
  );
}
