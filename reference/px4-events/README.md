# PX4 events metadata 字典（issue 014）

## 這是什麼

PX4 1.14 之後，機上的 vehicle 通知（Armed／Takeoff／Battery unhealthy…）**走
Events 協定而不是 STATUSTEXT**（實測：整天 tlog 裡 STATUSTEXT=0、EVENT=65）。
EVENT 訊息只帶 **event id ＋ 參數位元組**，人話文字要靠這份逐韌體的字典才翻得出。

    all_events-v1.14.3.json   取自 SITL 映像 jonasvautherin/px4-gazebo-headless:1.14.3
                              路徑 /root/Firmware/build/px4_sitl_default/events/all_events.json
                              取得日期 2026-08-13

## ⚠️ 版本綁定：字典是**跟著韌體版本走的**

事件 id 是**事件名稱的雜湊**，不同韌體版本重編譯後可能改變，新增/移除事件更是常態。
**拿 A 版字典翻 B 版韌體的事件，會翻出「看起來合理但完全錯誤」的句子**——那比
顯示原始 id 危險得多，因為讀的人不會懷疑它。

所以規則是：**版本對不上時 fallback 到 raw id，不猜、不硬翻**。

## id 的結構（實測 2026-08-13，這點文件沒寫清楚，踩過才知道）

MAVLink EVENT 訊息裡的 `id` **不是**字典的鍵：

    wire_id = (component_id << 24) | 字典鍵

字典鍵全部 < 2²⁴，而我們收到的 wire id 都在 2²⁴~2²⁵ 之間（component 1＝自駕儀）。
第一次直接拿 wire id 查表是 **0/10 命中**，套上這個關係後 **9/9 命中**。

## 真機注意

真機韌體版本若與本檔不同，要另外取一份（`build/<target>/events/all_events.json`，
或走 MAVLink FTP 從機上抓——本階段刻意不實作 FTP，先用釘檔）。
