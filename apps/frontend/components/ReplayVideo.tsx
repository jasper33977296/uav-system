"use client";
import { useEffect, useMemo, useRef, useState } from "react";

import { API, classifySinr } from "@/lib/signal";

/** 回放影片同步（ui-spec §5.4，022——使用者核准 2026-08-12）。
 *
 * 單一時鐘源＝回放 transport（rows/idx）：影片永遠跟隨時間軸，seek 依
 * 每段絕對時間錨（segment.started_at＝影片第 0 秒的絕對時刻）。
 * 誠實三空態嚴格分句：缺口「此時段無影像（錄製中斷）」／expired
 * 「影像已過保留期（N 天），已清除」／missing＝warn 卡（錄製故障）。
 * 底緣 SINR 色帶＝播放器 UI 疊層（絕不燒進影片檔）；畫質劣化＝研究證據。
 */

export interface VideoSeg {
  id: string; url: string;
  started_at: string;                       // 該段第 0 秒的絕對時間（錨點）
  duration_s: number;
  final?: boolean;                          // false＝長度未定案（仍在處理中）
  codec?: string | null; width?: number | null;   // Phase 2/3 才有值——
  height?: number | null; fps?: number | null;    // null 不畫、不用來源設定值填充
  bytes?: number | null;
}
export interface SessionVideo {
  retention_days: number;
  video_status: "available" | "off" | "no_source" | "expired" | "missing";
  segments: VideoSeg[];
}

interface Row { time: string; sinr?: number | null }

export default function ReplayVideo({ video, rows, tCurMs, playing, speed,
  droneName, droneColor }: {
  video: SessionVideo; rows: Row[]; tCurMs: number;
  playing: boolean; speed: number;
  droneName: string | null; droneColor: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [big, setBig] = useState(false);    // 可切大小（§5.4 同 PiP 語言）

  // 拖曳（與即時 PiP 同語言；獨立記憶 key）
  const boxRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const posRef = useRef(pos);
  useEffect(() => { posRef.current = pos; }, [pos]);
  const clamp = (p: { x: number; y: number }) => {
    const w = boxRef.current?.offsetWidth ?? 280;
    const h = boxRef.current?.offsetHeight ?? 158;
    return {
      x: Math.max(8, Math.min(p.x, window.innerWidth - w - 8)),
      y: Math.max(56, Math.min(p.y, window.innerHeight - h - 8)),
    };
  };
  useEffect(() => {
    try {
      const saved = localStorage.getItem("replay-vid-pos");
      if (saved) setPos(clamp(JSON.parse(saved)));
    } catch { /* 壞值用預設位置 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const drag = useRef<{ dx: number; dy: number; sx: number; sy: number;
    moved: boolean } | null>(null);
  function down(e: React.PointerEvent) {
    const r = boxRef.current!.getBoundingClientRect();
    drag.current = { dx: e.clientX - r.left, dy: e.clientY - r.top,
      sx: e.clientX, sy: e.clientY, moved: false };
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
  }
  function move(e: React.PointerEvent) {
    const d = drag.current;
    if (!d) return;
    if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 5) return;
    d.moved = true;
    setPos(clamp({ x: e.clientX - d.dx, y: e.clientY - d.dy }));
  }
  function up() {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (!d.moved) setBig((b) => !b);        // 點擊＝切大小
    else if (posRef.current) {
      localStorage.setItem("replay-vid-pos", JSON.stringify(posRef.current));
    }
  }

  // 目前時刻所在的段（絕對錨點比對）
  const seg = useMemo(() => video.segments.find((g) => {
    const s = new Date(g.started_at).getTime();
    return tCurMs >= s && tCurMs < s + g.duration_s * 1000;
  }), [video.segments, tCurMs]);

  // §5.4 final=false：長度未定案期間，「還沒算完」與「真的沒錄到」在資料上
  // 無法區分——不可說故障（錄製中斷）也不可說空（無影像），走中性句
  const processing = useMemo(() => {
    if (seg) return false;
    return video.segments.some((g) => {
      const s = new Date(g.started_at).getTime();
      return g.final === false && tCurMs >= s + g.duration_s * 1000;
    });
  }, [video.segments, tCurMs, seg]);

  // 同步引擎：時間軸→影片（seek＝絕對時刻−段錨點；漂移 >0.35s 校正）
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !seg) return;
    const want = (tCurMs - new Date(seg.started_at).getTime()) / 1000;
    if (Math.abs(v.currentTime - want) > 0.35) v.currentTime = Math.max(0, want);
    v.playbackRate = speed;
    if (playing) v.play().catch(() => { /* 使用者未互動前 autoplay 限制 */ });
    else v.pause();
  }, [tCurMs, playing, speed, seg]);

  // 底緣 SINR 同步色帶：UI 疊層（時間軸同刻度），分級色取樣 ≤160 段
  const band = useMemo(() => {
    if (rows.length < 2) return null;
    const n = Math.min(rows.length, 160);
    const step = rows.length / n;
    const stops: string[] = [];
    for (let i = 0; i < n; i++) {
      const r = rows[Math.floor(i * step)];
      const c = r?.sinr != null ? classifySinr(r.sinr).color : "transparent";
      stops.push(`${c} ${((i / n) * 100).toFixed(2)}% ${(((i + 1) / n) * 100).toFixed(2)}%`);
    }
    return `linear-gradient(90deg, ${stops.join(",")})`;
  }, [rows]);

  if (video.video_status === "no_source") return null;
  if (video.video_status === "off") return null;   // header 弱字由回放頁呈現
  if (video.video_status === "missing") {
    return (
      <div className="replay-vid vid-warn">
        ⚠ 應有影像但未錄到（錄製故障）
      </div>
    );
  }
  if (video.video_status === "expired") {
    return (
      <div className="replay-vid vid-empty-state">
        影像已過保留期（{video.retention_days} 天），已清除
      </div>
    );
  }

  // available
  return (
    <div className={`replay-vid ${big ? "vid-big" : ""}`} ref={boxRef}
      title="點擊切換大小／拖曳移動"
      style={pos ? { position: "fixed", left: pos.x, top: pos.y,
        right: "auto", bottom: "auto" } : undefined}
      onPointerDown={down} onPointerMove={move}
      onPointerUp={up} onPointerCancel={up}>
      {seg ? (
        // url 是後端相對路徑（實測 /api/video/segments/{id}/file）——補 API 前綴
        <video ref={videoRef} key={seg.id}
          src={seg.url.startsWith("http") ? seg.url : `${API}${seg.url}`}
          muted playsInline preload="auto" />
      ) : processing ? (
        // 長度未定案：不猜、但也不把未知說成故障（§5.4 中性句）
        <div className="vid-gap vid-proc">影像處理中</div>
      ) : (
        // 涵蓋帶缺口＝真空白：不拼接、不定格假裝連續（§5.4 硬約束成對）
        <div className="vid-gap">此時段無影像（錄製中斷）</div>
      )}
      {/* 機身識別徽章：§2.9/§5.4 明文不得因簡約原則移除 */}
      <div className="video-tile-label">
        <span className="dot" style={{ background: droneColor }} />
        {droneName || "未知機身"}
      </div>
      {band && <div className="vid-band" style={{ background: band }} />}
    </div>
  );
}
