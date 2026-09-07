"use client";
/** ⓘ：判讀說明的家（使用者定案 2026-09-07）。
 *
 * 原本這些句子住在版面上——卡片標頭的括號補述、清單上方的「需要注意的排
 * 前面」、頁首的「這套系統記得的每一趟飛行、每一則事件、每一個檔案」。
 * 它們每一句都是對的，但**它們是設計備忘錄，不是每次都要讀的東西**：
 * 讀第一次有用，讀第五十次只是把真正在變的數字往下擠。
 *
 * 規則：**畫面上只留事實，解釋一律進 ⓘ**。不是刪掉——刪掉的話新來的人
 * 沒有地方學會怎麼讀這一頁；是換一個「想知道才會去碰」的位置。
 *
 * 無障礙：`tabIndex` 讓鍵盤到得了（tooltip 對 `:focus-visible` 也會出現），
 * `aria-label` 讓讀螢幕的人拿到同一句話——**tooltip 不能是唯一的傳達路徑**。
 */
export default function InfoTip({ tip, className = "" }: {
  tip: string;
  className?: string;
}) {
  return (
    <span className={`info-tip ${className}`} tabIndex={0} role="note"
      aria-label={`說明：${tip}`} data-tip={tip}>ⓘ</span>
  );
}
