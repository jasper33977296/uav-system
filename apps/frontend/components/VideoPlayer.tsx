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

type Mode = "whep" | "mjpeg" | "video" | "error";

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

  useEffect(() => { setLive(false); setWaited(0); }, [url]);

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
        <img src={url} alt="即時畫面（MJPEG）"
          onLoad={() => setLive(true)}
          onError={() => { setMode("error"); setErr("MJPEG 串流無法載入"); }} />
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
        onLoadedData={() => setLive(true)}
        onPlaying={() => setLive(true)}
        onError={() => { setMode("error"); setErr("video 元素無法播放此來源"); }}
      />
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
