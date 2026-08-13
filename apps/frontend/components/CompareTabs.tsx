"use client";
import { useRouter } from "next/navigation";

/** 比較頁的檢視切換（ui-spec §6b，2026-08-13 使用者反饋）。
 *
 * 兩個檢視共用導覽列同一顆 icon：
 *   /compare     場域訊號（多趟累積：這個場域哪裡弱）
 *   /compare/ab  前後比較（兩趟對照：這條路徑改善前後差多少）
 *
 * **為什麼要這一列**：前後比較頁原本只能手打網址——規格寫的「從軌跡卡或
 * 路徑頁使用紀錄進入」是隱藏入口，等於沒有。使用者點導覽列的比較 icon
 * 看不到熱圖與前後對比，合理結論就是「這頁沒有這些功能」。
 * **同一入口下的兩個檢視，用檢視切換而不是隱藏連結。**
 */
export default function CompareTabs({ active }: { active: "field" | "ab" }) {
  const router = useRouter();
  return (
    <div className="seg cmp-tabs">
      <button className={active === "field" ? "on" : ""}
        onClick={() => router.push("/compare")}>場域訊號</button>
      <button className={active === "ab" ? "on" : ""}
        onClick={() => router.push("/compare/ab")}>前後比較</button>
    </div>
  );
}
