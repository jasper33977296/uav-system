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
 * **氣泡畫在 body 上，不是畫在原地**（使用者 2026-09-23：「會被頁面的其他
 * 元素蓋住，請無條件把他的顯示調為最上層」）。原本是 `::after`＋`z-index: 40`，
 * 而 z-index 只在同一個堆疊脈絡裡比得了大小：父層只要有自己的 z-index／
 * transform／`overflow: hidden`（面板、側欄、表格、地圖覆蓋層都有），
 * 這個數字再大也出不去，還會被裁掉。**改成 portal ＋ `position: fixed`**，
 * 位置由觸發點的座標算，並夾在視窗內——沒有任何祖先能蓋住它。
 *
 * 無障礙：`tabIndex` 讓鍵盤到得了（focus 也會顯示），`aria-label` 讓讀螢幕的
 * 人拿到同一句話——**tooltip 不能是唯一的傳達路徑**。
 */
import { useEffect, useRef } from "react";

const W = 250;          // 氣泡寬度
const GAP = 8;          // 與 ⓘ 的距離
const EDGE = 8;         // 離視窗邊緣至少這麼遠

export default function InfoTip({ tip, className = "" }: {
  tip: string;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLSpanElement | null>(null);

  const hide = () => {
    bubble.current?.remove();
    bubble.current = null;
  };
  const show = () => {
    const el = ref.current;
    if (!el || bubble.current) return;
    const b = document.createElement("span");
    b.className = "info-tip-bubble";
    // 說明文字用 `**` 當強調記號（與 lib/emph 同一套）。舊的 CSS ::after 只能吐
    // 純文字，所以畫面上一直看得到那兩顆星——這裡把它們變成真的粗體
    tip.split("**").forEach((part, i) => {
      if (!part) return;
      if (i % 2) {
        const em = document.createElement("strong");
        em.textContent = part;
        b.appendChild(em);
      } else {
        b.appendChild(document.createTextNode(part));
      }
    });
    document.body.appendChild(b);
    bubble.current = b;
    const r = el.getBoundingClientRect();
    // 右緣對齊 ⓘ，超出視窗就往左收；下面放不下就翻到上面
    const x = Math.max(EDGE, Math.min(r.right - W, window.innerWidth - W - EDGE));
    const below = r.bottom + GAP + b.offsetHeight < window.innerHeight;
    b.style.left = `${x}px`;
    b.style.top = `${below ? r.bottom + GAP : r.top - GAP - b.offsetHeight}px`;
  };

  // 捲動或改變視窗大小時收掉——留在原地就變成指著別的東西。
  // 元件被卸載時也要收（不然氣泡會留在 body 上）
  useEffect(() => {
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      hide();
    };
  }, []);

  return (
    <span ref={ref} className={`info-tip ${className}`} tabIndex={0} role="note"
      aria-label={`說明：${tip}`}
      onPointerEnter={show} onPointerLeave={hide}
      onFocus={show} onBlur={hide}>ⓘ</span>
  );
}
