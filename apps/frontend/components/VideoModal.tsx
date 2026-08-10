"use client";
/** 即時影像 modal：點擊地圖上的無人機開啟。
 *
 * 影像來源是每台機的 video_url（無人機頁設定，系統端管理，同改名）。
 * 依 URL 型態選播放器——瀏覽器不吃 RTSP，所以：
 *   - 路徑含 /whep    → WebRTC WHEP（MediaMTX 可把 RTSP 轉出，延遲最低）
 *   - 含 mjpeg/mjpg   → <img>（IP cam 常見，原生支援）
 *   - 其他            → <video>（MP4/WebM 直連）
 * 未設定則顯示指引，不擋操作。
 */
import { useEffect, useRef, useState } from "react";

import { API } from "@/lib/signal";

interface Props {
  droneId: string;
  name: string;
  color: string;
  onClose: () => void;
}

type Mode = "loading" | "none" | "whep" | "mjpeg" | "video" | "error";

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

export default function VideoModal({ droneId, name, color, onClose }: Props) {
  const [mode, setMode] = useState<Mode>("loading");
  const [url, setUrl] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let pc: RTCPeerConnection | null = null;
    let cancelled = false;
    (async () => {
      try {
        const drones = await (await fetch(`${API}/api/drones`)).json();
        const u: string | null =
          drones.find((d: { id: string }) => d.id === droneId)?.video_url ?? null;
        if (cancelled) return;
        setUrl(u);
        if (!u) { setMode("none"); return; }
        if (/\/whep\b/i.test(u)) {
          setMode("whep");
          pc = new RTCPeerConnection();
          pc.ontrack = (e) => {
            if (videoRef.current) videoRef.current.srcObject = e.streams[0];
          };
          await startWhep(u, pc);
        } else if (/mjpe?g/i.test(u)) {
          setMode("mjpeg");
        } else {
          setMode("video");
        }
      } catch (e) {
        if (!cancelled) { setMode("error"); setErr(String(e)); }
      }
    })();
    return () => { cancelled = true; pc?.close(); };
  }, [droneId]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal video-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="dot" style={{ background: color }} />
          <span className="name">{name}</span>
          <span className="meta">即時畫面</span>
          <span className="spacer" />
          <button className="btn-plain btn-sm" onClick={onClose}>關閉 Esc</button>
        </div>
        <div className="modal-body">
          {mode === "loading" && <div className="video-empty">連線中…</div>}
          {mode === "none" && (
            <div className="video-empty">
              <p>這台機尚未設定影像串流位址。</p>
              <p className="hint-line">
                到「無人機」頁按「影像」設定 video_url。瀏覽器不支援 RTSP——
                機上（或地面站）跑 MediaMTX 把 RTSP 轉 WHEP，
                填 <code>http://&lt;機IP&gt;:8889/&lt;路徑&gt;/whep</code>（延遲最低）；
                MJPEG／MP4 位址亦可直接播放。
              </p>
            </div>
          )}
          {mode === "error" && (
            <div className="video-empty">
              <p>串流連線失敗：{err}</p>
              <p className="hint-line">{url}</p>
            </div>
          )}
          {(mode === "whep" || mode === "video") && (
            <video
              ref={videoRef}
              src={mode === "video" ? url ?? undefined : undefined}
              autoPlay muted playsInline controls
              onError={() => { setMode("error"); setErr("video 元素無法播放此來源"); }}
            />
          )}
          {mode === "mjpeg" && url && (
            /* eslint-disable-next-line @next/next/no-img-element */
            <img src={url} alt="即時畫面（MJPEG）"
              onError={() => { setMode("error"); setErr("MJPEG 串流無法載入"); }} />
          )}
        </div>
        {url && <div className="modal-foot meta">{url}</div>}
      </div>
    </div>
  );
}
