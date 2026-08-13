/** 建置版本（ui-spec §0.2f，2026-08-13 事故後新增）。
 *
 * **為什麼有這個檔**：那次事故要回答「當下跑的是哪個版本」時，發現
 * **系統說不出自己是誰**——映像沒有 git sha、UI 沒有版本、也沒有任何端點
 * 可以被程式問。只能靠比對產物裡的 UI 字串做考古（`scripts/frontend-fingerprint.sh`）。
 * 這條只幫未來不幫那次，但**「這次沒有它」正是要做它的理由**。
 *
 * 呈現規則（設計師裁定）：
 *   - 版本已知：導覽列品名 tooltip ＋ console，零版面成本，飛行畫面不顯示。
 *   - **未知或 `-dirty`：必須有非 hover 可見的呈現**（機隊頁），因為
 *     §0.2e 是「『不知道』要說出來」——**用一個要滑鼠移上去才看得到的東西
 *     宣告「我不知道我是誰」，等於沒宣告**。
 *   - 缺值一律顯示「未知版本」，**不得留空**。
 */
const RAW = process.env.NEXT_PUBLIC_BUILD_SHA ?? "";

/** 建置時未注入 sha（本地 dev、或建置未帶 build arg）。 */
export const buildUnknown = RAW === "";
/** 建置時工作區有未提交的改動——產物不對應任何 commit。 */
export const buildDirty = RAW.endsWith("-dirty");
export const buildSha = RAW;

/** 人可讀的版本字串；未知時說「未知版本」而不是空字串。 */
export const buildLabel = buildUnknown ? "未知版本" : RAW;

/** 需要主動可見的情況：說不出自己是誰、或產物不對應任何 commit。 */
export const buildNeedsNotice = buildUnknown || buildDirty;

/** 主動可見時要說的話——說清楚後果，不只報狀態。 */
export const buildNoticeText = buildUnknown
  ? "未知版本——此建置沒有版本標記，事後無法確認畫面對應哪一版程式"
  : `版本 ${RAW}——建置時工作區有未提交的改動，不對應任何 commit`;
