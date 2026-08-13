/** 版本端點（ui-spec §0.2f）：讓**程式**問得出前端版本。
 *
 * 這三項（tooltip／可見告示／端點）裡端點最重要——那次事故真正缺的不是
 * 「UI 上看不看得到版本」，是**沒有東西能被程式問出版本**，於是只能靠
 * 比對產物字串做考古。收集腳本、健康檢查、調查者都該能一次取得。
 */
import { buildDirty, buildSha, buildUnknown } from "@/lib/buildInfo";

export const dynamic = "force-static";

export function GET() {
  return Response.json({
    // 未知時回 null 而不是空字串——「沒有這個值」與「值是空的」不同
    sha: buildUnknown ? null : buildSha,
    unknown: buildUnknown,
    dirty: buildDirty,
  });
}
