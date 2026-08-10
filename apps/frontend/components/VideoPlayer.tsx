"use client";
/** 單一影像串流播放器（可複用：單機 modal 與多機影像牆共用）。
 * 依 URL 型態選播放器：/whep → WebRTC、mjpeg → <img>、其他 → <video>。 */
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

export default function VideoPlayer({ url }: { url: string }) {
  const [mode, setMode] = useState<Mode>(
    /\/whep\b/i.test(url) ? "whep" : /mjpe?g/i.test(url) ? "mjpeg" : "video");
  const [err, setErr] = useState<string | null>(null);
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
      /* eslint-disable-next-line @next/next/no-img-element */
      <img src={url} alt="即時畫面（MJPEG）"
        onError={() => { setMode("error"); setErr("MJPEG 串流無法載入"); }} />
    );
  }
  return (
    <video
      ref={videoRef}
      src={mode === "video" ? url : undefined}
      autoPlay muted playsInline controls
      onError={() => { setMode("error"); setErr("video 元素無法播放此來源"); }}
    />
  );
}
