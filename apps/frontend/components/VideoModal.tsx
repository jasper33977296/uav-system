"use client";
/** 即時影像 modal：點擊地圖上的無人機開啟。
 *
 * 兩種檢視：單機（點的那台）與影像牆（所有已設定 video_url 的機，
 * 各自獨立串流）。影像來源是每台機的 video_url（無人機頁設定）。
 * 瀏覽器不支援 RTSP——機上跑 MediaMTX 轉 WHEP 延遲最低；
 * MJPEG／MP4 位址亦可直接播放（播放器見 VideoPlayer.tsx）。
 */
import { useEffect, useState } from "react";

import VideoPlayer from "@/components/VideoPlayer";
import { colorFor } from "@/components/droneLayer";
import { API } from "@/lib/signal";

interface Props {
  droneId: string;
  name: string;
  color: string;
  onClose: () => void;
}

interface DroneVideo { id: string; name: string; video_url: string | null }

export default function VideoModal({ droneId, name, color, onClose }: Props) {
  const [drones, setDrones] = useState<DroneVideo[] | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    fetch(`${API}/api/drones`)
      .then((r) => r.json())
      .then(setDrones)
      .catch((e) => setLoadErr(String(e)));
  }, []);

  const url = drones?.find((d) => d.id === droneId)?.video_url ?? null;
  const withVideo = (drones ?? []).filter((d) => d.video_url);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className={`modal video-modal ${showAll ? "video-modal-wide" : ""}`}
        onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          {showAll ? (
            <span className="name">影像牆（{withVideo.length} 台）</span>
          ) : (
            <>
              <span className="dot" style={{ background: color }} />
              <span className="name">{name}</span>
              <span className="meta">即時畫面</span>
            </>
          )}
          <span className="spacer" />
          {withVideo.length > 1 && (
            <button className="btn-plain btn-sm" onClick={() => setShowAll(!showAll)}>
              {showAll ? "單機" : `全部影像（${withVideo.length}）`}
            </button>
          )}{" "}
          <button className="btn-plain btn-sm" onClick={onClose}>關閉 Esc</button>
        </div>

        {showAll ? (
          <div className="video-grid">
            {withVideo.map((d) => (
              <div className="video-tile" key={d.id}>
                <div className="video-tile-label">
                  <span className="dot" style={{ background: colorFor(d.id) }} />
                  {d.name}
                </div>
                <VideoPlayer url={d.video_url!} />
              </div>
            ))}
          </div>
        ) : (
          <div className="modal-body">
            {drones === null && !loadErr && <div className="video-empty">連線中…</div>}
            {loadErr && <div className="video-empty"><p>無法取得機清單：{loadErr}</p></div>}
            {drones !== null && !url && (
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
            {url && <VideoPlayer url={url} />}
          </div>
        )}

        {!showAll && url && <div className="modal-foot meta">{url}</div>}
      </div>
    </div>
  );
}
