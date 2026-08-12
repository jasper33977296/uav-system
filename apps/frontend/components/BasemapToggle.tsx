"use client";
/** 底圖切換列（ui-spec §2.4b）：住圖例卡內最後一列——圖例本就是
 * 「地圖上有什麼」的說明處，不另開浮動按鈕。三態如實顯示。 */
export default function BasemapToggle({ on, set, offline, outside }: {
  on: boolean; set: (v: boolean) => void; offline: boolean; outside: boolean;
}) {
  return (
    <div className="row legend-base">
      底圖
      <span className="seg">
        <button className={!on ? "on" : ""} onClick={() => set(false)}>無</button>
        <button className={on ? "on" : ""} onClick={() => set(true)}>影像</button>
      </span>
      {on && offline && <span className="meta">底圖離線</span>}
      {/* 說明「為什麼沒有」而不只說「沒有」：使用者才知道這既不是故障、
          也不是自己操作錯（設計師定案措辭） */}
      {on && !offline && outside && (
        <span className="meta">此區無影像（圖資僅涵蓋臺灣）</span>
      )}
    </div>
  );
}
