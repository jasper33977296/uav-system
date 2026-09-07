import { redirect } from "next/navigation";

/** 舊網址。「錄製」在 2026-09-07 擴充成「資訊」（見 app/info/page.tsx），
 * 錄製與回傳成為它的第三個分頁。**舊連結不讓它死掉**——有人把這一頁存在
 * 書籤或貼在 issue 裡，一個 404 只會讓他以為功能被拿掉了。 */
export default function CapturesRedirect() {
  redirect("/info?tab=captures");
}
